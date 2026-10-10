"""Causal historical PAPER BTC-index short sensitivity; no venue or live authority.

Uses authentic sparse hourly spot observations and actual five-minute BTC OHLC.
The separate funded-whole-contract hedge_replay gate is deliberately unchanged.
"""
from __future__ import annotations
import argparse
import csv
from dataclasses import dataclass
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
from typing import Iterable

D = Decimal
ZERO = D('0')
DAY = D('86400')
BPS = D('10000')
INTERVAL = 300
SCHEMA = 'spectra-historical-paper-hedge-v1'


def number(value: object, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError('boolean is not a financial number')
    result = D(str(value))
    if not result.is_finite() or (nonnegative and result < ZERO):
        raise ValueError('finite nonnegative financial number required')
    return result


@dataclass(frozen=True)
class Bar:
    open_timestamp: int
    completed_timestamp: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None

    @property
    def present(self) -> bool:
        return self.open is not None

    def validate(self) -> None:
        if self.open_timestamp % INTERVAL or self.completed_timestamp != self.open_timestamp + INTERVAL:
            raise ValueError('exact five-minute OHLC timing required')
        fields = (self.open, self.high, self.low, self.close)
        if not self.present:
            if any(x is not None for x in fields):
                raise ValueError('missing bar must have no fabricated price')
            return
        if any(x is None or not x.is_finite() or x <= ZERO for x in fields):
            raise ValueError('positive finite OHLC required')
        assert self.high is not None and self.low is not None and self.close is not None
        assert self.open is not None
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close) or self.low > self.high:
            raise ValueError('inconsistent OHLC envelope')


@dataclass(frozen=True)
class Observation:
    timestamp: int
    cash: Decimal
    marked_value: Decimal
    equity: Decimal

    def validate(self) -> None:
        if self.timestamp % INTERVAL:
            raise ValueError('portfolio observation is not a five-minute boundary')
        if any(not x.is_finite() or x < ZERO for x in (self.cash, self.marked_value, self.equity)):
            raise ValueError('invalid spot observation')
        if abs(self.cash + self.marked_value - self.equity) > D('1e-18'):
            raise ValueError('spot equity does not reconcile to physical cash plus marked inventory')


@dataclass(frozen=True)
class Scenario:
    ratio: Decimal
    reserve: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    carry_bps_per_day: Decimal
    initial_margin_fraction: Decimal = D('1')
    maintenance_margin_fraction: Decimal = D('0.8')
    liquidation_penalty_bps: Decimal = D('10')
    latency_seconds: int = INTERVAL
    name: str = ''

    def validate(self) -> None:
        values = (self.ratio, self.reserve, self.fee_bps, self.slippage_bps,
                  self.carry_bps_per_day, self.initial_margin_fraction,
                  self.maintenance_margin_fraction, self.liquidation_penalty_bps)
        if any(not x.is_finite() or x < ZERO for x in values):
            raise ValueError('scenario values must be finite and nonnegative')
        if not ZERO <= self.ratio <= D('1'):
            raise ValueError('hedge ratio must be between zero and one')
        if not ZERO < self.maintenance_margin_fraction <= self.initial_margin_fraction:
            raise ValueError('invalid synthetic margin assumptions')
        if not isinstance(self.latency_seconds, int) or isinstance(self.latency_seconds, bool) or self.latency_seconds < INTERVAL or self.latency_seconds % INTERVAL:
            raise ValueError('latency must be a positive whole five-minute interval')


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_bars(path: Path) -> list[Bar]:
    result = []
    with path.open(newline='') as stream:
        reader = csv.DictReader(stream)
        expected = ['open_timestamp', 'completed_timestamp', 'open', 'high', 'low', 'close', 'volume', 'trades', 'present']
        if reader.fieldnames != expected:
            raise ValueError('unexpected canonical BTC schema')
        for row in reader:
            if row['present'] not in {'0', '1'}:
                raise ValueError('invalid presence flag')
            present = row['present'] == '1'
            if not present and any(row[k] for k in ('open', 'high', 'low', 'close', 'volume', 'trades')):
                raise ValueError('missing BTC row has fabricated numerics')
            prices = [number(row[k]) if present else None for k in ('open', 'high', 'low', 'close')]
            bar = Bar(int(row['open_timestamp']), int(row['completed_timestamp']), *prices)
            bar.validate()
            result.append(bar)
    return result


def load_observations(path: Path, start: int, end: int, boundary: dict | None = None) -> list[Observation]:
    result = []
    with path.open(newline='') as stream:
        for row in csv.DictReader(stream):
            timestamp = int(row['timestamp'])
            if start < timestamp <= end:
                obs = Observation(timestamp, number(row['cash']), number(row['marked_value']), number(row['equity']))
                obs.validate()
                result.append(obs)
    if boundary is not None:
        obs = Observation(end, number(boundary['cash']), number(boundary['marked_inventory']), number(boundary['equity']))
        obs.validate()
        if result and result[-1].timestamp == end:
            if result[-1] != obs:
                raise ValueError('conflicting terminal spot observation')
        else:
            result.append(obs)
    return result


def simulate(bars: Iterable[Bar], observations: Iterable[Observation], scenario: Scenario, *, keep_path: bool = False) -> dict:
    """Cash-settled linear short. Sale proceeds are never spendable collateral.

    At each actual open: mark the previous short from the last actual close,
    first liquidate a held position if the OPEN gap already breaches maintenance,
    then execute a past signal due at exactly this opening time, pay transaction
    costs and this bar's declared carrying cost, then stress at the bar high.
    The OHLC high liquidation is a conservative stress convention, not an
    inference about the exact intrabar order of venue trades. Missing bars
    retain last actual mark and charge carry on that reference explicitly.
    """
    with localcontext() as context:
        context.prec = 50
        return _simulate(list(bars), list(observations), scenario, keep_path=keep_path)


def _simulate(bars: list[Bar], observations: list[Observation], s: Scenario, *, keep_path: bool) -> dict:
    s.validate()
    if not bars or not observations:
        raise ValueError('nonempty historical bars and spot observations required')
    for i, bar in enumerate(bars):
        bar.validate()
        if i and bar.open_timestamp != bars[i-1].completed_timestamp:
            raise ValueError('BTC calendar must be contiguous, with explicit missing rows')
    for i, obs in enumerate(observations):
        obs.validate()
        if i and obs.timestamp <= observations[i-1].timestamp:
            raise ValueError('portfolio timestamps must increase strictly')
        if not bars[0].open_timestamp < obs.timestamp <= bars[-1].completed_timestamp:
            raise ValueError('portfolio observation outside benchmark calendar')
    by_time = {x.timestamp: x for x in observations}
    cash = s.reserve
    units = ZERO
    mark: Decimal | None = None
    pending: dict[int, tuple[Decimal, int]] = {}
    halted = False
    fees = slip = carry = penalties = turnover = gross_pnl = ZERO
    trades = denied = missing_exec = missing_open_position = unknown_signals = liquidation_count = 0
    peak = D('2000') + s.reserve
    maximum_dd = ZERO
    min_cash = cash
    max_short = ZERO
    min_margin_headroom: Decimal | None = None
    min_open_margin_headroom: Decimal | None = None
    path = []
    events = []
    last_total = observations[0].equity + s.reserve
    first_total = last_total

    def liquidate(price: Decimal, reference: Decimal, timestamp: int, kind: str) -> None:
        nonlocal cash, units, gross_pnl, fees, slip, penalties, turnover, liquidation_count, trades, halted
        variation = units * (reference - price)
        cash += variation
        gross_pnl += variation
        close_notional = units * price
        fee = close_notional * s.fee_bps / BPS
        slippage = close_notional * s.slippage_bps / BPS
        penalty = close_notional * s.liquidation_penalty_bps / BPS
        cash -= fee + slippage + penalty
        fees += fee
        slip += slippage
        penalties += penalty
        turnover += close_notional
        liquidation_count += 1
        trades += 1
        halted = True
        pending.clear()
        if keep_path:
            events.append({'kind':kind,'timestamp':timestamp,'cover_price':str(price),'units':str(units),'cash_after':str(cash)})
        units = ZERO

    for bar in bars:
        if not bar.present:
            if bar.open_timestamp in pending:
                missing_exec += 1
                if keep_path:
                    events.append({'kind':'MISSING_EXECUTION_REJECTED','timestamp':bar.open_timestamp,'signal_timestamp':pending[bar.open_timestamp][1]})
                del pending[bar.open_timestamp]
            if units:
                missing_open_position += 1
                assert mark is not None
                charge = units * mark * s.carry_bps_per_day / BPS * D(INTERVAL) / DAY
                cash -= charge
                carry += charge
            current_price = mark
        else:
            assert bar.open is not None and bar.high is not None and bar.close is not None
            if mark is not None:
                variation = units * (mark - bar.open)
                cash += variation
                gross_pnl += variation
            # Existing positions must survive an opening gap before any queued
            # ordinary reduction is allowed to rescue breached collateral.
            if units:
                max_short = max(max_short, units * bar.open)
                open_headroom = cash - units * bar.open * s.maintenance_margin_fraction
                if min_open_margin_headroom is None or open_headroom < min_open_margin_headroom:
                    min_open_margin_headroom = open_headroom
                if open_headroom <= ZERO:
                    liquidate(bar.open, bar.open, bar.open_timestamp, 'OPEN_STRESS_LIQUIDATION')
            if any(t < bar.open_timestamp for t in pending):
                raise ValueError('stale pending execution escaped exact-time handling')
            if bar.open_timestamp in pending:
                desired = pending[bar.open_timestamp][0]
                delta = desired - units
                cost_notional = abs(delta) * bar.open
                fee = cost_notional * s.fee_bps / BPS
                slippage = cost_notional * s.slippage_bps / BPS
                affordable = cash - fee - slippage >= desired * bar.open * s.initial_margin_fraction
                if desired <= units or affordable:
                    cash -= fee + slippage
                    fees += fee
                    slip += slippage
                    turnover += cost_notional
                    units = desired
                    if delta:
                        trades += 1
                        if keep_path:
                            events.append({'kind':'REBALANCE','timestamp':bar.open_timestamp,'signal_timestamp':pending[bar.open_timestamp][1], 'units_change':str(delta),'open':str(bar.open),'fee':str(fee),'slippage':str(slippage)})
                else:
                    denied += 1
                    if keep_path:
                        events.append({'kind':'RESERVE_DENIED','timestamp':bar.open_timestamp,'signal_timestamp':pending[bar.open_timestamp][1],'desired_units':str(desired)})
                del pending[bar.open_timestamp]
            charge = units * bar.open * s.carry_bps_per_day / BPS * D(INTERVAL) / DAY
            cash -= charge
            carry += charge
            if units:
                max_short = max(max_short, units * bar.high)
                high_collateral = cash + units * (bar.open - bar.high)
                margin_headroom = high_collateral - units * bar.high * s.maintenance_margin_fraction
                if min_margin_headroom is None or margin_headroom < min_margin_headroom:
                    min_margin_headroom = margin_headroom
                if margin_headroom <= ZERO:
                    liquidate(bar.high, bar.open, bar.completed_timestamp, 'HIGH_STRESS_LIQUIDATION')
            variation = units * (bar.open - bar.close)
            cash += variation
            gross_pnl += variation
            mark = bar.close
            current_price = bar.close
            max_short = max(max_short, units * bar.high)
        min_cash = min(min_cash, cash)
        obs = by_time.get(bar.completed_timestamp)
        if obs is not None:
            total = obs.equity + cash
            peak = max(peak, total)
            if peak > ZERO:
                maximum_dd = max(maximum_dd, (peak - total) / peak)
            last_total = total
            if keep_path:
                path.append({'timestamp':obs.timestamp,'spot_equity':str(obs.equity),'observed_spot_marked_value':str(obs.marked_value),'hedge_cash_marked':str(cash),'short_units':str(units),'btc_last_observed_close':str(current_price) if current_price is not None else '', 'total_equity':str(total),'hourly_drawdown_fraction':str((peak-total)/peak) if peak else '0','btc_bar_present':bar.present,'hedge_halted':halted})
            if not halted and s.ratio > ZERO:
                if not bar.present:
                    unknown_signals += 1
                else:
                    assert bar.close is not None
                    target = s.ratio * obs.marked_value / bar.close
                    pending[obs.timestamp + s.latency_seconds] = (target, obs.timestamp)
    # All source samples must have been reached, and terminal accounting is exact.
    if observations[-1].timestamp != bars[-1].completed_timestamp:
        raise ValueError('exact final spot observation at final benchmark completion required')
    net = cash - s.reserve
    if abs(net - (gross_pnl - fees - slip - carry - penalties)) > D('1e-35'):
        raise AssertionError('cash/PNL reconciliation failed')
    result = {
        'scenario_name':s.name,'ratio':str(s.ratio),'reserve':str(s.reserve),
        'fee_bps':str(s.fee_bps),'slippage_bps':str(s.slippage_bps),'carry_bps_per_day':str(s.carry_bps_per_day),
        'initial_total_equity':str(D('2000')+s.reserve),'terminal_spot_equity':str(observations[-1].equity),
        'terminal_total_equity':str(last_total),'terminal_hedge_cash_marked':str(cash),
        'hedge_net_pnl':str(net),'gross_short_pnl':str(gross_pnl),'fees':str(fees),'slippage':str(slip),'carry_cost':str(carry),'liquidation_penalties':str(penalties),
        'total_profit_vs_start':str(last_total-(D('2000')+s.reserve)),
        'hourly_sampled_max_drawdown_fraction':str(maximum_dd),'minimum_hedge_cash_marked':str(min_cash),
        'maximum_intrabar_short_notional':str(max_short),'minimum_observed_high_margin_headroom':str(min_margin_headroom) if min_margin_headroom is not None else None,
        'minimum_observed_open_margin_headroom':str(min_open_margin_headroom) if min_open_margin_headroom is not None else None,
        'terminal_short_units':str(units),'terminal_short_marked_notional':str(units*mark) if mark is not None else '0',
        'turnover_notional':str(turnover),'rebalance_or_liquidation_fills':trades,'initial_margin_denied_orders':denied,
        'stress_liquidations':liquidation_count,'hedge_halted':halted,'missing_execution_rejections':missing_exec,
        'open_position_missing_btc_intervals':missing_open_position,'unknown_signal_price_rejections':unknown_signals,
        'original_spot_cash_touched':False,'terminal_pending_cancelled':bool(pending),
        'funded_original_2000_system_proven':False,'historical_venue_profit_proven':False,
        'unseen_missing_interval_liquidation_ruled_out':missing_open_position == 0,
    }
    if keep_path:
        result['path'] = path
        result['events'] = events
    return result


def run(args: argparse.Namespace) -> dict:
    protocol_path = Path(args.protocol)
    protocol = json.loads(protocol_path.read_bytes())
    if protocol.get('schema') != 'spectra-historical-paper-hedge-protocol-v1':
        raise ValueError('wrong frozen protocol schema')
    if args.protocol_sha256 != sha256(protocol_path):
        raise ValueError('frozen protocol hash mismatch')
    if args.btc_sha256 != sha256(Path(args.btc)) or args.portfolio_sha256 != sha256(Path(args.portfolio)):
        raise ValueError('historical input hash mismatch')
    if args.boundary_sha256 != sha256(Path(args.boundary)):
        raise ValueError('exact terminal observation hash mismatch')
    boundary_json = json.loads(Path(args.boundary).read_bytes())
    boundary = boundary_json.get('ending_account_summary', boundary_json)
    bars = load_bars(Path(args.btc))
    observations = load_observations(Path(args.portfolio), protocol['historical_window_start_inclusive'], protocol['historical_window_end_exclusive'], boundary)
    if bars[0].open_timestamp != protocol['historical_window_start_inclusive'] or bars[-1].completed_timestamp != protocol['historical_window_end_exclusive']:
        raise ValueError('benchmark does not cover frozen historical window')
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    results = []
    primary = []
    for reserve in protocol['extra_cash_reserve_usd']:
        for cost in protocol['transaction_scenarios']:
            for funding in protocol['short_carry_cost_bps_per_day']:
                for ratio in protocol['hedge_ratios']:
                    is_primary = reserve == protocol['primary_reserve_usd'] and cost['name'] == protocol['primary_transaction_scenario'] and funding == protocol['primary_carry_bps_per_day']
                    scenario = Scenario(D(ratio),D(reserve),D(cost['fee_bps']),D(cost['slippage_bps']),D(funding),D(protocol['initial_margin_fraction']),D(protocol['maintenance_margin_fraction']),D(protocol['liquidation_penalty_bps']),protocol['minimum_order_latency_seconds'],cost['name'])
                    result = simulate(bars, observations, scenario, keep_path=is_primary)
                    if is_primary:
                        path = result.pop('path')
                        events = result.pop('events')
                        tag = ratio.replace('.','_')
                        with (out/f'primary_{tag}_observations.csv').open('w',newline='') as stream:
                            writer = csv.DictWriter(stream,fieldnames=list(path[0]));writer.writeheader();writer.writerows(path)
                        (out/f'primary_{tag}_events.json').write_text(json.dumps(events,indent=2)+'\n')
                        primary.append(result)
                    results.append(result)
    receipt = {'schema':SCHEMA,'research_only':True,'protocol_sha256':args.protocol_sha256,
               'inputs':{'btc_sha256':args.btc_sha256,'portfolio_sha256':args.portfolio_sha256,'boundary_sha256':args.boundary_sha256},
               'btc_calendar_rows':len(bars),'btc_observed_rows':sum(b.present for b in bars),'authentic_portfolio_observation_rows_including_boundary':len(observations),
               'portfolio_resolution':'hourly, first00:05plus exact terminalJune boundary','scenario_count':len(results),'primary':primary,'scenarios':results,
               'limits':protocol['bounds']+[protocol['missing_btc_bar_policy']]}
    (out/'results.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('protocol','protocol-sha256','btc','btc-sha256','portfolio','portfolio-sha256','boundary','boundary-sha256','output'):
        parser.add_argument('--'+name,required=True)
    receipt = run(parser.parse_args())
    print(json.dumps({'schema':receipt['schema'],'scenario_count':receipt['scenario_count'],'primary':receipt['primary']},indent=2))


if __name__ == '__main__':
    main()
