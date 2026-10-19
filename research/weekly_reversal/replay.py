"""One fixed weekly paper replay over actual native accounting components.

Historical entry requires separately confirmed source/protocol/registry admission.
Synthetic fixtures are software tests. There are no live clients or order routes.
"""
from collections import Counter
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path
import hashlib
import json
import time

from spectra_core.reversal_signals import IndicatorHistory, SignalGate, SignalPolicy, SignalObservation
from .compounding_adapter import WeeklyCompoundingAdapter
from .owned_cash_reversal import OwnedCashReversal
from .owned_spot.adapter import serial
from .owned_spot.model import Blocked, CapitalBlocked, CompletedBar, ObservedOpen, ZERO
from .entry_admission import AdmissionBlocked
from .warmup import prime_empty_account
from .diagnostics import inventory_report, cycle_report


def dumps(value):
    return json.dumps(serial(value),sort_keys=True,separators=(',',':'),allow_nan=False)


class WeeklyReplay:
    def __init__(self,directory,*,tape,config,membership,signal_policy,protocol_sha256,admission=None):
        if not isinstance(signal_policy,SignalPolicy) or not signal_policy.enabled:
            raise ValueError('Explicit admitted signal policy required')
        if not tape.synthetic:
            if (not admission or admission.get('historical_execution_admitted') is not True
                    or admission.get('confirmed_launch_fence') is not True
                    or admission.get('protocol_sha256')!=protocol_sha256 or not admission.get('run_id')):
                raise ValueError('Confirmed historical launch admission required')
        input_identity=tape.seal()
        if not tape.synthetic and (not input_identity.get('source_file_sha256')
                or admission.get('input_sha256')!=input_identity['source_file_sha256']):
            raise ValueError('Historical tape identity is not bound to admitted input')
        self.root=Path(directory)
        if self.root.exists() and any(self.root.iterdir()):
            raise ValueError('Fresh replay requires empty output directory; use verified checkpoint restore')
        self.root.mkdir(parents=True,exist_ok=True)
        self.tape=tape;self.protocol_sha256=protocol_sha256;self.admission=admission
        self.policy=signal_policy;self.started=time.monotonic()
        self.a=WeeklyCompoundingAdapter(self.root/'account',config,now=tape.warmup_start,membership=membership,enabled=True)
        self.histories=prime_empty_account(self.a,tape,signal_policy=signal_policy)
        self.gate=SignalGate.loads(self.a.state['weekly_signal_gate'])
        self.observations={p:SignalObservation(**raw) for p,raw in self.a.state['weekly_last_signal_observations'].items()}
        self.references={};self.counts=Counter();self.steps=0;self.last_at=None
        self.metrics={'initial_equity':'2000','close_peak':'2000','max_close_drawdown':'0','max_close_drawdown_fraction':'0',
            'max_completed_bar_stress_drawdown':'0','max_completed_bar_stress_fraction':'0',
            'complete_marks':0,'incomplete_marks':0,'stale_mark_pairs':{},'last_marked_equity':None,
            'last_complete_equity_at':None,'last_observation_at':None,'last_observation_complete':False,'last_missing_owned_bars':[],
            'cash_wait_seconds':{},'cash_wait_rebound_observations':{},'cash_wait_upward_price_change':{}}
        self.signal_decisions={};self.activation_at=None;self.psm_recovery_at={};self.repair_observation_reasons={}
        self.events=(self.root/'events.jsonl').open('a')
        self.marks=(self.root/'marks.jsonl').open('a')
        self.signal_log=(self.root/'signals.jsonl').open('a')

    def close(self):
        for name in ('events','marks','signal_log'):
            stream=getattr(self,name,None)
            if stream:stream.close()
        self.a.close()

    def log(self,kind,**values):
        self.events.write(dumps({'at':int(self.a.now),'kind':kind,**values})+'\n')
        self.counts[kind]+=1

    def _blocked(self,context,pair,exc,**details):
        code=getattr(exc,'code',type(exc).__name__)
        self.log(context+'_BLOCKED',pair=pair,code=code,reason=str(exc),**details)

    def _mark_completed_bar(self,event):
        a=self.a;cash=a.cash();lots=a._lots()
        units={p:sum((l.base_remaining for l in lots if l.pair==p),ZERO) for p in self.tape.pairs}
        bars=dict(zip(event.pairs,event.completed));owned=[p for p,q in units.items() if q>0]
        missing=[p for p in owned if bars[p] is None]
        complete=not missing
        m=self.metrics
        m['last_observation_at']=event.at;m['last_observation_complete']=complete
        m['last_missing_owned_bars']=missing
        if complete:
            equity=cash+sum((units[p]*bars[p].close for p in owned),ZERO)
            low=cash+sum((units[p]*bars[p].low for p in owned),ZERO)
            peak=max(D(m['close_peak']),equity)
            dd=peak-equity;stress=peak-low
            m['close_peak']=str(peak)
            m['max_close_drawdown']=str(max(D(m['max_close_drawdown']),dd))
            m['max_close_drawdown_fraction']=str(max(D(m['max_close_drawdown_fraction']),dd/peak if peak>0 else ZERO))
            m['max_completed_bar_stress_drawdown']=str(max(D(m['max_completed_bar_stress_drawdown']),stress))
            m['max_completed_bar_stress_fraction']=str(max(D(m['max_completed_bar_stress_fraction']),stress/peak if peak>0 else ZERO))
            m['last_marked_equity']=str(equity);m['last_complete_equity_at']=event.at;m['complete_marks']+=1
        else:
            equity=low=None;m['incomplete_marks']+=1
            for p in missing:m['stale_mark_pairs'][p]=m['stale_mark_pairs'].get(p,0)+1
        self.marks.write(dumps({'at':event.at,'cash':cash,'units':units,'equity':equity,'simultaneous_low_equity_bound':low,
            'complete':complete,'missing_owned_bars':missing,'scope':'COMPLETED_BAR_BEFORE_NEXT_OPEN_FILLS'})+'\n')
        elapsed=0 if self.last_at is None else event.at-self.last_at
        for pair,meta in a.state['psm'].items():
            if meta['mode']=='PSM' and a.psm._position(pair)['phase']=='REBUILD' and a.cash_pools().get('psm:'+pair,ZERO)>0:
                m['cash_wait_seconds'][pair]=m['cash_wait_seconds'].get(pair,0)+elapsed
                bar=bars[pair]
                if bar is not None and bar.close>bar.open:
                    m['cash_wait_rebound_observations'][pair]=m['cash_wait_rebound_observations'].get(pair,0)+1
                    prior=D(m['cash_wait_upward_price_change'].get(pair,'0'))
                    m['cash_wait_upward_price_change'][pair]=str(prior+bar.close-bar.open)

    def _observe(self,event):
        a=self.a;a.advance(event.at)
        for bar in event.completed:
            if bar is None:continue
            old=a.state['marks'].get(bar.pair)
            if old and old['completed_at']==bar.completed_at:continue # warmup includes exact start boundary
            a.observe(CompletedBar(bar.pair,bar.opened_at,bar.completed_at,bar.close,bar.quote_volume_proxy,bar.completed_at),now=event.at)
        a._save()
        a.refresh_accounting_all(now=event.at)
        if event.completed_hours is not None and event.at>self.tape.start:
            for pair,bar in zip(event.pairs,event.completed_hours):
                if bar is None:
                    self.counts['INCOMPLETE_HOUR:'+pair]+=1;continue
                obs=self.histories[pair].push(bar,observed_at=event.at)
                self.observations[pair]=obs
                a.state.setdefault('next_entry_completed_hours',{}).setdefault(pair,[]).append(asdict(bar))
                decision=self.gate.evaluate(obs,now=event.at)
                self.signal_decisions[pair]=decision
                self.signal_log.write(dumps({'at':event.at,'observation':asdict(obs),'decision':decision})+'\n')
                self.counts['SIGNAL:'+decision['reason']+':'+pair]+=1
                ref=self.references.get(pair)
                if ref is not None and ref.closed_at is None and obs.atr14 is not None:
                    result=ref.observe(bar,obs,atr=D(str(obs.atr14)),now=event.at)
                    self.log('ADAPTIVE_REVERSAL',pair=pair,result=result)
        a._save()

    def _recover_unsealed_psm(self,pair,*,now):
        meta=self.a.state['psm'].get(pair)
        action=meta.get('action') if meta else None
        if not action or (action.get('sealed') and not action.get('preparation_interrupted')):
            return False
        if action['side']!='buy':raise Blocked('Unexpected unsealed non-rebuild PSM action')
        if any(o['status']=='PREPARING' for o in self.a._pending(pair)):
            raise Blocked('Interrupted PSM child requires native restart reconciliation')
        order=action['order']
        if not action.get('sealed'):
            self.a.psm.cancel_unsealed_batch(pair,now=now)
        else:
            for oid in list(action['children']):
                if self.a.state['orders'][oid]['status'] in {'OPEN','PARTIAL','CANCEL_REQUESTED'}:
                    self.a.reconcile_terminal(oid,'CANCELED',now=now)
            self.a.psm.sync()
            if meta['action'] is not None:self.a.psm._finish_action(pair)
        if meta['action'] is not None or self.a.psm._position(pair)['pending'] is not None or self.a._pending(pair):
            raise Blocked('Unsealed PSM cancellation remains unresolved')
        self.psm_recovery_at[pair]=now
        self.log('PSM_UNSEALED_BATCH_CANCELED',pair=pair,order=order)
        return True

    def _fills(self,event):
        if event.evaluation_end:return
        a=self.a
        for j,pair in enumerate(event.pairs):
            self._recover_unsealed_psm(pair,now=event.at)
            pending=list(a._pending(pair))
            if any(o['status'] in {'PREPARING','FUNDING_PREPARED','ENTRY_PREPARED'} for o in pending):
                raise Blocked('Unresolved preparation cannot reach historical execution')
            due=[o for o in pending if o['decision_at']+a.config.execution_delay_seconds==event.at]
            if any(o['decision_at']+a.config.execution_delay_seconds<event.at for o in pending):
                raise Blocked('Overdue pending order escaped exact-open cancellation')
            if not due:continue
            opening=event.observed_opens[j]
            psm=[o for o in due if o['kind'] in {'PSM_EXIT','PSM_REBUILD'}]
            if psm:
                if len(psm)!=len(due):raise Blocked('PSM overlaps ordinary asset actions')
                if opening is None:a.psm.missing_open(pair,expected_at=event.at,now=event.at)
                else:a.psm.observe_open(pair,ObservedOpen(opening,event.at,event.at))
            else:
                for order in due:
                    if opening is None:a.missing_open(order['id'],expected_at=event.at,now=event.at)
                    else:a.observe_open(order['id'],ObservedOpen(opening,event.at,event.at))
            self.log('ORDER_EXECUTION_BOUNDARY',pair=pair,orders=[o['id'] for o in due],observed_open=opening)
        a.refresh_accounting_all(now=event.at)
        a.sync_profit_cash(now=event.at)

    def _seed(self,event):
        a=self.a
        if a.seed_complete():
            if self.activation_at is None:
                self.activation_at=event.at;self.log('SEED_BARRIER_COMPLETE',orders=len(a.state['seed_completion_receipt']['orders']))
            return True
        for pair,bar in zip(event.pairs,event.completed):
            if bar is None or a._pending(pair):continue
            try:
                if pair not in a.state['assets']:a.prepare_seed_asset(pair,now=event.at)
                else:
                    a.resume_seed_asset(pair,now=event.at)
                    for c in list(a.state['cohorts'].values()):
                        if c['pair']==pair and c.get('origin')=='SEED' and c.get('anchor') is None:
                            a.retry_seed(c['id'],now=event.at)
            except Blocked as exc:self._blocked('SEED',pair,exc)
        return False

    def _episode(self,pair):
        a=self.a;meta=a.state['psm'][pair];position=a.psm._position(pair)
        if meta['mode']=='REPAIR':
            ref=self.references.get(pair)
            if ref is not None and ref.closed_at is None:
                trades=[r['trade_id'] for r in a._receipts() if a.state['orders'][r['order_id']].get('psm_position')==meta['position']
                        and a.state['orders'][r['order_id']]['kind']=='PSM_REBUILD']
                ref.complete_owned_rebuild(at=int(a.now),actual_rebuild_receipt_ids=trades)
                self.log('PSM_CYCLE_COMPLETE',pair=pair,episode=meta['episode'],rebuild_receipts=trades)
            return False
        if meta.get('action') or a._pending(pair):return True
        phase=position['phase']
        if phase=='EXIT':
            try:a.psm.close_exit(pair,now=a.now)
            except Blocked:
                a.psm.request_sell(pair,now=a.now);return True
            meta=a.state['psm'][pair];position=a.psm._position(pair);phase=position['phase']
        if phase=='REBUILD':
            actual_units=position['original_units']+position['rebuilt_units']
            if (position['rebuilt_units']>ZERO and actual_units>position['snapshot_units']
                    and actual_units>=position['rebuild_target']):
                # Per-owner rounding can leave the aggregate logical order
                # canceled with a tiny residual even after actual unit gain is
                # achieved. Use the canonical completion guard explicitly;
                # never buy dust again just to force parent-order FILLED.
                a.psm.complete(pair,now=a.now)
                if a.state['psm'][pair]['mode']!='REPAIR':
                    raise Blocked('Canonical completion failed to hand back ownership')
                return self._episode(pair)
            ref=self.references.get(pair)
            if ref is None or ref.state.cycle_id!=meta['position']:
                receipts=[r for r in a._receipts() if a.state['orders'][r['order_id']].get('psm_position')==meta['position']
                          and a.state['orders'][r['order_id']]['kind']=='PSM_EXIT']
                ref=OwnedCashReversal.from_settled_sales(cycle_id=meta['position'],pair=pair,sale_receipts=receipts,sealed_at=int(a.now))
                self.references[pair]=ref;self.log('PSM_CASH_REFERENCE_STARTED',pair=pair,episode=meta['episode'])
            qualification=ref.qualification(now=int(a.now))
            if qualification['eligible']:
                self.log('PSM_REBUILD_SIGNAL_QUALIFIED',pair=pair,episode=meta['episode'],qualification=qualification)
                a.psm.rebuild_signal(pair,decision_ref=qualification['intent_id'],qualified=True,
                    expires_at=qualification['source_hour']+300,now=a.now)
                try:a.psm.request_rebuild(pair,min_unit_gain=ZERO,now=a.now)
                except Blocked as exc:
                    self.log('PSM_QUALIFIED_REBUILD_BLOCKED',pair=pair,episode=meta['episode'],reason=str(exc))
                    raise
                self.log('PSM_REBUILD_ADMITTED',pair=pair,qualification=qualification)
            else:self.counts['REBUILD_SIGNAL:'+qualification['reason']+':'+pair]+=1
        return True

    def _decisions(self,event):
        a=self.a
        if not self._seed(event):return
        # Soft RSI ranking changes priority only, never turns an eligible asset off.
        ranking=sorted(event.pairs,key=lambda p:(-(self.signal_decisions.get(p,{}).get('rsi_priority') or 0),p))
        for pair in ranking:
            if self.psm_recovery_at.get(pair)==event.at:continue
            if a._pending(pair):continue
            mark=a.state['marks'].get(pair)
            if not mark or mark['completed_at']!=event.at:
                self.counts['DECISION_MISSING_CURRENT_BAR:'+pair]+=1;continue
            if pair in a.state['psm']:
                try:
                    if self._episode(pair):continue
                except Blocked as exc:
                    self._blocked('PSM',pair,exc);continue
            if a._asset(pair).get('exit'):
                try:a.continue_exit(pair,now=event.at)
                except Blocked as exc:self._blocked('COHORT_EXIT',pair,exc)
                continue
            selected=a._asset(pair)['selected']
            if selected:
                decision=a.selected_cohort_cycle(pair,now=event.at)
                if decision.action.value=='EXIT':
                    try:a.request_cohort_exit(pair,now=event.at)
                    except Blocked as exc:self._blocked('COHORT_EXIT',pair,exc)
                    continue
                selected_rung=a.state['cohorts'][selected].get('rung')
                if selected_rung and selected_rung['status'] in {'INCOMPLETE','BLOCKED'}:
                    retry=a.repair_retry_feasibility(pair,now=event.at)
                    retry_reason=retry['reason_code']
                    self.counts['REPAIR_RETRY_OBSERVATION:'+str(retry_reason)+':'+pair]+=1
                    reason_key='RETRY:'+pair
                    if self.repair_observation_reasons.get(reason_key)!=retry_reason:
                        self.log('REPAIR_RETRY_FEASIBILITY_CHANGED',pair=pair,reason_code=retry_reason,
                            reason=retry.get('reason'),cohort=selected,target=retry.get('target'))
                        self.repair_observation_reasons[reason_key]=retry_reason
                    if retry['eligible']:
                        try:
                            order=a.retry_repair(pair,now=event.at)
                            self.log('REPAIR_RETRY_ADMITTED',pair=pair,cohort=selected,
                                order=order['id'],remaining_target=order['target'],scope=order['repair_retry'])
                        except (Blocked,AdmissionBlocked) as exc:self._blocked('REPAIR_RETRY',pair,exc)
                    # The same selected partial retains its original rung. No
                    # second double, waiting-partial repair, or unproven PSM
                    # fallback is invented merely because its remainder blocks.
                    continue
            evidence=a.repair_feasibility(pair,now=event.at)
            reason=evidence['reason_code']
            self.counts['REPAIR_OBSERVATION:'+str(reason)+':'+pair]+=1
            if self.repair_observation_reasons.get(pair)!=reason:
                self.log('REPAIR_FEASIBILITY_CHANGED',pair=pair,reason_code=reason,
                         reason=evidence['reason'],cohort=evidence['cohort_id'])
                self.repair_observation_reasons[pair]=reason
            if evidence['guards']['rung_due'] and evidence['guards']['profit_clear']:
                try:
                    a.request_repair(pair,rung=evidence['next_rung'],now=event.at)
                    self.log('REPAIR_ADMITTED',pair=pair,cohort=evidence['cohort_id']);continue
                except CapitalBlocked as exc:
                    self._blocked('REPAIR_CAPITAL',pair,exc)
                    signal=self.signal_decisions.get(pair)
                    if (signal and signal['eligible'] and event.at==signal['completed_at']
                            and all(evidence['guards'].values())):
                        try:
                            a.psm.start(pair,decision_ref=f'hour:{pair}:{event.at}',falling_knife=True,repair_eligible=False,
                                policy_ref=self.protocol_sha256,now=event.at)
                            a.psm.request_sell(pair,now=event.at);self.log('PSM_SELL_ADMITTED',pair=pair,signal=signal)
                        except Blocked as other:self._blocked('PSM_SELL',pair,other)
                    else:self.counts['PSM_SIGNAL_UNAVAILABLE:'+pair]+=1
                    continue
                except (Blocked,AdmissionBlocked) as exc:
                    self._blocked('REPAIR',pair,exc);continue
            # One original sold partial at a time, only after native selected work.
            sold=sorted((c for c in a.state['cohorts'].values() if c['pair']==pair and c.get('origin')=='SEED' and c['status']=='SOLD'),key=lambda c:c['ordinal'])
            for candidate in sold:
                # An original SOLD ancestor may still own rebuilt descendants.
                # Read-only exact root proof excludes their inventory/dust before
                # attempting the next slot. Stable order never hides later roots.
                try:
                    source=a.entry_admission.root_reentry_source(candidate['id'],now=int(event.at))
                except (Blocked,AdmissionBlocked) as exc:
                    self._blocked('NEXT_ENTRY_ROOT',pair,exc,cohort=candidate['id'])
                    continue
                profit_budget=None
                try:
                    profit_budget=a.available_profit(pair)
                    order=a.request_reentry(candidate['id'],now=event.at)
                    self.log('NEXT_ENTRY_ADMITTED',pair=pair,cohort=candidate['id'],
                        order=order['id'],base_budget=order['base_budget'],profit_budget=order['profit_budget'])
                    break  # At most one admitted next entry for this asset/bar.
                except (Blocked,AdmissionBlocked) as exc:
                    self._blocked('NEXT_ENTRY',pair,exc,cohort=candidate['id'],
                        source_pool=source['source_pool'],available_base_cash=source['available_base_cash'],
                        base_budget=source['base_budget'],profit_budget=profit_budget,
                        full_target=None if profit_budget is None else D(source['base_budget'])+profit_budget,
                        unavailable_to_this_entry=source['unavailable_to_this_entry'])
                    # A failed attempt may retain uncertain executable authority.
                    # No other root may advance until that authority reconciles.
                    if a._pending(pair):break

    def step(self,event):
        expected=self.tape.start if self.last_at is None else self.last_at+300
        if event.at!=expected:raise ValueError('Completed boundaries must be processed exactly once in order')
        self.a.advance(event.at)
        self._observe(event)
        self._mark_completed_bar(event)
        self._fills(event)
        if not event.evaluation_end:self._decisions(event)
        self.a.audit(require_marks=False)
        self.last_at=event.at;self.steps+=1
        self.a._save()
        for stream in (self.events,self.marks,self.signal_log):stream.flush()
        return {'at':event.at,'steps':self.steps,'elapsed_seconds':time.monotonic()-self.started}

    def finish(self):
        if self.last_at!=self.tape.end:
            raise ValueError('Full frozen evaluation window has not completed')
        a=self.a
        # End is exclusive for fills: cancel existing intents without inspecting
        # any post-window price or pretending an economic liquidation occurred.
        for pair in self.tape.pairs:
            # Cancel an unsubmitted logical rebuild even when it has no native
            # child. No price observation or new order is needed for this cleanup.
            self._recover_unsealed_psm(pair,now=self.tape.end)
            pending=list(a._pending(pair))
            if not pending:continue
            if any(o['status'] in {'PREPARING','FUNDING_PREPARED','ENTRY_PREPARED'} for o in pending):
                raise Blocked('Unresolved entry authority prevents terminal result')
            psm=[o for o in pending if o['kind'] in {'PSM_EXIT','PSM_REBUILD'}]
            if psm:
                if len(psm)!=len(pending):raise Blocked('Mixed terminal ownership')
                a.psm.missing_open(pair,expected_at=self.tape.end,now=self.tape.end)
            else:
                for order in pending:a.reconcile_terminal(order['id'],'CANCELED',now=self.tape.end)
            self.log('WINDOW_END_CANCEL',pair=pair,orders=[o['id'] for o in pending])
        a.refresh_accounting_all(now=self.tape.end);a.sync_profit_cash(now=self.tape.end)
        a._assert_clean(health=False)
        terminal_projection=a.profit_bridge.before_new_spending()
        if not terminal_projection.new_spending_allowed:
            raise Blocked('Terminal cash authority remains quarantined or unreconciled')
        report=a.report();receipts=a._receipts()
        if (report['accounting_status']!='RECONCILED' or report['unapplied_receipt_ids']
                or any(report['native_inventory_discrepancy'].values())):
            raise Blocked('Terminal native accounting has unresolved discrepancy')
        report.update(protocol_sha256=self.protocol_sha256,synthetic=self.tape.synthetic,
            window={'warmup_start':self.tape.warmup_start,'start':self.tape.start,'end_exclusive':self.tape.end},
            signal_policy=asdict(self.policy),counts=dict(self.counts),metrics=self.metrics,
            allocation=a.allocator.audit(),funding_cash=a.funding_cash(),capital_commitments=a.capital_commitments(),
            actual_fill_count=len(receipts),sale_fill_count=sum(r['trade_type']=='SELL' for r in receipts),
            buy_fill_count=sum(r['trade_type']=='BUY' for r in receipts),
            actual_fees=sum((D(r['fee_paid']) for r in receipts),ZERO),
            inventory=inventory_report(a,at=self.tape.end),
            psm_cycles=cycle_report(a,receipts),
            canceled_native_orders=sum(o['status']=='CANCELED' for o in a.state['orders'].values()),
            rejected_native_orders=sum(o['status']=='REJECTED' for o in a.state['orders'].values()),
            unfinished_repair_rungs=[{'cohort_id':c['id'],'pair':c['pair'],
                'generation':c.get('generation',1),'rung':c['rung']}
                for c in a.state['cohorts'].values() if c.get('rung') and
                c['rung']['status'] not in {'COMPLETE','ROUNDING_COMPLETED'}],
            pending_exit_plans=[{'pair':pair,'manifest':asset['exit'],'execution_state':'WINDOW_ENDED_NO_DISPATCH'}
                for pair,asset in a.state['assets'].items() if asset.get('exit')],
            pending_psm_actions=[{'pair':pair,'action':meta.get('action'),
                'logical_order':a.psm._position(pair).get('pending')}
                for pair,meta in a.state['psm'].items() if meta.get('action') is not None
                or a.psm._position(pair).get('pending') is not None],
            pending_orders=[o['id'] for o in a._pending()],
            pending_cash_reconciliation=list(a.profit_bridge.cash_projection().pending_trade_ids),
            activation_at=self.activation_at,elapsed_seconds=time.monotonic()-self.started,
            scope='OWNED_SPOT_PAPER_ADAPTIVE_REBUILD_WITH_NEXT_ENTRY_COMPOUNDING',
            derivative_pnl=None,completed=False)
        if report['pending_orders'] or report['pending_cash_reconciliation'] or report['pending_psm_actions']:
            raise Blocked('Terminal result has unresolved executable or cash authority')
        # Completion of the bounded replay does not establish a fresh valuation.
        # Preserve last-observed diagnostics but never label a missing exact final
        # owned-asset candle as current equity, even if a native age tolerance passes.
        final_bars=dict(zip(self.tape.pairs,self.tape.boundary(self.tape.end).completed))
        missing=sorted({lot.pair for lot in a._lots() if lot.base_remaining>ZERO and final_bars.get(lot.pair) is None})
        report['missing_owned_bars']=missing
        report['last_complete_equity']=self.metrics['last_marked_equity']
        report['last_complete_equity_at']=self.metrics['last_complete_equity_at']
        report['final_valuation_at']=self.tape.end
        if missing:
            keys=('equity','total_pnl','unrealized_pnl','market_exposure',
                  'receipt_implied_equity','receipt_implied_total_pnl','receipt_implied_market_exposure')
            report['last_observed_valuation_diagnostic']={key:report.get(key) for key in keys}
            for key in keys:report[key]=None
            report['valuation_complete']=False
            report['valuation_scope']='INCOMPLETE_FINAL_OWNED_MARKS'
        report['completed']=True
        (self.root/'result.json').write_text(dumps(report)+'\n')
        return report

    def export_state(self):
        return {'schema':2,'protocol_sha256':self.protocol_sha256,'admission':self.admission,
            'input_identity':self.tape.input_identity,
            'synthetic':self.tape.synthetic,'bounds':[self.tape.warmup_start,self.tape.start,self.tape.end],
            'pairs':list(self.tape.pairs),'config':asdict(self.a.config),'membership':asdict(self.a.membership),
            'policy':asdict(self.policy),'histories':{p:h.dumps() for p,h in self.histories.items()},
            'gate':self.gate.dumps(),'observations':{p:asdict(o) for p,o in self.observations.items()},
            'references':{p:r.dumps() for p,r in self.references.items()},'counts':dict(self.counts),
            'steps':self.steps,'last_at':self.last_at,'metrics':self.metrics,'signal_decisions':self.signal_decisions,
            'activation_at':self.activation_at,'psm_recovery_at':self.psm_recovery_at,
            'repair_observation_reasons':self.repair_observation_reasons}

    @classmethod
    def resume_restored(cls,directory,*,tape,config,membership,protocol_sha256,confirmed_admission=None):
        root=Path(directory);raw=json.loads((root/'caller.json').read_text())
        input_identity=tape.seal()
        if (raw.get('schema')!=2 or raw.get('input_identity')!=input_identity
                or raw['protocol_sha256']!=protocol_sha256
                or raw['bounds']!=[tape.warmup_start,tape.start,tape.end]
                or raw['pairs']!=list(tape.pairs) or raw['synthetic']!=tape.synthetic
                or raw['config']!=json.loads(dumps(asdict(config)))
                or raw['membership']!=json.loads(dumps(asdict(membership)))):
            raise ValueError('Restored caller configuration/input scope differs')
        if not tape.synthetic and (not confirmed_admission or confirmed_admission.get('confirmed_launch_fence') is not True
                or confirmed_admission.get('protocol_sha256')!=protocol_sha256
                or confirmed_admission.get('run_id')!=raw['admission']['run_id']):
            raise ValueError('Same-run confirmed resume fence required')
        obj=object.__new__(cls);obj.root=root;obj.tape=tape;obj.protocol_sha256=protocol_sha256
        obj.admission=confirmed_admission or raw['admission'];obj.policy=SignalPolicy(**raw['policy']);obj.started=time.monotonic()
        obj.a=WeeklyCompoundingAdapter(root/'account',config,now=raw['last_at'],membership=membership,enabled=True)
        obj.histories={p:IndicatorHistory.loads(v) for p,v in raw['histories'].items()}
        obj.gate=SignalGate.loads(raw['gate'])
        obj.observations={p:SignalObservation(**v) for p,v in raw['observations'].items()}
        obj.references={p:OwnedCashReversal.loads(v) for p,v in raw['references'].items()}
        obj.counts=Counter(raw['counts']);obj.steps=raw['steps'];obj.last_at=raw['last_at']
        obj.metrics=raw['metrics'];obj.signal_decisions=raw['signal_decisions'];obj.activation_at=raw['activation_at'];obj.psm_recovery_at=raw['psm_recovery_at'];obj.repair_observation_reasons=raw['repair_observation_reasons']
        obj.events=(root/'events.jsonl').open('a');obj.marks=(root/'marks.jsonl').open('a');obj.signal_log=(root/'signals.jsonl').open('a')
        return obj
