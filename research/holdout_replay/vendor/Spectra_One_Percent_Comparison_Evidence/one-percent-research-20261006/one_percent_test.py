"""Research-only matched hard net-profit exits and recycling liquidity tests.

No production source edits. Strict-floor sales can remain unfilled; they use
only immediate recorded next opens and never manufacture intrabar fills.
"""
from __future__ import annotations
import argparse, csv, gzip, json, os, sys, time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT.parent
sys.path.insert(0, str(WORK/'baseline-recovery-20261001/adapter'))
import sparse_adapter as a
from spectra_core.economic_position import (EconomicLot, EconomicLotSource,
    EconomicPositionSnapshot, EconomicPositionStatus, OwnershipBucket)
from spectra_core.models import ExecutionProvenance, IntentKind
from spectra_core.partial_recycle import (PartialRecycleAction,
    PartialRecyclePolicy, PartialRecycleManifestStatus, plan_partial_recycle)
from spectra_core.kraken_market_fabric import KrakenTradingRule

D = Decimal
ZERO = D(0)
CASES = {
    'legacy_174_reuse': dict(whole_target_bps='174', partial_target_bps='174', hard_target=False, execution_floor_bps=None, park_proceeds=False),
    'legacy_174_park': dict(whole_target_bps='174', partial_target_bps='174', hard_target=False, execution_floor_bps=None, park_proceeds=True),
    'hard_100_reuse': dict(whole_target_bps='100', partial_target_bps='100', hard_target=True, execution_floor_bps='100', park_proceeds=False),
    'hard_100_park': dict(whole_target_bps='100', partial_target_bps='100', hard_target=True, execution_floor_bps='100', park_proceeds=True),
    'hard_174_reuse': dict(whole_target_bps='174', partial_target_bps='174', hard_target=True, execution_floor_bps='174', park_proceeds=False),
}

@dataclass(frozen=True)
class ResearchOrder(a.original.FleetOrder):
    def __post_init__(self):
        if self.kind == 'PARTIAL_RECYCLE':
            if self.position_id is None or self.budget != ZERO:
                raise ValueError('partial sale requires parent and zero buy budget')
        else:
            super().__post_init__()

class MemoryLedger:
    def __init__(self):
        self.lots = {}; self.by_position = {}; self.parents = {}
    def list_active_lots(self, pid):
        return [self.lots[x] for x in self.by_position.get(pid, [])
                if self.lots[x].base_remaining > ZERO]
    def snapshot(self, pid):
        lots = self.list_active_lots(pid)
        return EconomicPositionSnapshot(pid, self.parents[pid],
            EconomicPositionStatus.ACTIVE if lots else EconomicPositionStatus.CLOSED,
            sum((x.base_remaining for x in lots), ZERO),
            sum((x.cost_basis_remaining_quote for x in lots), ZERO),
            sum((x.entry_fees_remaining_quote for x in lots), ZERO),
            ZERO, ZERO, ZERO, tuple(x.lot_id for x in lots),
            tuple(x.proposal_id for x in lots if x.source == EconomicLotSource.VCAP_RECOVERY))
    def consume(self, lots, now):
        for x in lots:
            assert self.lots[x.lot_id].base_remaining == x.base_remaining > ZERO
            self.lots[x.lot_id] = replace(x, base_remaining=ZERO,
                cost_basis_remaining_quote=ZERO, entry_fees_remaining_quote=ZERO,
                updated_at=float(now))

class MemoryGovernor:
    def __init__(self): self.provenance = {}
    def get_execution_provenance(self, pid): return self.provenance.get(pid)
    def get_execution_receipt(self, pid):
        p = self.provenance.get(pid)
        return p.executor_id if p else None

class MemoryStore:
    def __init__(self, dataset):
        self.dataset = dataset; self.active = {}; self.manifests = {}; self.completions = []
    def open_for_position(self, pid): return self.active.get(pid)
    def create(self, manifest):
        assert manifest.position_id not in self.active
        self.active[manifest.position_id] = manifest
        self.manifests[manifest.manifest_id] = manifest
        self.dataset.events.append({'event':'MANIFEST_CREATE',
            'manifest':asdict(manifest), 'active_lots':
            [asdict(x) for x in self.dataset.ledger.list_active_lots(manifest.position_id)],
            **self.dataset.plan_context})
        return manifest
    def complete(self, pid, now, reason):
        m = self.active.pop(pid, None)
        if m:
            self.completions.append({'manifest_id':m.manifest_id,
                'timestamp':now, 'reason':reason})

class Dataset(a.SparseDataset):
    def __init__(self, *, case, enabled=True, frames=None, trace_path=None):
        super().__init__(WORK/'baseline-recovery-20261001/adapter/prepared',
            stop_frames=frames, conservative_capacity=True)
        self.enabled = enabled
        self.case = dict(CASES[case]); self.case_name = case
        self.hard_target = self.case['hard_target']
        self.whole_target_bps = D(self.case['whole_target_bps'])
        self.partial_target_bps = D(self.case['partial_target_bps'])
        self.execution_floor_bps = None if self.case['execution_floor_bps'] is None else D(self.case['execution_floor_bps'])
        self.park_proceeds = self.case['park_proceeds']
        self.ledger = MemoryLedger(); self.governor = MemoryGovernor()
        self.store = MemoryStore(self); self.events = []; self.orders = {}
        self.counts = Counter(); self.plan_context = {}
        self.principal_cash = ZERO; self.profit_cash = ZERO
        self.principal_reused = ZERO; self.profit_reused = ZERO
        self.principal_released = ZERO; self.gross_basis_released = ZERO
        self.partial_profit = ZERO
        self.principal_returned = ZERO
        self.trace_path = Path(trace_path).with_suffix('.csv.gz') if trace_path else None
        self.trace_file = gzip.open(self.trace_path, 'wt', newline='', compresslevel=1) if self.trace_path else None
        self.trace_writer = csv.writer(self.trace_file) if self.trace_file else None
        if self.trace_writer:
            self.trace_writer.writerow(['timestamp','position_id','pair','signal_close',
                'parent_profit_action','action','reasons','manifest_id'])
        self.manifest.update(recycle_enabled=enabled,
            partial_planner_source_sha256=a.sha(a.ARCHIVE_SOURCE/'spectra_core/partial_recycle.py'),
            research_bridge_source_sha256=a.sha(__file__),
            partial_execution='MARKET intent estimated at next immediate actual open minus5bps; same surrogate1%FOK capacity as control; no execution-time profit floor',
            ownership_boundary='exact modeled confirmed buy fills as passive recovery owners; in-memory immutable source scopes; live accounting-health, persistence and reconciliation not reproduced',
            pool_policy='archived shared fungible cash with25%reserve and5%paircap; recycling changes no allocation rule',
            proceeds_reuse='confirmed proceeds immediately join existing sharedcash; proportional provenance attribution of recycledcash on subsequent buy fills; no spending beforefill')
        self.manifest.update(experiment_case=case, case_settings=self.case,
            partial_execution='next immediate actual open minus adverse5bps; same conservative1%FOK capacity; strict net-floor rejection when configured; no intrabar fills',
            proceeds_reuse='partial netproceeds quarantined inside totalcash/equity; ordinary whole-exit proceeds remain reusable' if self.park_proceeds else 'confirmed partial netproceeds reusable through unchanged sharedcash allocator',
            whole_exit_policy='immediate completed-close hard target; previous reached-floor memory cannot force sale belowtarget' if self.hard_target else 'unchanged archived DTTP')
    def parked_cash(self):
        return self.principal_cash + self.profit_cash if self.park_proceeds else ZERO
    def floor_passes(self, basis, proceeds):
        return self.execution_floor_bps is None or proceeds >= basis*(D(1)+self.execution_floor_bps/D(10000))
    def note_full(self, order, position, close, estimated_bps, effective_action):
        self.events.append({'event':'FULL_SUBMIT', 'order':asdict(order),
            'signal_close':close, 'parent_quantity':position.quantity,
            'parent_basis':position.basis, 'estimated_net_bps':estimated_bps,
            'effective_action':str(effective_action), 'target_bps':str(self.whole_target_bps),
            'execution_floor_bps':None if self.execution_floor_bps is None else str(self.execution_floor_bps)})
    def record_buy(self, fill, position, cash_before):
        pid = str(fill.position_id); proposal = f'buy:{fill.order_id}'
        executor = f'exec:{fill.order_id}'
        source = EconomicLotSource.BASE_ENTRY if fill.kind == 'ENTRY' else EconomicLotSource.VCAP_RECOVERY
        lot = EconomicLot(f'lot:{fill.order_id}', pid, fill.pair,
            OwnershipBucket.ACTIVE_TRADE, source, fill.quantity,
            fill.gross_quote, fill.fee_quote, fill.quantity,
            fill.gross_quote, fill.fee_quote, proposal, executor,
            float(fill.timestamp), float(fill.timestamp))
        self.ledger.lots[lot.lot_id] = lot
        self.ledger.by_position.setdefault(pid, []).append(lot.lot_id)
        self.ledger.parents[pid] = fill.pair
        intent = IntentKind.ENTRY if fill.kind == 'ENTRY' else IntentKind.RECOVERY
        self.governor.provenance[proposal] = ExecutionProvenance(proposal,
            executor, fill.pair, intent, fill.gross_quote, D(1),
            'research-account', 'recorded-kraken', f'parent:{pid}')
        total = fill.gross_quote + fill.fee_quote
        parked_before = self.parked_cash()
        assert total <= cash_before-parked_before+D('1e-18')
        used_principal = ZERO if self.park_proceeds else self.principal_cash * total / cash_before
        used_profit = ZERO if self.park_proceeds else self.profit_cash * total / cash_before
        self.principal_cash -= used_principal; self.profit_cash -= used_profit
        self.principal_reused += used_principal; self.profit_reused += used_profit
        self.events.append({'event':'BUY', 'fill':asdict(fill), 'lot':asdict(lot),
            'cash_before':cash_before, 'recycled_principal_used':used_principal,
            'recycled_profit_used':used_profit, 'parked_cash_before':parked_before,
            'deployable_cash_before':cash_before-parked_before})
        self.check_parent(position)
    def check_parent(self, position):
        snap = self.ledger.snapshot(str(position.position_id))
        assert abs(snap.active_base-position.quantity) < D('1e-18')
        assert abs(snap.active_cost_basis_quote+snap.active_entry_fees_quote-position.basis) < D('1e-18')
    def record_full_exit(self, fill):
        pid = str(fill.position_id); lots = self.ledger.list_active_lots(pid)
        self.events.append({'event':'FULL_EXIT', 'fill':asdict(fill),
            'lots':[asdict(x) for x in lots]})
        self.ledger.consume(lots, fill.timestamp)
        self.store.complete(pid, fill.timestamp, 'whole_parent_exit_superseded_unfilled_manifest')
    def recycle_manifest(self, position, close, timestamp, cfg, profit_action):
        if not self.enabled or profit_action in {a.original.VCAPProfitExitAction.EXIT,
                                                 a.original.VCAPProfitExitAction.EXTEND}:
            return None
        pid = str(position.position_id)
        # Bypass positions without RECOVERY owners. This does not change selection.
        if not any(x.source == EconomicLotSource.VCAP_RECOVERY
                   for x in self.ledger.list_active_lots(pid)):
            return None
        self.plan_context = {'signal_timestamp':timestamp, 'parent_profit_action':str(profit_action),
            'parent_basis':position.basis, 'parent_quantity':position.quantity,
            'signal_close':close, 'target_bps':str(self.partial_target_bps), 'fee_bps':str(cfg.fee_bps),
            'slippage_bps':str(cfg.slippage_bps), 'min_quote':str(cfg.simulated_min_order_quote),
            'min_base':str(cfg.simulated_base_step), 'modeled_owner_status':'PASSIVE_AFTER_COMPLETE_BUY'}
        rule = KrakenTradingRule(position.pair, cfg.simulated_base_step,
            cfg.simulated_min_order_quote, ZERO, ZERO, cfg.simulated_base_step,
            ZERO, True, True)
        decision = plan_partial_recycle(ledger=self.ledger, governor=self.governor,
            position_id=pid, trading_rule=rule, current_price=close,
            expected_exit_fee_bps=cfg.fee_bps, slippage_allowance_bps=cfg.slippage_bps,
            now=timestamp, policy=PartialRecyclePolicy(self.partial_target_bps), store=self.store)
        self.counts['planner_calls'] += 1
        self.counts[decision.reasons[-1]] += 1
        if self.trace_writer:
            self.trace_writer.writerow([timestamp,pid,position.pair,str(close),
                str(profit_action),str(decision.action),'|'.join(decision.reasons),
                decision.manifest.manifest_id if decision.manifest else ''])
        return decision.manifest if decision.action == PartialRecycleAction.RECYCLE else None
    def note_partial(self, order, manifest, close, profit_action):
        self.orders[order.order_id] = manifest
        self.events.append({'event':'PARTIAL_SUBMIT', 'order':asdict(order),
            'manifest_id':manifest.manifest_id, 'signal_close':close,
            'parent_profit_action':str(profit_action)})
    def fill_partial(self, order, position, index, timestamp, cfg, capacity):
        if position is None or position.position_id != order.position_id:
            return None, 'stale_position_identity'
        manifest = self.orders[order.order_id]
        lots = [self.ledger.lots[x] for x in manifest.lot_ids]
        if any(x.base_remaining <= ZERO or x.source != EconomicLotSource.VCAP_RECOVERY for x in lots):
            return None, 'stale_recovery_manifest'
        quantity = sum((x.base_remaining for x in lots), ZERO)
        gross_basis = sum((x.cost_basis_remaining_quote for x in lots), ZERO)
        entry_fees = sum((x.entry_fees_remaining_quote for x in lots), ZERO)
        price = self.current_bars[order.pair].open * (D(1)-cfg.slippage_bps/D(10000))
        gross = quantity*price
        if quantity < manifest.min_order_size or gross < manifest.min_notional_quote:
            return None, 'partial_execution_minimum'
        if gross > capacity:
            return None, 'volume_participation_fok'
        fee = gross*cfg.fee_bps/D(10000)
        basis = gross_basis+entry_fees; proceeds = gross-fee; pnl = proceeds-basis
        if not self.floor_passes(basis, proceeds):
            return None, 'net_profit_limit_unfilled'
        fill = a.original.FleetFill(timestamp, order.order_id, order.pair,
            'PARTIAL_RECYCLE', position.position_id, quantity, price, gross,
            fee, basis, pnl, None)
        self.events.append({'event':'PARTIAL_FILL', 'fill':asdict(fill),
            'manifest_id':manifest.manifest_id, 'lots':[asdict(x) for x in lots],
            'gross_recovery_exposure_released':gross_basis, 'all_in_principal_released':basis})
        self.ledger.consume(lots, timestamp)
        self.store.complete(str(position.position_id), timestamp, 'modeled_confirmed_full_manifest_fill')
        self.principal_released += basis; self.gross_basis_released += gross_basis
        self.partial_profit += pnl
        # Cash coloring is bookkeeping only. A gapped loss returns less principal.
        self.principal_cash += min(basis, proceeds)
        self.principal_returned += min(basis, proceeds)
        self.profit_cash += max(ZERO, pnl)
        updated = replace(position, quantity=position.quantity-quantity,
            basis=position.basis-basis)
        assert updated.quantity > ZERO and updated.basis >= cfg.minimum_position_basis
        self.check_parent(updated)
        return (fill, updated), None

def build():
    source = a.ADAPTED_SOURCE; changes = []
    def change(old, new):
        nonlocal source
        assert source.count(old) == 1, (old, source.count(old))
        source = source.replace(old, new); changes.append({'before':old, 'after':new})
    change('due = sorted(pending, key=lambda order: order.kind != "EXIT")',
           'due = sorted(pending, key=lambda order: {"EXIT":0,"PARTIAL_RECYCLE":1}.get(order.kind,2))')
    change('            if order.kind == "EXIT":', '''            if order.kind == "PARTIAL_RECYCLE":
                filled, reason = dataset.fill_partial(order, position, index, timestamp, cfg, capacity)
                if filled is None:
                    reject(index, order, reason)
                    continue
                fill, updated = filled
                cash += fill.gross_quote - fill.fee_quote
                realized += fill.realized_pnl
                fees += fill.fee_quote
                turnover += fill.gross_quote
                positions[order.pair] = updated
                fills.append(fill)
                successful_orders.add(order.order_id)
                continue
            if order.kind == "EXIT":''')
    change('                del positions[order.pair]',
           '                dataset.record_full_exit(fills[-1])\n                del positions[order.pair]')
    change('            successful_orders.add(order.order_id)\n            if order.depends_on_order_id is not None:',
           '            dataset.record_buy(fills[-1], position, cash + total)\n            successful_orders.add(order.order_id)\n            if order.depends_on_order_id is not None:')
    change('        recovery_orders: list[FleetOrder] = []',
           '        recovery_orders: list[FleetOrder] = []\n        partial_orders: list[FleetOrder] = []')
    change('            average = position.basis / position.quantity', '''            partial = dataset.recycle_manifest(position, bar.close, timestamp, cfg, exit_plan.action)
            if partial is not None:
                order = next_order(timestamp, pair, "PARTIAL_RECYCLE", position_id=position.position_id)
                dataset.note_partial(order, partial, bar.close, exit_plan.action)
                partial_orders.append(order)
                pending_pairs.add(pair)
                continue
            average = position.basis / position.quantity''')
    change('        pending = exit_orders + recovery_orders + entry_orders',
           '        pending = exit_orders + partial_orders + recovery_orders + entry_orders')
    change('                cash += proceeds\n                realized += pnl', '''                if not dataset.floor_passes(position.basis, proceeds):
                    reject(index, order, "net_profit_limit_unfilled")
                    continue
                cash += proceeds
                realized += pnl''')
    change('            if total > cash:\n', '''            if total > cash - dataset.parked_cash():
                reject(index, order, "parked_partial_proceeds" if dataset.park_proceeds else "insufficient_shared_cash")
                continue
            if total > cash:
''')
    change('        spendable = max(ZERO, cash - equity * cfg.reserve_fraction)',
           '        spendable = min(max(ZERO, cash - equity * cfg.reserve_fraction), max(ZERO, cash - dataset.parked_cash()))')
    change('            decision_digest.update(', '''            if dataset.hard_target:
                effective_action = (VCAPProfitExitAction.EXIT
                    if exit_plan.estimated_net_profit_bps >= dataset.whole_target_bps
                    else VCAPProfitExitAction.WAIT)
                effective_reason = ("strict_hard_net_target_reached"
                    if effective_action == VCAPProfitExitAction.EXIT else "strict_hard_net_target_not_reached")
            decision_digest.update(''')
    change('                exit_orders.append(order)', '''                dataset.note_full(order, position, bar.close,
                    exit_plan.estimated_net_profit_bps, effective_action)
                exit_orders.append(order)''')
    change('dataset.recycle_manifest(position, bar.close, timestamp, cfg, exit_plan.action)',
           'dataset.recycle_manifest(position, bar.close, timestamp, cfg, effective_action)')
    ns = dict(a.original.__dict__); ns['FleetOrder'] = ResearchOrder
    exec(compile(source, str(ROOT/'generated_recycle_body.py'), 'exec'), ns)
    return ns['run'], source, changes

run_body, GENERATED_SOURCE, CHANGES = build()
class Simulator(a.original.RotatingFleetSimulator): run = run_body

def run(args):
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    data = Dataset(case=args.case, enabled=not args.disabled, frames=args.frames,
        trace_path=out/'planner_decisions.csv')
    cfg = replace(a.original.FleetConfig(), initial_cash=D(2000))
    simulator = Simulator(cfg,
        exit_policy=replace(a.VCAPProfitExitPolicy(), target_net_profit_bps=data.whole_target_bps),
        exit_strategy=replace(a.original.FleetExitStrategy(), mode=a.original.FleetExitMode.HARD_TARGET if data.hard_target else a.original.FleetExitMode.DTTP))
    t = time.monotonic(); result = simulator.run(data)
    if data.trace_file:
        data.trace_file.flush(); data.trace_file.close()
        with data.trace_path.open('rb') as durable:
            os.fsync(durable.fileno())
        with gzip.open(data.trace_path, 'rt', newline='') as trace:
            actual_calls = sum(1 for _ in csv.reader(trace))-1
        assert actual_calls == data.counts['planner_calls'], (actual_calls, data.counts['planner_calls'])
    a.emit_result(args.name, result, data, out, time.monotonic()-t)
    payload = json.loads((out/f'{args.name}.json').read_text())
    partials = [x for x in result.fills if x.kind == 'PARTIAL_RECYCLE']
    payload.update(engine='archived-control-plus-actual-PR20-selection-research-bridge',
        warning='Research-only in-memory ownership and generic modeled minimums; not full live lifecycle/executor. Strict limit floors are additional research behavior, not actual PR20 MARKET executor. No BTD/shorts/newselectors/deposits/withdrawals.',
        experiment_case=args.case, case_settings=data.case,
        planner_trace_file=data.trace_path.name, planner_trace_rows=actual_calls,
        planner_trace_sha256=a.sha(data.trace_path),
        partial_recycle_fills=len(partials), partial_realized_pnl=str(data.partial_profit),
        partial_gross_basis_released=str(data.gross_basis_released),
        partial_fee_inclusive_principal_released=str(data.principal_released),
        partial_principal_cash_returned=str(data.principal_returned),
        recycled_principal_cash_reused=str(data.principal_reused),
        recycled_surplus_cash_reused=str(data.profit_reused),
        ending_recycled_principal_cash=str(data.principal_cash),
        ending_recycled_surplus_cash=str(data.profit_cash),
        negative_partial_fills=sum(x.realized_pnl < ZERO for x in partials),
        partial_below_174bps_fills=sum(x.realized_pnl/x.basis_released*D(10000)<D(174) for x in partials),
        partial_below_case_target_fills=sum(x.realized_pnl/x.basis_released*D(10000)<data.partial_target_bps for x in partials),
        whole_exit_below_case_target_fills=sum(x.kind=='EXIT' and x.realized_pnl/x.basis_released*D(10000)<data.whole_target_bps for x in result.fills),
        parked_partial_net_proceeds=str(data.parked_cash()),
        planner_counts=dict(data.counts), open_partial_manifests=len(data.store.active),
        recycle_source_adaptations=CHANGES)
    (out/f'{args.name}.json').write_text(json.dumps(payload, indent=2)+'\n')
    (out/'events.json').write_text(json.dumps(data.events, default=str, indent=2)+'\n')
    (out/'terminal_lots.json').write_text(json.dumps([asdict(x) for x in data.ledger.lots.values() if x.base_remaining>ZERO], default=str, indent=2)+'\n')
    (out/'manifests.json').write_text(json.dumps([asdict(x) for x in data.store.manifests.values()], default=str, indent=2)+'\n')
    (out/'manifest_completions.json').write_text(json.dumps(data.store.completions, indent=2)+'\n')
    (out/'recycle_source_changes.json').write_text(json.dumps(CHANGES, indent=2)+'\n')
    (out/'generated_recycle_body.py').write_text(GENERATED_SOURCE)
    print(json.dumps({k:payload[k] for k in ['partial_recycle_fills','partial_realized_pnl','partial_fee_inclusive_principal_released','recycled_principal_cash_reused','open_partial_manifests']}), flush=True)

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--frames', type=int); p.add_argument('--disabled', action='store_true')
    p.add_argument('--case', choices=tuple(CASES), required=True)
    p.add_argument('--name', default='recycle_reuse_2000'); p.add_argument('--output', default=str(ROOT/'recycle'))
    run(p.parse_args())
