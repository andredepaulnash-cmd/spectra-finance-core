"""Isolated timing/input adapter for the archived rotating fleet simulator.

The strategy body is recovered verbatim then changed only at explicit sparse,
quote, elapsed-time and execution boundaries. No archived source is edited.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import io
import json
import sys
import textwrap
import time
import zipfile
from collections import deque
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ARCHIVE_SOURCE = ROOT.parent / 'recovered/original-plan/Spectra_Exit_Strategy_Evidence/Spectra_Exit_Strategy_Evidence/spectra-finance-core'
sys.path.insert(0, str(ARCHIVE_SOURCE))
from spectra_core import rotating_fleet as original
from spectra_core.asset_fingerprint import AssetObservation
from spectra_core.binance_kline_import import MinuteBar, KlineDataset
from spectra_core.vcap_profit_exit import VCAPProfitExitPolicy

D = Decimal
START = 1767225600
END = 1782864000
INTERVAL = 300


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def prepare(data_dir, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    category_data = json.loads((ROOT.parent / 'recovered/research-20261001/dataset.json').read_text())
    categories = dict(zip(category_data['symbols'], category_data['categories']))
    symbols = sorted(s for s, c in categories.items() if c == 'crypto_candidate')
    paths = sorted(Path(data_dir).glob('kraken-2026-??-5m-usd.zip'))
    if len(paths) != 6:
        raise ValueError('exactly six monthly archives required')
    archives = [zipfile.ZipFile(p) for p in paths]
    rows = np.lib.format.open_memmap(output / 'ohlcv.npy', mode='w+', dtype='float64', shape=((END-START)//INTERVAL, len(symbols), 6))
    rows[:] = np.nan
    count = 0
    for j, symbol in enumerate(symbols):
        for z in archives:
            name = f'ohlcvt/{symbol}_5.csv'
            if name not in z.namelist():
                continue
            raw = pd.read_csv(io.BytesIO(z.read(name)), header=None).to_numpy(dtype='float64')
            ix = ((raw[:,0]-START)//INTERVAL).astype('int64')
            if np.any(raw[:,0] % INTERVAL) or np.any(ix < 0) or np.any(ix >= len(rows)):
                raise ValueError(f'out of period/unaligned rows: {symbol}')
            if len(np.unique(ix)) != len(ix):
                raise ValueError(f'duplicate rows: {symbol}')
            if np.isfinite(rows[ix,j,0]).any():
                raise ValueError(f'overlapping archive rows: {symbol}')
            if not np.isfinite(raw).all() or (raw[:,1:5] <= 0).any() or (raw[:,5:] < 0).any():
                raise ValueError(f'invalid source values: {symbol}')
            if (raw[:,2] < np.maximum(raw[:,1],raw[:,4])).any() or (raw[:,3] > np.minimum(raw[:,1],raw[:,4])).any():
                raise ValueError(f'invalid source price geometry: {symbol}')
            # [open, high, low, close, base-volume, trade-count]. All observed.
            rows[ix,j,:] = raw[:,1:7]
            count += len(raw)
        if j % 60 == 0:
            print(f'Prepared {j+1}/{len(symbols)} crypto markets; actual rows={count}', flush=True)
    rows.flush()
    for z in archives:
        z.close()
    metadata = {
        'schema': 1, 'start': START, 'end': END, 'interval_seconds': INTERVAL,
        'symbols': symbols, 'pairs': [f'{s[:-3]}-USD' for s in symbols],
        'source_hashes': {p.name: sha(p) for p in paths},
        'observed_rows': count, 'category_source': 'prior validated dataset.json',
        'no_hindsight_filter': True,
        'quote_turnover_estimate': 'recorded close times recorded base volume; Kraken candles do not contain quote turnover',
    }
    (output/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    print(json.dumps({'prepared':str(output),'markets':len(symbols),'rows':count}),flush=True)


class RecordedBar:
    __slots__ = ('timestamp','raw','_cache','execution')
    def __init__(self, timestamp, raw, execution):
        self.timestamp = timestamp
        self.raw = raw
        self._cache = {}
        self.execution = execution
    def dec(self, k):
        if k not in self._cache:
            self._cache[k] = D(str(float(self.raw[k])))
        return self._cache[k]
    @property
    def open(self): return self.dec(0)
    @property
    def high(self): return self.dec(1)
    @property
    def low(self): return self.dec(2)
    @property
    def close(self): return self.dec(3)
    @property
    def base_volume(self): return self.dec(4)
    @property
    def quote_volume(self):
        if 'q' not in self._cache:
            self._cache['q'] = self.close * self.base_volume
        return self._cache['q']
    @property
    def vwap(self):
        # This property name belongs to the original runner API. It explicitly
        # returns next actual open under the Kraken adapter, not recorded VWAP.
        return self.open if self.base_volume > 0 else None


class SparseDataset:
    def __init__(self, prepared_dir, *, stop_frames=None, recovery_enabled=True, conservative_capacity=False):
        self.prepared_dir = Path(prepared_dir)
        self.meta = json.loads((self.prepared_dir/'metadata.json').read_text())
        self.rows = np.load(self.prepared_dir/'ohlcv.npy',mmap_mode='r')
        self.pairs = tuple(self.meta['pairs'])
        self.frames = min(len(self.rows),stop_frames) if stop_frames else len(self.rows)
        self.interval_seconds = self.meta['interval_seconds']
        self.window_frames = 86400//self.interval_seconds
        self.start = self.meta['start']
        self.marks = {}
        self.last_seen = {}
        self.previous_bars = {}
        self.current_bars = {}
        self.current_index = -1
        self.recovery_enabled = recovery_enabled
        self.conservative_capacity = conservative_capacity
        self.capacity_policy = 'min_previous_execution_bar' if conservative_capacity else 'previous_bar'
        self.volume_history = deque()
        self.start_clock = time.monotonic()
        capacity_description = 'minimum of previous observed decision-bar and actual execution-bar close*base-volume' if conservative_capacity else 'previous observed decision-bar close*base-volume'
        self.execution_model = f'next immediate actual 5m open plus adverse slippage; FOK cap 1% of {capacity_description}; expire on absent next candle'
        self.manifest = {
            **self.meta, 'sparse_timeline_grid': True, 'ledger_quote': 'USD',
            'source_fingerprint': hashlib.sha256(json.dumps(self.meta,sort_keys=True).encode()).hexdigest(),
            'adapter_source_sha256':sha(__file__), 'frames_replayed':self.frames,
            'execution_model':self.execution_model,
            'capacity_policy': self.capacity_policy,
            'capacity_actual_bar_is_ex_post_fill_feasibility_only': conservative_capacity,
            'missing_candle_policy':'no fabricated candles; no observations/orders/fills on absent bars; last actual close for marking only',
            'fingerprint_observations':'all actual 5m closes; unchanged wall-clock decay horizons',
        }
    def timestamp(self,index):
        return self.start+index*self.interval_seconds
    def frame(self,index):
        self.previous_bars=self.current_bars
        self.current_index=index
        indices=np.flatnonzero(np.isfinite(self.rows[index,:,0]))
        ts=self.timestamp(index)
        self.current_bars={self.pairs[int(j)]:RecordedBar(ts,self.rows[index,int(j)],'open') for j in indices}
        if index and index % (288*7)==0:
            print(f'Replay day {index//288}/{self.frames//288}; elapsed {time.monotonic()-self.start_clock:.1f}s',flush=True)
        return self.current_bars
    def capacity(self,index,pair):
        prior=self.previous_bars.get(pair)
        prior_capacity = D(0) if prior is None else prior.quote_volume
        if self.conservative_capacity:
            current = self.current_bars.get(pair)
            return min(prior_capacity, D(0) if current is None else current.quote_volume)
        return prior_capacity
    def roll_volume(self,index,volume_24h,bars):
        # Next-open fills must authorize against previously known marks. Only
        # now, after due fills, does the newly completed candle enter marks.
        for p,b in bars.items():
            self.marks[p]=b.close
            self.last_seen[p]=b.timestamp+self.interval_seconds
        amounts={p:b.quote_volume for p,b in bars.items()}
        for p,q in amounts.items():volume_24h[p]+=q
        self.volume_history.append(amounts)
        if len(self.volume_history)>self.window_frames:
            old=self.volume_history.popleft()
            for p,q in old.items():volume_24h[p]-=q


class DenseRegressionDataset:
    """Native 60s/VWAP/current-volume adapter route for exact regression."""
    def __init__(self,dataset,recovery_enabled=True):
        self.original=dataset
        self.pairs=dataset.pairs
        self.frames=dataset.frames
        self.interval_seconds=60
        self.window_frames=1440
        self.recovery_enabled=recovery_enabled
        self.marks={}
        self.volume_history=deque()
        self.series=dataset.series
        self.execution_model='next-minute recorded VWAP plus slippage; FOK at recorded quote-volume participation cap'
        self.manifest=dict(dataset.manifest, sparse_timeline_grid=True,ledger_quote='USDT')
    def timestamp(self,index):return self.series[self.pairs[0]][index].timestamp
    def frame(self,index):
        bars={p:self.series[p][index] for p in self.pairs}
        self.bars=bars
        self.marks={p:b.close for p,b in bars.items()}
        return bars
    def capacity(self,index,pair):return self.bars[pair].quote_volume
    def roll_volume(self,index,volume_24h,bars):
        for p,b in bars.items():
            volume_24h[p]+=b.quote_volume
            if index>=1440:volume_24h[p]-=self.series[p][index-1440].quote_volume


def build_strategy_function():
    source=textwrap.dedent(inspect.getsource(original.RotatingFleetSimulator.run))
    changes=[]
    def change(old,new):
        nonlocal source
        occurrences=source.count(old)
        if occurrences!=1:
            raise RuntimeError(f'adapter expected exactly one source boundary ({occurrences}): {old[:100]}')
        source=source.replace(old,new)
        changes.append({'before':old,'after':new})
    change('if not dataset.manifest.get("complete_synchronized_grid"):', 'if not dataset.manifest.get("sparse_timeline_grid"):')
    change('raise ValueError("fleet replay requires a complete synchronized grid")','raise ValueError("adapter requires an explicit synchronized time axis, not fabricated synchronized candles")')
    change('if any(pair.split("-")[1] != "USDT" for pair in pairs):','if any(pair.split("-")[1] != dataset.manifest["ledger_quote"] for pair in pairs):')
    change('dataset.series[order.pair][index].timestamp + 60','dataset.timestamp(index) + dataset.interval_seconds')
    change('bars = {pair: dataset.series[pair][index] for pair in pairs}\n        timestamp = next(iter(bars.values())).timestamp + 60','bars = dataset.frame(index)\n        timestamp = dataset.timestamp(index) + dataset.interval_seconds')
    change('bar = bars[order.pair]\n            if (','bar = bars.get(order.pair)\n            if bar is None:\n                reject(index, order, "absent_immediate_next_candle")\n                continue\n            if (')
    change('capacity = bar.quote_volume * cfg.volume_participation','capacity = dataset.capacity(index, order.pair) * cfg.volume_participation')
    change('current.quantity * bars[pair].close','current.quantity * dataset.marks[pair]')
    change('''        for pair, bar in bars.items():
            volume_24h[pair] += bar.quote_volume
            if index >= 1440:
                volume_24h[pair] -= dataset.series[pair][index - 1440].quote_volume
            observation''','''        dataset.roll_volume(index, volume_24h, bars)
        for pair, bar in bars.items():
            observation''')
    change('position.quantity * bars[pair].close for pair, position in positions.items()','position.quantity * dataset.marks[pair] for pair, position in positions.items()')
    change('bar = bars[pair]\n            state = VCAPPositionState(','bar = bars.get(pair)\n            if bar is None:\n                continue\n            state = VCAPPositionState(')
    change('if bar.close >= average * (ONE - cfg.recovery_trigger) or spendable <= ZERO:', 'if not dataset.recovery_enabled or bar.close >= average * (ONE - cfg.recovery_trigger) or spendable <= ZERO:')
    change('''            for pair in pairs
            if pair not in positions''','''            for pair in bars
            if pair not in positions''')
    change('final_bar = {pair: dataset.series[pair][-1] for pair in pairs}','final_marks = dataset.marks')
    change('position.quantity * final_bar[pair].close','position.quantity * final_marks[pair]')
    change('"minutes_below_target_after_initialization": below_target,','"minutes_below_target_after_initialization": below_target * dataset.interval_seconds // 60,')
    change('"execution_model": "next-minute recorded VWAP plus slippage; FOK at recorded quote-volume participation cap",','"execution_model": dataset.execution_model,')
    namespace=dict(original.__dict__)
    exec(compile(source,str(HERE/'generated_strategy_body.py'),'exec'),namespace)
    return namespace['run'],changes,source


adapted_run, SOURCE_CHANGES, ADAPTED_SOURCE=build_strategy_function()
class SparseRotatingFleetSimulator(original.RotatingFleetSimulator):
    run=adapted_run


def emit_result(name,result,dataset,output,elapsed):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    payload=original.report(result)
    payload.update({
        'engine':'archived-original-rotating-fleet-sparse-kraken-adapter',
        'warning':'Original strategy logic with isolated recorded sparse5m USD timing/execution adapter; estimated fills, no historical exchange minimum/borrow validation; not a full production runtime.',
        'elapsed_seconds':elapsed, 'recovery_enabled':dataset.recovery_enabled,
        'source_adaptations':SOURCE_CHANGES,
        'planner_hashes':{p.name:sha(p) for p in (ARCHIVE_SOURCE/'spectra_core').glob('*.py') if p.name in {'asset_fingerprint.py','recovery_fingerprint.py','vcap_tranches.py','vcap_profit_exit.py','rotating_fleet.py'}},
    })
    (output/f'{name}.json').write_text(json.dumps(payload,default=str,indent=2)+'\n')
    with (output/f'{name}_fills.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=['action','signal_timestamp','execution_open_timestamp','fill_timestamp','capacity_source_bar_timestamp','pair','order_id','position_id','quantity','fillprice','gross_quote','fee','realized_pnl','basis_released','dependency_order_id'])
        writer.writeheader()
        for fill in result.fills:
            writer.writerow({'action':fill.kind,'signal_timestamp':fill.timestamp-dataset.interval_seconds,'execution_open_timestamp':fill.timestamp-dataset.interval_seconds,'fill_timestamp':fill.timestamp,'capacity_source_bar_timestamp':fill.timestamp-2*dataset.interval_seconds,'pair':fill.pair,'order_id':fill.order_id,'position_id':fill.position_id,'quantity':str(fill.quantity),'fillprice':str(fill.price),'gross_quote':str(fill.gross_quote),'fee':str(fill.fee_quote),'realized_pnl':str(fill.realized_pnl),'basis_released':str(fill.basis_released),'dependency_order_id':fill.dependency_order_id})
    with (output/f'{name}_equity.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=['timestamp','cash','committed_basis','marked_value','equity','active_positions','unique_base_assets'])
        writer.writeheader()
        for i,s in enumerate(result.samples):
            if (s.timestamp%3600==0) or i==0 or i==len(result.samples)-1:writer.writerow(asdict(s))
    terminal=[]
    final_ts=dataset.timestamp(dataset.frames-1)+dataset.interval_seconds
    for pos in result.positions:
        terminal.append({**asdict(pos),'mark':dataset.marks[pos.pair],'mark_timestamp':dataset.last_seen[pos.pair],'mark_age_seconds':final_ts-dataset.last_seen[pos.pair]})
    (output/f'{name}_terminal.json').write_text(json.dumps(terminal,default=str,indent=2)+'\n')
    (output/'adapter_source_changes.json').write_text(json.dumps(SOURCE_CHANGES,indent=2)+'\n')
    (output/'generated_strategy_body.py').write_text(ADAPTED_SOURCE)
    print(json.dumps(result.metrics,default=str,sort_keys=True),flush=True)


def run(args):
    dataset=SparseDataset(args.prepared,stop_frames=args.frames,recovery_enabled=not args.no_recovery,conservative_capacity=args.conservative_capacity)
    config=replace(original.FleetConfig(),initial_cash=D(args.capital),target_positions=args.positions,entry_budget=D(args.entry_budget),fee_bps=D(args.fee_bps),simulated_base_step=D(args.base_step))
    simulator=SparseRotatingFleetSimulator(config,exit_policy=replace(VCAPProfitExitPolicy(),target_net_profit_bps=D(args.target_bps)))
    t=time.monotonic()
    result=simulator.run(dataset)
    emit_result(args.name,result,dataset,args.output,time.monotonic()-t)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prepare');p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('run');p.add_argument('--prepared',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--name',required=True);p.add_argument('--capital',default='1000');p.add_argument('--positions',type=int,default=50);p.add_argument('--entry-budget',default='10.25');p.add_argument('--fee-bps',default='40');p.add_argument('--target-bps',default='174');p.add_argument('--base-step',default='0.000000000001');p.add_argument('--no-recovery',action='store_true');p.add_argument('--frames',type=int);p.add_argument('--conservative-capacity',action='store_true')
    args=parser.parse_args()
    if args.command=='prepare':prepare(args.data_dir,args.output)
    else:run(args)

if __name__=='__main__':main()
