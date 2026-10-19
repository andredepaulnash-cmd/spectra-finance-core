"""Default-off weekly paper wiring for cash-backed next-entry allocations.

This is an isolated caller integration, never an exchange executor. Frozen native
inventory and receipts remain authorities. The bridge is initialized before any
restart authorization. An unresolved funding saga blocks all new spending.
"""
from contextlib import ExitStack
from copy import deepcopy
from functools import wraps
from pathlib import Path
import hashlib
import sqlite3
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal as D, ROUND_DOWN
import json

from spectra_core.next_entry_allocator import Allocator, Policy, MembershipSnapshot, Member
from spectra_core.next_entry_psm_root_bridge import NativePSMRootFundingBridge
from spectra_core.next_entry_profit_bridge import ESCROW_POOL, ProfitBridgeBlocked
from .entry_admission import NativeEntryAdmission, AdmissionBlocked
from .owned_spot.adapter import OwnedSpotAdapter, encoded, per_call_snapshot
from .owned_spot.model import Blocked, CapitalBlocked, ZERO, ONE
from .owned_spot import native as N


class _WeeklyReadScope:
    """Memoize verified reads only inside one serialized, read-only adapter call.

    Connection-local data_version detects committed writes in all three stores.
    Exact detached state and clock comparisons also catch unpersisted changes.
    Source bytes are checked on each lookup; neither grants nor spending gates
    are cached. No connection or result survives the public call's finally block.
    """
    def __init__(self):
        self.stack = ExitStack()
        self.connections = {}
        self.values = {}
        self.token = None
        self.state = None

    def close(self):
        self.stack.close()

    def _token(self, adapter):
        bridge = adapter.profit_bridge
        versions = []
        for path in (bridge.paper_db, bridge.native_db, bridge.allocator.path):
            path = Path(path).resolve()
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            old = self.connections.get(path)
            if old is None or old[0] != identity:
                db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
                db.execute('PRAGMA query_only=ON')
                self.stack.callback(db.close)
                self.connections[path] = (identity, db)
            db = self.connections[path][1]
            versions.append((str(path), identity, db.execute('PRAGMA data_version').fetchone()[0]))
        # Use actual current bytes, never mtime-only or a persisted approval bit.
        root = Path(__file__).resolve().parents[2]
        sources = bridge._extension_identity()
        native = N.verify_sources()
        return (adapter.now, id(bridge), bridge.enabled, bridge.funding_enabled,
                bridge._identity, encoded(asdict(bridge.allocator.policy)),
                encoded(asdict(adapter.config)), tuple(versions), encoded(sources),
                encoded(native), hashlib.sha256((root/'research/weekly_reversal/owned_spot/native.py').read_bytes()).hexdigest())

    def read(self, adapter, key, reader):
        token = self._token(adapter)
        if token != self.token or adapter.state != self.state:
            self.values.clear()
            # Inherited receipts/lots/provenance caches share this same scope.
            inherited = getattr(adapter, '_read_snapshot', None)
            if inherited is not None:
                inherited.clear()
            self.token, self.state = token, deepcopy(adapter.state)
        if key not in self.values:
            value = reader()
            if self._token(adapter) == token and adapter.state == self.state:
                self.values[key] = deepcopy(value)
            return value
        return deepcopy(self.values[key])


def _weekly_read_only(function):
    """Extend the existing per-call snapshot, never a mutation/bar-wide cache."""
    snapshot_function = per_call_snapshot(function)
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        if getattr(self, '_weekly_read_scope', None) is not None:
            return snapshot_function(self, *args, **kwargs)
        scope = _WeeklyReadScope()
        self._weekly_read_scope = scope
        try:
            return snapshot_function(self, *args, **kwargs)
        finally:
            self._weekly_read_scope = None
            scope.close()
    return wrapped


class WeeklyCompoundingAdapter(OwnedSpotAdapter):
    def __init__(self, root, config, *, now, membership, enabled=False,
                 allocator_policy=None, fault_hook=None):
        if enabled is not True:
            raise Blocked('Weekly compounding is default-off; explicit research admission required')
        self.profit_bridge = None
        self._compounding_ready = False
        self.membership = membership
        if not isinstance(membership, MembershipSnapshot):
            raise Blocked('Explicit frozen equal-share membership required')
        expected = tuple(Member(p, config.owner + ':' + p, 'POSITION_ALLOCATION')
                         for p in sorted(config.rules)) + (Member('VCAP', config.owner + ':VCAP', 'VCAP'),)
        if (membership.account_id != config.account or membership.currency != 'USD'
                or sorted(membership.members,key=lambda m:m.allocation_id) != sorted(expected,key=lambda m:m.allocation_id)):
            raise Blocked('Membership must contain each frozen asset allocation plus one VCAP share')
        super().__init__(root,config,now=now,defer_recovery=True)
        try:
            identity = json.loads(encoded(asdict(membership)))
            old = self.state.get('weekly_compounding_membership')
            if old is not None and old != identity:
                raise Blocked('Compounding membership changed on restart')
            self.state['weekly_compounding_membership'] = identity
            self._save()
            # High precision records the native finite decimal realization without
            # rounding it down; cent-sized allocation shares retain exact carry.
            policy = allocator_policy or Policy(D('1e-30'),D('.01'),86400,3600)
            self.allocator = Allocator(self.root/'allocator.sqlite',account_id=config.account,currency='USD',policy=policy)
            self.entry_admission = NativeEntryAdmission(self,allocator=self.allocator,snapshot_id=membership.snapshot_id)
            self.profit_bridge = NativePSMRootFundingBridge(admission=self.entry_admission,funding_enabled=True,root_cash_enabled=True,
                paper_db=self.db_path,native_db=self.root/'native.sqlite',allocator=self.allocator,
                account_id=config.account,connector=config.connector,owner_id=config.owner,enabled=True,fault_hook=fault_hook)
            self._compounding_ready = True
            # Recover actual receipts first. Never authorize an unknown prepared
            # funding entry through the ordinary PREPARING shortcut.
            self._recover()
        except BaseException:
            self.close()
            raise

    def _reload(self):
        with self._db() as db:
            row=db.execute('SELECT value FROM state WHERE id=1').fetchone()
        if row is None: raise Blocked('Paper state disappeared')
        state=json.loads(row[0])
        if state['identity'] != self.state['identity'] or state['now'] > self.now:
            raise Blocked('Paper identity or clock changed outside serialized writer')
        self.state=state

    def _verified_read(self, key, reader):
        scope = getattr(self, '_weekly_read_scope', None)
        if scope is None or self.profit_bridge is None:
            return reader()
        return scope.read(self, key, reader)

    def _cash_projection(self):
        return self._verified_read('cash_projection', self.profit_bridge.cash_projection)

    def _psm_funding_cash(self, pair):
        return self._verified_read(('psm_funding_cash', pair), lambda: self.psm.funding_cash(pair))

    @_weekly_read_only
    def audit(self, *, require_marks=True):
        return super().audit(require_marks=require_marks)

    @_weekly_read_only
    def report(self):
        return super().report()

    @_weekly_read_only
    def repair_feasibility(self, pair, *, now, rung=None):
        return super().repair_feasibility(pair, now=now, rung=rung)

    def cash_pools(self):
        if self.profit_bridge is None:
            return super().cash_pools()
        return dict(self._cash_projection().cash_pools)

    def available_pool(self,pool):
        if self.profit_bridge is None:
            return super().available_pool(pool)
        return self._cash_projection().available_base_pools.get(pool,ZERO)

    def _before_spending(self):
        if not self._compounding_ready or self.state.get('weekly_recovery_required'):
            raise Blocked('Compounding recovery is not ready')
        try: return self.profit_bridge.before_new_spending()
        except ProfitBridgeBlocked as exc: raise Blocked('Pending profit/funding reconciliation: '+str(exc)) from exc

    def _pending(self,pair=None):
        # Prepared orders remain non-executable yet retain exclusive mutation
        # ownership. A final admitted proof excludes only its exact own candidate.
        statuses={'PREPARING','ENTRY_PREPARED','FUNDING_PREPARED','OPEN','PARTIAL','CANCEL_REQUESTED'}
        return [o for o in self.state['orders'].values() if o['status'] in statuses and (pair is None or o['pair']==pair)]

    def _hold(self,order):
        if order.get('status') in {'FUNDING_PREPARED','ENTRY_PREPARED'}:
            return ZERO  # No executable order exists; native commitment is separate.
        return super()._hold(order)

    def _dedicated_owner(self,pool):
        if not pool.startswith('next_entry:'):
            raise Blocked('Not a dedicated funding pool')
        entry=pool[len('next_entry:'):]
        with self.profit_bridge._paper() as db:
            row,proof=self.profit_bridge._funding_row(db,entry)
        order=self.state['orders'].get(row['order_id'])
        if (order is None or order.get('entry_id')!=entry or order['pool']!=pool
                or proof['entry_pool']!=pool or order['funding_owner'] not in {'seed','repair'}
                or proof['evidence']['order']['pair']!=order['pair']
                or proof['evidence']['order']['funding_owner']!=order['funding_owner']):
            raise Blocked('Dedicated cash lost exact asset/source lineage')
        return order['pair'],order['funding_owner']

    def funding_cash(self):
        if self.profit_bridge is None: return super().funding_cash()
        pools=self.cash_pools()
        result={'seed':pools.get('seed',ZERO),'repair':pools.get('repair',ZERO),
                'protected':pools.get('protected',ZERO),'unspent_profit':pools.get(ESCROW_POOL,ZERO)}
        for pool,amount in pools.items():
            if pool.startswith('next_entry:'):
                _,owner=self._dedicated_owner(pool);result[owner]+=amount
            elif pool.startswith('psm:'):
                amounts=self._psm_funding_cash(pool[len('psm:'):])
                for key,value in amounts.items():
                    owner=key.rsplit('|',1)[-1]
                    if owner not in {'seed','repair'}: raise Blocked('Unknown PSM funding lineage')
                    result[owner]+=value
            elif pool not in {'seed','repair','protected',ESCROW_POOL}:
                raise Blocked('Unclassified physical cash pool')
        if sum(result.values(),ZERO)!=sum(pools.values(),ZERO):
            raise Blocked('Source funding projection does not conserve physical cash')
        return result

    def recovery_psm_cash(self):
        if self.profit_bridge is None: return super().recovery_psm_cash()
        return sum((value for pair in self.state['psm']
                    for key,value in self._psm_funding_cash(pair).items()
                    if key.rsplit('|',1)[-1]=='repair'),ZERO)

    @per_call_snapshot
    def capital_commitments(self):
        pools=self.cash_pools()
        # Segregated cash already backs its pending order: count it once.
        ordinary=[o for o in self._pending() if not o['pool'].startswith(('psm:','next_entry:'))]
        total=self.deployed_basis()+sum((self._hold(o) for o in ordinary),ZERO)
        pairs={p:self.deployed_basis(p)+sum((self._hold(o) for o in ordinary if o['pair']==p),ZERO) for p in self.config.rules}
        recovery=self.recovery_basis()+sum((self._hold(o) for o in ordinary if o.get('funding_owner')=='repair'),ZERO)
        for pool,amount in pools.items():
            if pool.startswith('psm:'):
                pair=pool[len('psm:'):];total+=amount;pairs[pair]+=amount
            elif pool.startswith('next_entry:'):
                pair,owner=self._dedicated_owner(pool);total+=amount;pairs[pair]+=amount
                if owner=='repair': recovery+=amount
        psm_recovery=self.recovery_psm_cash();recovery+=psm_recovery
        return {'total':total,'pairs':pairs,'recovery':recovery,
                'native_recovery_principal':self.recovery_principal(),'segregated_recovery_psm_cash':psm_recovery}

    @_weekly_read_only
    def _budget_admission(self,pair,target,pool,funding_owner=None):
        if self.profit_bridge is None: return super()._budget_admission(pair,target,pool,funding_owner)
        self._before_spending()
        target=D(target)
        if target<=0: raise Blocked('Positive full budget required')
        required_base=target
        if pool=='repair':
            profit=self.available_profit('VCAP')
            if profit>=target and profit>0:
                raise Blocked('PROFIT_EXCEEDS_REPAIR_TARGET: unchanged full double requires a positive base portion')
            required_base=target-profit
        if self.available_pool(pool)<required_base: raise CapitalBlocked('OWNER_CASH','Owner cash plus exact available allocation cannot fund full target')
        if self.cash()-self.holds()-self._unsettled_sales()-target < self.config.protected_cash:
            raise CapitalBlocked('RESERVE','Full target breaches protected cash reserve')
        funding_owner=funding_owner or ('repair' if pool=='repair' else 'seed')
        claims=self.capital_commitments()
        increment=ZERO if pool.startswith(('psm:','next_entry:')) else target
        if claims['total']+increment>self.config.total_budget_cap: raise CapitalBlocked('TOTAL_CAP','Full target breaches total owned capital cap')
        if claims['pairs'][pair]+increment>self.config.pair_budget_cap: raise CapitalBlocked('PAIR_CAP','Full target breaches asset capital cap')
        if funding_owner=='repair' and claims['recovery']+increment>self.config.recovery_cap:
            raise CapitalBlocked('RECOVERY_CAP','Full target breaches recovery capital cap')

    def available_profit(self,allocation_id,*,raw=False):
        ledger=self.allocator.export_research_state()
        key=json.dumps([self.membership.snapshot_id,allocation_id],separators=(',',':'))
        atoms=sum(row['units'] for row in ledger['buckets'].get(key,[]))
        if not raw: atoms=(atoms//self.allocator.quantum)*self.allocator.quantum
        return D(self.allocator._amount(atoms))

    def sync_profit_cash(self,*,now):
        """Reconcile confirmed receipts; this method never creates a new entry."""
        self.advance(now);self._save()
        # Already staged exact operations can replay. Newly terminal PSM children
        # wait for their complete logical sale reconciliation before allocation.
        with self.profit_bridge._paper() as db:
            staged={row['trade_id']:row['status'] for row in db.execute('SELECT * FROM next_profit_outbox')}
        actions=[]
        for receipt in self._receipts(applied=True):
            order=self.state['orders'][receipt['order_id']]
            if order['side']!='SELL' or not order.get('terminal'): continue
            tid=receipt['trade_id']
            if staged.get(tid) in {'ACKNOWLEDGED','INELIGIBLE'}: continue
            if tid not in staged:
                if order['kind']=='PSM_EXIT':
                    meta=self.state['psm'].get(order['pair'])
                    if meta is None or meta.get('action') is not None: continue
                self.profit_bridge.stage(trade_id=tid,snapshot=self.membership,now=int(self.now))
                self._reload()
            self.profit_bridge.dispatch(trade_id=tid,now=int(self.now));self._reload()
            actions.append(tid)
        with self.profit_bridge._paper() as db:
            funded=[row['entry_id'] for row in db.execute("SELECT * FROM next_entry_funding WHERE status IN ('FUNDED','CLOSING')")]
        for entry in funded:
            self.profit_bridge.sync_entry(entry_id=entry,now=int(self.now));self._reload()
        return {'sale_receipts_checked':tuple(actions),'projection':self.profit_bridge.cash_projection(),
                'allocator':self.allocator.audit()}

    def _recover(self):
        if not self._compounding_ready:
            raise Blocked('Funding bridge must exist before restart recovery')
        # Existing native receipt recovery is retained. Funded prepared entries
        # are handled by explicit recovery below, never ordinary _authorize.
        super()._recover()
        for order in list(self.state['orders'].values()):
            if order['kind']=='REENTRY' and order.get('terminal') and order.get('activation'):
                self._reconcile_reentry_outcome(order['id'])
        prepared=[o['id'] for o in self.state['orders'].values()
                  if o['status'] in {'FUNDING_PREPARED','ENTRY_PREPARED'}]
        if prepared:
            self.state['weekly_recovery_required']=prepared
            self._save('weekly_prepared_recovery_required',{'orders':prepared})
        return prepared

    def refresh_accounting(self,pair,*,now):
        self.advance(now)
        results={}
        with self.entry_admission._native() as db:
            bound={row[0] for row in db.execute('SELECT order_id FROM economic_fill_bindings')}
        for position in self._asset(pair)['positions']:
            proposals=list(dict.fromkeys(o['proposal'] for o in self.state['orders'].values()
                if o['position']==position and o['status'] not in {'FUNDING_PREPARED','ENTRY_PREPARED'}
                and (o['status']!='REJECTED' or o['id'] in bound)))
            try:
                reports=[self._sync_proposal(p,position) for p in proposals]
                results[position]=self.health.record_reports(position_id=position,reports=reports,now=self.now)
            except Exception as exc:
                self.health.record_conflict(position_id=position,now=self.now,reason='weekly_native_sync_failed: '+str(exc))
                raise
        return results

    def _candidate_source(self,cohort):
        generation=cohort.get('generation',1)
        orders=[o for o in self.state['orders'].values() if o.get('cohort')==cohort['id']
                and o.get('cohort_generation',1)==generation and o['side']=='BUY'
                and o['intent']=='ENTRY' and o['kind'] in {'SEED','REENTRY'}
                and any(D(r['amount'])>ZERO for r in self._receipts(o['id'],applied=True))]
        sources={o['pool'] for o in orders}
        if len(sources)!=1: raise Blocked('No unique exact predecessor entry pool')
        return sources.pop()

    def _prepare_next_entry(self,*,pair,cid,kind,target,base,profit,source_pool,rung=None,repair_retry=None,source_owner=None,previous_generation=None):
        self._before_spending();self._assert_clean(pair)
        bar=self._bar(pair);cohort=self.state['cohorts'][cid]
        if bar.completed_at!=self.now: raise Blocked('Current completed decision boundary required')
        q=self.config.quantity_for_budget(pair,target,bar.close)
        oid=self._id('weekly-buy');generation=cohort.get('generation',1) if previous_generation is None else previous_generation
        active=self._current_position(pair)
        if kind=='REENTRY' and active not in self.ledger.list_active_position_ids():
            position=f'{self.config.owner}:{pair}:native:{len(self._asset(pair)["positions"])+1}'
        else: position=active
        allocation='VCAP' if kind=='REPAIR' else pair
        order={'id':oid,'entry_id':oid,'pair':pair,'position':position,'cohort':cid,'kind':kind,'side':'BUY',
            'pool':'next_entry:'+oid if profit>0 else source_pool,'source_pool':source_pool,'source_owner':deepcopy(source_owner),
            'funding_owner':'repair' if kind=='REPAIR' else 'seed',
            'status':'FUNDING_PREPARED' if profit>0 else 'ENTRY_PREPARED',
            'proposal':oid+':proposal','executor':oid+':executor','intent':'RECOVERY' if kind=='REPAIR' else 'ENTRY',
            'target':str(target),'principal':str(target/(ONE+self.config.fee_rate)),
            'base_budget':str(base),'profit_budget':str(profit),'quantity':str(q),'reference':str(bar.close),
            'decision_quantity':str(q),'decision_reference':str(bar.close),'decision_at':self.now,
            'created_at':self.now,'decision_bar':bar.completed_at,'capacity':str(bar.quote_volume*self.config.participation),
            'terminal':None,'execution_frozen':False,'cohort_generation':generation+(kind=='REENTRY'),
            'previous_cohort_generation':generation if kind=='REENTRY' else None,'repair_rung':rung,
            'repair_retry':repair_retry,
            'snapshot_id':self.membership.snapshot_id,'allocation_id':allocation,
            'allocation_owner_id':self.config.owner+':'+allocation,
            'allocation_role':'VCAP' if kind=='REPAIR' else 'POSITION_ALLOCATION'}
        self.state['orders'][oid]=order;self._save('weekly_nonexecutable_entry',{'order':oid})
        preview=self.entry_admission.preview(oid,now=int(self.now),base_only=profit==0)
        if not preview['eligible_except_native']:
            order['status']='REJECTED';order['terminal']='REJECTED';order['admission_rejection']=preview
            self._save('weekly_entry_blocked',{'order':oid,'reason':preview})
            raise AdmissionBlocked(preview['reason_code'],preview.get('reason','Entry preview blocked'))
        proposal=N.models.CapitalProposal(order['proposal'],pair,N.models.IntentKind(order['intent']),D(order['principal']))
        with N.paper_time(self.now):
            decision=self.governor.reserve(proposal,self._snapshot(pair,order))
            if not decision.approved:
                order['status']='REJECTED';order['terminal']='REJECTED';self._save('weekly_native_rejection',{'order':oid,'reason':decision.reason})
                raise Blocked('Native governor rejected full entry: '+decision.reason)
            if not self.governor.commit_if_matches(proposal):
                raise Blocked('Exact native reservation could not commit')
        # From this boundary failures retain protected authority for explicit
        # reconciliation. There is no unsafe generic rollback or retry refund.
        if profit>0:
            proof=self.profit_bridge.prepare_entry(order_id=oid,now=int(self.now));self._reload()
            self.profit_bridge.fund_entry(entry_id=oid,now=int(self.now));self._reload()
            self.entry_admission.validate(proof,self.state['orders'][oid],now=int(self.now),replay=True)
        else:
            proof=self.entry_admission.prove_base_entry(oid,now=int(self.now))
            self.entry_admission.validate_base_entry(proof,self.state['orders'][oid],now=int(self.now))
            self.state['orders'][oid]['base_entry_proof']=proof;self._save('base_entry_admitted',{'order':oid,'proof':proof['proof_sha256']})
        return self._activate_entry(oid,proof)

    def _activate_entry(self,order_id,proof):
        order=self.state['orders'][order_id]
        if order['status'] not in {'FUNDING_PREPARED','ENTRY_PREPARED'}:
            raise Blocked('Entry activation requires original non-executable state')
        admitted=proof['permit'] if proof.get('permit') else proof['base_entry']
        if self.now>admitted['expires_at']:
            raise Blocked('Prepared entry authorization expired')
        pair=order['pair'];cohort=self.state['cohorts'][order['cohort']]
        bar=self._bar(pair);target=D(order['target'])
        # Native position creation is idempotent; the source successor is fixed
        # before funding and cannot create a duplicate active same-asset position.
        new_position=order['position'] not in self._asset(pair)['positions']
        if new_position:
            self.ledger.open_position(position_id=order['position'],pair=pair,created_at=self.now)
        activation=order.get('activation')
        if activation is None:
            if order['kind']=='REENTRY':
                if cohort['status']!='SOLD' or self._cohort_lots(cohort): raise Blocked('Reentry slot is not empty')
                cohort.setdefault('generation_history',[]).append({k:cohort.get(k) for k in
                    ('generation','anchor','acquired_at','last_rung','rung','status','profit_state','lot_ids')})
                cohort.update(generation=order['cohort_generation'],anchor=None,acquired_at=None,last_rung=0,rung=None,status='OWNED',profit_state=None,lot_ids=[])
            else:
                asset=self._asset(pair)
                if asset['selected'] not in {None,cohort['id']}: raise Blocked('Another selected partial owns repair')
                asset['selected']=cohort['id'];asset['last_repair_bar']=bar.completed_at;asset['repair_block']=None
                if order.get('repair_retry'):
                    original=proof['evidence']['cohort']['rung']
                    if cohort['rung']!=original: raise Blocked('Exact prior incomplete rung changed before activation')
                    cohort['rung']['orders'].append(order_id)
                    cohort['rung'].update(status='PENDING',quantity=order['decision_quantity'],reference=order['decision_reference'])
                else:
                    cohort['rung']={'level':order['repair_rung'],'requested_target':order['target'],
                        'start_basis':order['target'],'actual_added':'0','shortfall':order['target'],'status':'PENDING',
                        'decision_bar':bar.completed_at,'quantity':order['decision_quantity'],
                        'reference':order['decision_reference'],'orders':[order_id]}
            if order['position'] not in self._asset(pair)['positions']:
                self._asset(pair)['positions'].append(order['position'])
            key=f'{pair}:{bar.completed_at}';budgets=self.state.setdefault('liquidity',{})
            used=D(budgets.get(key,'0'))
            if used+target>bar.quote_volume*self.config.participation:
                raise Blocked('Admitted liquidity changed before entry activation')
            budgets[key]=str(used+target)
            order['activation']={'at':self.now,'proof_sha256':proof['proof_sha256'],'liquidity_key':key,'liquidity_debit':str(target)}
            order['status']='PREPARING'
            self._save('weekly_entry_activation',{'order':order_id,'activation':order['activation']})
        elif activation['proof_sha256']!=proof['proof_sha256']:
            raise Blocked('Conflicting entry activation proof')
        self._authorize(order)
        # A newly active successor needs genuine native proposal health before
        # the next asset's whole-account guard. Existing positions are unchanged.
        if new_position: self.refresh_accounting(pair,now=self.now)
        return dict(order)

    def request_reentry(self,cohort_id,*,now):
        self.advance(now);self._save()
        c=self.state['cohorts'][cohort_id];pair=c['pair']
        source=self.entry_admission.root_reentry_source(cohort_id,now=int(self.now))
        profit=self.available_profit(pair);base=D(source['base_budget'])
        return self._prepare_next_entry(pair=pair,cid=cohort_id,kind='REENTRY',target=base+profit,
            base=base,profit=profit,source_pool=source['source_pool'],source_owner=source['source_owner'],
            previous_generation=source['latest_generation'])

    def request_repair(self,pair,*,rung,now):
        self.advance(now);profit=self.available_profit('VCAP')
        if profit==0: return super().request_repair(pair,rung=rung,now=now)
        self._before_spending()
        evidence=super().repair_feasibility(pair,now=now,rung=rung)
        if not evidence['eligible'] and evidence['reason_code']!='OWNER_CASH':
            if evidence['capital_blocked']:
                self._asset(pair)['repair_block']=evidence;self._save('repair_capital_block',evidence)
                raise CapitalBlocked(evidence['reason_code'],evidence['reason'])
            raise Blocked(evidence['reason'])
        target=D(evidence['target']);base=target-profit
        if base<=0: raise AdmissionBlocked('PROFIT_EXCEEDS_REPAIR_TARGET','Entire VCAP entitlement leaves no positive base for unchanged full double')
        return self._prepare_next_entry(pair=pair,cid=evidence['cohort_id'],kind='REPAIR',target=target,
            base=base,profit=profit,source_pool='repair',rung=rung)

    def repair_retry_feasibility(self,pair,*,now):
        self.advance(now)
        return self.entry_admission.repair_retry_feasibility(pair,now=int(self.now))

    def retry_repair(self,pair,*,now):
        """Admit only the original rung's exact unfilled remainder."""
        self.advance(now);self._before_spending()
        evidence=self.repair_retry_feasibility(pair,now=now)
        if not evidence['eligible']:
            raise AdmissionBlocked(evidence['reason_code'],evidence.get('reason','Same-target retry blocked'))
        # Keep the original native profit lifecycle checkpoint, after diagnostics
        # and before the independently verified non-executable entry candidate.
        self.selected_cohort_cycle(pair,now=now)
        return self._prepare_next_entry(pair=pair,cid=evidence['cohort_id'],kind='REPAIR',
            target=D(evidence['target']),base=D(evidence['base_budget']),profit=D(evidence['profit_budget']),
            source_pool='repair',rung=evidence['next_rung'],repair_retry=evidence['repair_retry'])

    def reconcile_terminal(self,order_id,status,*,now):
        super().reconcile_terminal(order_id,status,now=now)
        order=self.state['orders'][order_id]
        if order['kind']=='REENTRY' and order.get('activation'):
            self._reconcile_reentry_outcome(order_id)

    def _reconcile_reentry_outcome(self,order_id):
        """Finalize one real entry attempt without turning an empty buy into ownership.

        The activated generation is provisional until there is an acquisition.
        A zero-fill terminal attempt restores its exact previously sold cohort;
        the failed order, native position, activation and audit remain immutable.
        Any actual fill instead keeps the generation and establishes its anchor.
        """
        order=self.state['orders'][order_id]
        if (order['kind']!='REENTRY' or not order.get('activation')
                or order.get('terminal') not in {'FILLED','CANCELED','REJECTED'}
                or order['status']!=order['terminal']):
            raise Blocked('Exact activated terminal reentry required')
        with self._db() as db:
            terminal=[{'at':row[0],'payload':row[1]} for row in db.execute("SELECT at,payload FROM audit_events WHERE kind='terminal_reconciliation'")
                      if json.loads(row[1]).get('order')==order_id]
        if len(terminal)!=1 or json.loads(terminal[0]['payload'])!={'order':order_id,'status':order['terminal']}:
            raise Blocked('Exact original reentry terminal audit required')
        at=terminal[0]['at']
        if D(order['profit_budget'])>0:
            with self.profit_bridge._paper() as db:
                _,proof=self.profit_bridge._funding_row(db,order['entry_id'])
            self.entry_admission.validate(proof,order,now=int(self.now),replay=True)
        else:
            proof=order.get('base_entry_proof')
            self.entry_admission.validate_base_entry(proof,order,now=int(self.now),replay=True)
        rows=self._receipts(order_id,applied=True)
        if rows!=self._receipts(order_id):
            raise Blocked('Unapplied reentry receipt prevents terminal slot transition')
        units=sum((D(r['amount']) for r in rows),ZERO)
        gross=sum((D(r['amount'])*D(r['price']) for r in rows),ZERO)
        fee=sum((D(r['fee_paid']) for r in rows),ZERO)
        lot_ids=['trade-fill:'+r['trade_id'] for r in rows]
        with self.entry_admission._native() as native:
            native_ids={row[0] for row in native.execute('SELECT lot_id FROM economic_lots WHERE proposal_id=? OR executor_id=?',
                                                        (order['proposal'],order['executor']))}
            native_fills={row[0] for row in native.execute('SELECT trade_id FROM economic_trade_fill_reconciliation WHERE order_id=?',(order_id,))}
        if native_ids!=set(lot_ids) or native_fills!={r['trade_id'] for r in rows}:
            raise Blocked('Reentry terminal paper/native acquisition coverage differs')
        for row,lid in zip(rows,lot_ids):
            lot=self.ledger.get_lot(lid)
            if (lot.proposal_id!=order['proposal'] or lot.executor_id!=order['executor']
                    or lot.pair!=order['pair'] or lot.position_id!=order['position']
                    or lot.base_original!=D(row['amount'])
                    or lot.cost_basis_original_quote!=D(row['amount'])*D(row['price'])
                    or lot.entry_fees_original_quote!=D(row['fee_paid'])):
                raise Blocked('Reentry terminal original native lot differs from actual receipt')
        if units<ZERO or order['terminal']=='FILLED' and units<=ZERO or order['terminal']=='REJECTED' and units:
            raise Blocked('Terminal reentry status conflicts with actual acquired quantity')
        anchor=str(gross/units) if units else None
        acquired=max(datetime.fromisoformat(r['timestamp']).timestamp() for r in rows) if units else None
        outcome={'outcome':'ACQUIRED' if units else 'EMPTY_RESTORED','status':order['terminal'],
            'at':at,'proof_sha256':proof['proof_sha256'],'generation':order['cohort_generation'],
            'quantity':str(units),'gross':str(gross),'fee':str(fee),'lot_ids':lot_ids,
            'anchor':anchor,'acquired_at':acquired}
        old=order.get('reentry_terminal_outcome')
        with self._db() as db:
            anchors=[json.loads(row[0]) for row in db.execute("SELECT payload FROM audit_events WHERE kind='reentry_terminal_outcome'")
                     if json.loads(row[0]).get('order')==order_id]
        if old is not None:
            if old!=outcome or anchors!=[{'order':order_id,'outcome':outcome}]:
                raise Blocked('Original reentry terminal outcome changed')
            return  # A later sale or generation must never be rewound by replay.
        if anchors:
            raise Blocked('Orphan reentry terminal outcome audit')
        # Reconcile the formerly OPEN native executor to terminal before restoring
        # slot availability; exact bound REJECTED orders remain in native health.
        self._sync_proposal(order['proposal'],order['position'])
        self.refresh_accounting(order['pair'],now=self.now)
        prior=deepcopy(proof['evidence']['cohort'])
        cohort=self.state['cohorts'][order['cohort']]
        expected=deepcopy(prior)
        fields=('generation','anchor','acquired_at','last_rung','rung','status','profit_state','lot_ids')
        expected.setdefault('generation_history',[]).append({key:prior.get(key) for key in fields})
        expected.update(generation=order['cohort_generation'],anchor=None,acquired_at=None,
                        last_rung=0,rung=None,status='OWNED',profit_state=None,lot_ids=lot_ids)
        if cohort!=expected or prior['status']!='SOLD':
            raise Blocked('Reentry cohort changed before exact terminal transition')
        if units:
            cohort['anchor']=anchor;cohort['acquired_at']=acquired
            cohort.setdefault('anchor_history',[]).append({'generation':cohort['generation'],'price':anchor,
                'receipt_orders':[order_id],'at':at,'acquired_at':acquired})
        else:
            if self._cohort_lots(cohort):
                raise Blocked('Zero-fill reentry still owns native inventory')
            self.state['cohorts'][order['cohort']]=prior
        order['reentry_terminal_outcome']=outcome
        self._save('reentry_terminal_outcome',{'order':order_id,'outcome':outcome})

    def _authorize(self,order):
        if order.get('entry_id'):
            activation=order.get('activation')
            if order['status']!='PREPARING' or activation is None:
                raise Blocked('Next-entry executor binding requires atomic admitted activation')
            if D(order['profit_budget'])>0:
                with self.profit_bridge._paper() as db:
                    row,proof=self.profit_bridge._funding_row(db,order['entry_id'])
                    if row['status']!='FUNDED': raise Blocked('Entry funding is not durably acknowledged')
                self.entry_admission.validate_activation(proof,order,now=int(self.now))
                approved=proof['permit']
            else:
                proof=order.get('base_entry_proof')
                if proof is None: raise Blocked('Base entry lacks original admitted proof')
                self.entry_admission.validate_activation(proof,order,now=int(self.now))
                approved=proof['base_entry']
            if self.now>approved['expires_at'] or activation['proof_sha256']!=proof['proof_sha256']:
                raise Blocked('Original entry authority expired or changed before executor binding')
            with self._db() as db:
                records=[json.loads(r[0]) for r in db.execute("SELECT payload FROM audit_events WHERE kind='weekly_entry_activation'")
                         if json.loads(r[0]).get('order')==order['id']]
            if records!=[{'order':order['id'],'activation':activation}]:
                raise Blocked('Exact atomic activation audit is missing or duplicated')
            cohort=self.state['cohorts'][order['cohort']]
            if cohort.get('generation',1)!=order['cohort_generation']:
                raise Blocked('Activated cohort generation changed')
            key=activation['liquidity_key']
            if D(self.state.get('liquidity',{}).get(key,'0'))<D(activation['liquidity_debit']):
                raise Blocked('Activated liquidity debit is missing')
        return super()._authorize(order)
