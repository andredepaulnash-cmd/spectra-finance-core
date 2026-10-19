"""New weekly owner/cohort coordinator over pinned PR40 native primitives.

Paper receipts are the physical-cash authority; native lots are inventory authority.
No network, exchange client, historical data loader or registry writer exists here.
"""
from contextlib import contextmanager
from functools import wraps
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP, ROUND_DOWN
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3

from . import native as N
from .model import AdapterConfig, Blocked, CapitalBlocked, CompletedBar, ObservedOpen, D, ZERO, ONE, BPS, decimal, floor_step, timestamp


def serial(value):
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, dict): return {k:serial(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)): return [serial(v) for v in value]
    return value


def encoded(value):
    return json.dumps(serial(value), sort_keys=True, separators=(',',':'), allow_nan=False)


def iso(now):
    return datetime.fromtimestamp(now,tz=timezone.utc).isoformat()


def per_call_snapshot(function):
    """Reuse real read results only within one serial read-only public call.

    This never caches accounting health across calls/bars or replaces native APIs.
    No inventory/cash mutation is allowed in a decorated body.
    """
    @wraps(function)
    def wrapped(self,*args,**kwargs):
        outer=getattr(self,'_read_snapshot',None)
        if outer is not None: return function(self,*args,**kwargs)
        self._read_snapshot={}
        try: return function(self,*args,**kwargs)
        finally: self._read_snapshot=None
    return wrapped


class OwnedSpotAdapter:
    def __init__(self, root, config: AdapterConfig, *, now, defer_recovery=False):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock = (self.root/'writer.lock').open('a+')
        try: fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise Blocked('Another writer owns this account')
        self.config = config
        self.now = timestamp(now)
        self.db_path = self.root/'paper.sqlite'
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, order_id TEXT NOT NULL, payload TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS audit_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            ''')
            row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
        identity = {'config':serial(asdict(config)),'sources':N.verify_sources(),'schema':1}
        if row:
            self.state=json.loads(row[0])
            if self.state['identity'] != identity:
                self.close(); raise Blocked('Configuration or native identities changed on restart')
            if self.now < self.state['now']:
                self.close(); raise Blocked('Paper clock cannot go backward on restart')
        else:
            self.state={'identity':identity,'now':self.now,'assets':{},'cohorts':{},'orders':{},'marks':{},'counter':0,'breaker':'NORMAL','quarantine':[],'psm':{}}
            self._save()
        db=self.root/'native.sqlite'
        policy=N.models.CapitalPolicy(config.protected_cash,config.total_budget_cap,config.pair_budget_cap,config.pair_budget_cap,config.recovery_cap)
        with N.paper_time(self.now): self.governor=N.capital_governor.CapitalGovernor(db,policy)
        self.ledger=N.economic_position.EconomicPositionLedger(db)
        self.reconciler=N.fill_reconciliation.LiveFillLedgerReconciler(ledger=self.ledger,governor=self.governor)
        self.health=N.accounting_health.AccountingHealthSupervisor(db)
        self.lifecycle=N.vcap_state.VCAPStateStore(db)
        if not defer_recovery:
            self._recover()

    def _db(self):
        db=sqlite3.connect(self.db_path)
        db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=FULL')
        return db

    def close(self):
        if getattr(self,'lock',None) and not self.lock.closed:
            fcntl.flock(self.lock,fcntl.LOCK_UN); self.lock.close()

    def __enter__(self): return self
    def __exit__(self,*args): self.close()

    def _save(self,kind=None,payload=None):
        self.state['now']=self.now
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO state VALUES (1,?)',(encoded(self.state),))
            if kind: db.execute('INSERT INTO audit_events(at,kind,payload) VALUES (?,?,?)',(self.now,kind,encoded(payload or {})))

    def advance(self,now):
        value=timestamp(now)
        if value < self.now: raise Blocked('Paper clock cannot move backward')
        self.now=value

    def _id(self,prefix):
        self.state['counter']+=1
        self._save()
        return f'{prefix}:{self.state["counter"]:08d}'

    def _asset(self,pair):
        if pair not in self.config.rules: raise Blocked('Asset outside frozen weekly scope')
        if pair not in self.state['assets']: raise Blocked('Asset has not been seeded')
        return self.state['assets'][pair]

    def _current_position(self,pair): return self._asset(pair)['positions'][-1]

    def _receipts(self,order_id=None,applied=None):
        cache=getattr(self,'_read_snapshot',None)
        if cache is not None:
            if 'receipts' not in cache:
                with self._db() as db: cache['receipts']=[(json.loads(row[0]),bool(row[1])) for row in db.execute('SELECT payload,applied FROM receipts ORDER BY rowid')]
            return [row for row,done in cache['receipts'] if (order_id is None or row['order_id']==order_id) and (applied is None or done==applied)]
        query='SELECT payload FROM receipts WHERE 1=1'; args=[]
        if order_id is not None: query+=' AND order_id=?'; args.append(order_id)
        if applied is not None: query+=' AND applied=?'; args.append(int(applied))
        query+=' ORDER BY rowid'
        with self._db() as db: return [json.loads(r[0]) for r in db.execute(query,args)]

    def _totals(self,order):
        fills=self._receipts(order['id'])
        q=sum((D(f['amount']) for f in fills),ZERO)
        gross=sum((D(f['amount'])*D(f['price']) for f in fills),ZERO)
        fee=sum((D(f['fee_paid']) for f in fills),ZERO)
        return q,gross,fee

    def cash_pools(self):
        pools={'protected':self.config.protected_cash,'seed':D('1000'),'repair':D('500')}
        for f in self._receipts():
            order=self.state['orders'][f['order_id']]
            gross=D(f['amount'])*D(f['price']); fee=D(f['fee_paid'])
            pool=order['pool']
            pools.setdefault(pool,ZERO)
            pools[pool]+= gross-fee if order['side']=='SELL' else -(gross+fee)
        return pools

    def cash(self): return sum(self.cash_pools().values(),ZERO)

    def _pending(self,pair=None):
        return [o for o in self.state['orders'].values() if o['status'] in {'PREPARING','OPEN','PARTIAL','CANCEL_REQUESTED'} and (pair is None or o['pair']==pair)]

    def _hold(self,order):
        if order not in self._pending() or order['side']!='BUY': return ZERO
        _,gross,fee=self._totals(order)
        return max(ZERO,D(order['target'])-gross-fee)

    def holds(self,pool=None):
        return sum((self._hold(o) for o in self._pending() if pool is None or o['pool']==pool),ZERO)

    def _unsettled_sales(self,pool=None):
        return sum((self._totals(o)[1]-self._totals(o)[2] for o in self._pending() if o['side']=='SELL' and (pool is None or o['pool']==pool)),ZERO)

    def available_pool(self,pool):
        return self.cash_pools().get(pool,ZERO)-self.holds(pool)-self._unsettled_sales(pool)

    def _lots(self,pair=None):
        cache=getattr(self,'_read_snapshot',None)
        if cache is not None:
            if 'lots' not in cache: cache['lots']=tuple(l for pid in self.ledger.list_active_position_ids() for l in self.ledger.list_active_lots(pid))
            return tuple(l for l in cache['lots'] if pair is None or l.pair==pair)
        return tuple(l for pid in self.ledger.list_active_position_ids() for l in self.ledger.list_active_lots(pid) if pair is None or l.pair==pair)

    def _provenance(self,proposal):
        cache=getattr(self,'_read_snapshot',None)
        if cache is None: return self.governor.get_execution_provenance(proposal)
        provenance=cache.setdefault('provenance',{})
        if proposal not in provenance: provenance[proposal]=self.governor.get_execution_provenance(proposal)
        return provenance[proposal]

    def deployed_basis(self,pair=None):
        return sum((l.cost_basis_remaining_quote+l.entry_fees_remaining_quote for l in self._lots(pair)),ZERO)

    def _cohort_lots(self,cohort):
        ids=set(cohort['lot_ids'])
        return tuple(l for l in self._lots(cohort['pair']) if l.lot_id in ids)

    def cohort_basis(self,cohort_id):
        return sum((l.cost_basis_remaining_quote+l.entry_fees_remaining_quote for l in self._cohort_lots(self.state['cohorts'][cohort_id])),ZERO)

    def _assert_clean(self,pair=None,*,health=True):
        if self.state['quarantine']: raise Blocked('Account reconciliation quarantine: '+str(self.state['quarantine']))
        if self.state['breaker']=='HALTED': raise Blocked('Accounting/global breaker HALTED')
        if self._receipts(applied=False): raise Blocked('Unapplied paper receipts require recovery')
        self.audit(require_marks=False)
        if health and pair:
            for position in self.ledger.list_active_position_ids():
                decision=self.health.assess(position_id=position,now=self.now)
                if not decision.allows_live_mutation(now=self.now): raise Blocked('Native whole-account accounting health is missing, stale or unresolved')

    def _native_rule(self,pair):
        rule=self.config.rules[pair]
        return N.kraken_market_fabric.KrakenTradingRule(trading_pair=pair,min_order_size=rule.min_base,min_price_increment=rule.price_tick,min_base_amount_increment=rule.base_step,min_notional_size=rule.min_quote,min_order_value=rule.min_quote,min_quote_amount_increment=self.config.fee_quantum,supports_limit_orders=True,supports_market_orders=True)

    def observe(self,bar:CompletedBar,*,now):
        self.advance(now)
        if bar.pair not in self.config.rules: raise Blocked('Unsupported pair')
        bar.validate_at(self.now,self.config.max_mark_age_seconds)
        old=self.state['marks'].get(bar.pair)
        if old and bar.completed_at<=old['completed_at']: raise Blocked('Completed-bar times must strictly increase')
        self.state['marks'][bar.pair]=serial(asdict(bar))
        history=self.state.setdefault('volume_history',{}).setdefault(bar.pair,[])
        history.append([bar.completed_at,str(bar.quote_volume)])
        history[:]=[item for item in history if item[0]>bar.completed_at-86400]
        observation=N.asset_fingerprint.AssetObservation(trading_pair=bar.pair,timestamp=float(bar.completed_at),price=float(bar.close),quote_volume_24h=float(sum((D(item[1]) for item in history),ZERO)))
        fingerprints=self.state.setdefault('fingerprints',{})
        previous=fingerprints.get(bar.pair)
        fingerprint=N.asset_fingerprint.AssetFingerprint.from_dict(previous).update(observation) if previous else N.asset_fingerprint.AssetFingerprint.cold_start(observation)
        fingerprints[bar.pair]=fingerprint.to_dict()
        self._save('completed_bar',{'pair':bar.pair,'completed_at':bar.completed_at})

    def _bar(self,pair):
        raw=self.state['marks'].get(pair)
        if not raw: raise Blocked('No completed causal bar')
        bar=CompletedBar(**{**raw,'close':D(raw['close']),'quote_volume':D(raw['quote_volume'])})
        bar.validate_at(self.now,self.config.max_mark_age_seconds)
        return bar

    def seed_complete(self):
        if self.state.get('seed_completion_receipt'): return True
        if set(self.state['assets'])!=set(self.config.rules): return False
        for pair in self.config.rules:
            cohorts=[c for c in self.state['cohorts'].values() if c['pair']==pair and c.get('origin')=='SEED']
            if len(cohorts)!=self.config.seed_count: return False
            for c in cohorts:
                orders=[o for o in self.state['orders'].values() if o['cohort']==c['id'] and o['kind']=='SEED']
                if not orders or any(o['terminal'] is None for o in orders) or orders[-1]['terminal']!='FILLED': return False
        self.state['seed_completion_receipt']={'at':self.now,'orders':[o['id'] for o in self.state['orders'].values() if o['kind']=='SEED'],'cohorts':[c['id'] for c in self.state['cohorts'].values() if c.get('origin')=='SEED']}
        self._save('seed_completion_barrier',self.state['seed_completion_receipt'])
        return True

    def prepare_seed_asset(self,pair,*,now):
        """Admit one immutable configured-ticket batch from a prior completed bar.

        All tickets share one liquidity budget. Caller supplies the same later
        observed open for every arm; no strategy mutation before initialization.
        """
        self.advance(now); self._assert_clean(health=False)
        if pair in self.state['assets']: return self.resume_seed_asset(pair,now=self.now)
        bar=self._bar(pair)
        total=self.config.seed_ticket*self.config.seed_count
        self._budget_admission(pair,total,'seed')
        self._admit_liquidity(pair,total,bar)
        position=f'{self.config.owner}:{pair}:native:1'
        self.ledger.open_position(position_id=position,pair=pair,created_at=self.now)
        self.state['assets'][pair]={'positions':[position],'selected':None,'last_native_decision':None,'last_repair_bar':None,'seed_batch':{'decision_at':self.now,'completed_bar':bar.completed_at,'reference':str(bar.close),'expected_count':self.config.seed_count,'ticket':str(self.config.seed_ticket)}}
        self._save('seed_begin',{'pair':pair,'completed_bar':bar.completed_at})
        result=[]
        for index in range(self.config.seed_count):
            cid=f'{pair}:seed:{index+1:02d}'
            self.state['cohorts'][cid]={'id':cid,'pair':pair,'ordinal':index+1,'generation':1,'origin':'SEED','anchor':None,'anchor_history':[],'lot_ids':[],'last_rung':0,'rung':None,'status':'OWNED','profit_state':None}
            quantity=self.config.quantity_for_budget(pair,self.config.seed_ticket,bar.close)
            order=self._new_buy(pair,cid,'SEED','seed',self.config.seed_ticket,bar.close,quantity,decision_at=self.now,capacity=None)
            order['decision_bar']=bar.completed_at; self._save()
            result.append(dict(order))
        return result

    def _fingerprint(self,pair):
        raw=self.state.get('fingerprints',{}).get(pair)
        if not raw: raise Blocked('Missing causal native fingerprint')
        return N.asset_fingerprint.AssetFingerprint.from_dict(raw)

    @per_call_snapshot
    def _budget_admission(self,pair,target,pool,funding_owner=None):
        target=decimal(target,positive=True)
        if self.available_pool(pool)<target: raise CapitalBlocked('OWNER_CASH','Owner cash insufficient for full requested target')
        if self.cash()-self.holds()-self._unsettled_sales()-target < self.config.protected_cash:
            raise CapitalBlocked('RESERVE','Full target breaches protected cash reserve')
        psmcash=sum((v for k,v in self.cash_pools().items() if k.startswith('psm:')),ZERO)
        allholds=sum((self._hold(o) for o in self._pending() if not o['pool'].startswith('psm:')),ZERO)
        pairholds=sum((self._hold(o) for o in self._pending(pair) if not o['pool'].startswith('psm:')),ZERO)
        increment=ZERO if pool.startswith('psm:') else target
        if self.deployed_basis()+psmcash+allholds+increment > self.config.total_budget_cap: raise CapitalBlocked('TOTAL_CAP','Full target breaches total deployed budget cap')
        if self.deployed_basis(pair)+self.cash_pools().get('psm:'+pair,ZERO)+pairholds+increment>self.config.pair_budget_cap: raise CapitalBlocked('PAIR_CAP','Full target breaches per-asset deployed budget cap')
        funding_owner=funding_owner or ('repair' if pool=='repair' else 'seed')
        if funding_owner=='repair':
            recovery_holds=sum((self._hold(o) for o in self._pending() if o.get('funding_owner')=='repair' and not o['pool'].startswith('psm:')),ZERO)
            recovery_increment=ZERO if pool.startswith('psm:') else target
            if self.recovery_basis()+self.recovery_psm_cash()+recovery_holds+recovery_increment>self.config.recovery_cap:
                raise CapitalBlocked('RECOVERY_CAP','Full target breaches fee-inclusive repair-owner recovery cap')

    def _snapshot(self,pair,new_order):
        # Native holds are principal only. Adapter holds remain authoritative after
        # RECONCILED receipts clear native protection, including all fee reserves.
        active_native=self.governor.protected_quote()
        other=[o for o in self._pending() if o['id']!=new_order['id']]
        shadow_holds=sum((self._hold(o) for o in other),ZERO)
        _,filled,fees=self._totals(new_order)
        current_fee=max(ZERO,D(new_order['target'])-D(new_order['principal']))
        principal=sum((l.cost_basis_remaining_quote for l in self._lots()),ZERO)
        pairprincipal=sum((l.cost_basis_remaining_quote for l in self._lots(pair)),ZERO)
        # Calls are serialized and all prior native buys are already reconciled;
        # PREPARING recovery is completed before another admission.
        if active_native: raise Blocked('Unresolved native reservation blocks new admission')
        return N.models.CapitalSnapshot(free_quote=self.cash()-shadow_holds-self._unsettled_sales()-current_fee,committed_quote=principal+shadow_holds,pair_exposure_quote=pairprincipal+sum((self._hold(o) for o in other if o['pair']==pair),ZERO),position_exposure_quote=pairprincipal+sum((self._hold(o) for o in other if o['pair']==pair),ZERO),recovery_exposure_quote=self.recovery_principal()+sum((self._hold(o)/(ONE+self.config.fee_rate) for o in other if o.get('funding_owner')=='repair'),ZERO))

    def _new_buy(self,pair,cohort,kind,pool,target,price,quantity,*,decision_at,capacity,funding_owner=None,position_id=None):
        funding_owner=funding_owner or ('repair' if kind=='REPAIR' else 'seed')
        if funding_owner not in {'seed','repair'}: raise Blocked('Unknown acquisition funding owner')
        self._budget_admission(pair,target,pool,funding_owner)
        rule=self.config.rules[pair]
        if quantity<=0 or quantity<rule.min_base or quantity*price<rule.min_quote: raise Blocked('Rounded buy below configured minimum')
        if capacity is not None and target>capacity: raise Blocked('Full target exceeds prior-completed-bar liquidity; no clipping')
        oid=self._id('paper-buy')
        intent='RECOVERY' if funding_owner=='repair' else 'ENTRY'
        order={'id':oid,'pair':pair,'position':position_id or self._current_position(pair),'cohort':cohort,'kind':kind,'side':'BUY','pool':pool,'funding_owner':funding_owner,'status':'PREPARING','proposal':oid+':proposal','executor':oid+':executor','intent':intent,'target':str(target),'principal':str(target/(ONE+self.config.fee_rate)),'quantity':str(quantity),'reference':str(price),'decision_at':decision_at,'created_at':decision_at,'capacity':str(capacity) if capacity is not None else None,'terminal':None,'execution_frozen':False}
        order['decision_quantity']=str(quantity)
        order['decision_reference']=str(price)
        order['cohort_generation']=self.state['cohorts'][cohort].get('generation',1)
        if kind=='REPAIR': order['repair_rung']=self.state['cohorts'][cohort]['rung']['level']
        self.state['orders'][oid]=order; self._save('buy_intent',order)
        self._authorize(order)
        return order

    def _authorize(self,order):
        if order['side']!='BUY': return
        proposal=N.models.CapitalProposal(order['proposal'],order['pair'],N.models.IntentKind(order['intent']),D(order['principal']))
        with N.paper_time(self.now):
            decision=self.governor.get_decision(order['proposal'])
            if decision is None: decision=self.governor.reserve(proposal,self._snapshot(order['pair'],order))
            if decision.status==N.models.DecisionStatus.RESERVED:
                if not self.governor.commit_if_matches(proposal): raise Blocked('Native reservation expired or mismatched')
            elif decision.status not in {N.models.DecisionStatus.COMMITTED,N.models.DecisionStatus.RECONCILED}:
                order['status']='REJECTED'; self._save('native_rejection',{'id':order['id'],'reason':decision.reason}); raise Blocked(decision.reason)
            if not self.governor.reconcile_execution(proposal_id=order['proposal'],executor_id=order['executor'],account_name=self.config.account,connector_name=self.config.connector,controller_id=self.config.owner,reason='isolated paper executor receipt'):
                raise Blocked('Native exact executor receipt reconciliation failed')
        if order['kind']=='PSM_REBUILD': self.psm.ensure_order_position(order)
        order['status']='OPEN'; self._save('buy_authorized',{'id':order['id']})
        self._sync_proposal(order['proposal'],order['position'])

    def request_repair(self,pair,*,rung,now):
        evidence=self.repair_feasibility(pair,rung=rung,now=now)
        if not evidence['eligible']:
            if evidence['capital_blocked']:
                # Evidence recording does not select a cohort, consume a rung,
                # create a reservation, or persist a waiting-cohort profit latch.
                self._asset(pair)['repair_block']=evidence; self._save('repair_capital_block',evidence)
                raise CapitalBlocked(evidence['reason_code'],evidence['reason'])
            raise Blocked(evidence['reason'])
        bar=self._bar(pair); asset=self._asset(pair); cid=evidence['cohort_id']; cohort=self.state['cohorts'][cid]
        target=D(evidence['target']); quantity=self.config.quantity_for_budget(pair,target,bar.close)
        self._admit_liquidity(pair,target,bar)
        asset['selected']=cid; asset['last_repair_bar']=bar.completed_at; asset['repair_block']=None
        self.selected_cohort_cycle(pair,cohort_id=cid,now=self.now)
        cohort['rung']={'level':rung,'requested_target':str(target),'start_basis':str(target),'actual_added':'0','shortfall':str(target),'status':'PENDING','decision_bar':bar.completed_at,'quantity':str(quantity),'reference':str(bar.close),'orders':[]}
        self._save('cohort_selected',{'cohort':cid,'rung':rung})
        try: order=self._new_buy(pair,cid,'REPAIR','repair',target,bar.close,quantity,decision_at=self.now,capacity=bar.quote_volume*self.config.participation)
        except Exception:
            cohort['rung']['status']='BLOCKED'; self._save(); raise
        cohort['rung']['orders'].append(order['id']); self._save()
        return dict(order)

    def record_fill(self,order_id,*,trade_id,quantity,price,fee,occurred_at,crash_after_journal=False):
        order=self.state['orders'][order_id]
        quantity=decimal(quantity,positive=True); price=decimal(price,positive=True); fee=decimal(fee)
        when=timestamp(occurred_at)
        if when>self.now or when<order['decision_at']: raise Blocked('Future or predecision fill')
        raw={'trade_id':trade_id,'order_id':order_id,'account_name':self.config.account,'connector_name':self.config.connector,'trading_pair':order['pair'],'trade_type':order['side'],'amount':str(quantity),'price':str(price),'fee_paid':str(fee),'fee_currency':'USD','timestamp':iso(when)}
        payload=encoded(raw)
        with self._db() as db: existing=db.execute('SELECT payload,applied FROM receipts WHERE id=?',(trade_id,)).fetchone()
        if existing:
            if existing[0]!=payload: raise Blocked('Conflicting duplicate paper trade ID')
            if not existing[1]: self._apply_receipt(raw)
            return
        if order['kind'] in {'PSM_EXIT','PSM_REBUILD'}: self.psm.validate_fill(order_id,quantity=quantity,price=price,fee=fee)
        filled,gross,fees=self._totals(order)
        if order['status'] not in {'OPEN','PARTIAL','CANCEL_REQUESTED'}: raise Blocked('Late fill after terminal reconciliation')
        if quantity/self.config.rules[order['pair']].base_step != (quantity/self.config.rules[order['pair']].base_step).to_integral_value(): raise Blocked('Fill quantity violates base step')
        if filled+quantity>D(order['quantity']): raise Blocked('Fill exceeds authorized quantity')
        if fee!=self.expected_fill_fee(order_id,quantity*price): raise Blocked('Paper fee contradicts declared cumulative order rate/rounding')
        if order['side']=='BUY':
            if not order.get('execution_frozen'): raise Blocked('Executable quantity must be frozen before fill')
            if price>D(order['reference']): raise Blocked('Buy exceeds frozen execution price')
            if gross+fees+quantity*price+fee>D(order['target']): raise Blocked('Fill exceeds full all-in budget')
        else:
            eligible=self.ledger.active_lot_ids_for_provenance(position_id=order['position'],proposal_id=order['proposal'],executor_id=order['executor'])
            allowed=set(order['eligible_lot_ids'])
            if not set(eligible)<=allowed: raise Blocked('Sell ownership scope expanded')
            owned=sum((self.ledger.get_lot(l).base_remaining for l in eligible),ZERO)
            if quantity>owned: raise Blocked('Cannot sell more than exact owner inventory')
        with self._db() as db: db.execute('INSERT INTO receipts(id,order_id,payload) VALUES (?,?,?)',(trade_id,order_id,payload))
        if crash_after_journal: raise RuntimeError('synthetic crash after durable receipt journal')
        self._apply_receipt(raw)
        if order['kind'] in {'PSM_EXIT','PSM_REBUILD'}: self.psm.sync()

    def _apply_receipt(self,raw):
        order=self.state['orders'][raw['order_id']]
        self._sync_proposal(order['proposal'],order['position'])
        if order['side']=='BUY':
            cid=order['cohort']; lot='trade-fill:'+raw['trade_id']
            if lot not in self.state['cohorts'][cid]['lot_ids']: self.state['cohorts'][cid]['lot_ids'].append(lot)
            if order['kind']=='SEED': self.state['cohorts'][cid]['anchor']=raw['price']
        order['status']='PARTIAL'
        self._save('receipt_reconciled',{'trade_id':raw['trade_id']})
        with self._db() as db: db.execute('UPDATE receipts SET applied=1 WHERE id=?',(raw['trade_id'],))

    def _sync_proposal(self,proposal,position):
        orders=[o for o in self.state['orders'].values() if o['proposal']==proposal]
        executor=orders[0]['executor']
        evidence=[]; trades=[]
        for o in orders:
            q,g,f=self._totals(o)
            evidence.append(N.executor_provenance_bridge.ExecutorOrderEvidence(executor_id=executor,order_id=o['id'],order_type='PAPER_LIMIT',side=o['side'],amount=D(o['quantity']),price=D(o['reference']),status=o['status'],filled_amount=q,average_fill_price=g/q if q else None,created_at=o['created_at'],updated_at=self.now))
            trades.extend(N.fill_reconciliation.trade_fill_evidence_from_raw(r) for r in self._receipts(o['id']))
        report=N.executor_provenance_bridge.synchronize_executor_evidence(reconciler=self.reconciler,position_id=position,proposal_id=proposal,order_snapshot=N.executor_provenance_bridge.ExecutorOrderSnapshot(executor,tuple(evidence)),trade_snapshot=N.executor_provenance_bridge.ExecutorTradeSnapshot(executor,tuple(trades)),now=self.now)
        if not report.fully_reconciled: raise Blocked('Native exact fill reconciliation incomplete')
        gross=sum((self._totals(o)[1] for o in orders),ZERO)
        fees=sum((self._totals(o)[2] for o in orders),ZERO)
        remaining=sum((l.cost_basis_remaining_quote for l in self.ledger.list_active_lots(position) if l.proposal_id==proposal),ZERO)
        pending=any(o['status'] in {'PREPARING','OPEN','PARTIAL','CANCEL_REQUESTED'} for o in orders)
        with N.paper_time(self.now):
            self.governor.reconcile_executor_accounting(N.models.ExecutorAccountingObservation(executor_id=executor,status='RUNNING' if pending or remaining else 'TERMINATED',filled_amount_quote=gross,cum_fees_quote=fees,position_size_quote=remaining,executor_type='grid_executor',close_type=None if pending or remaining else 'COMPLETED'))
        return report

    def refresh_accounting(self,pair,*,now):
        self.advance(now)
        position=self._current_position(pair)
        proposals=list(dict.fromkeys(o['proposal'] for o in self.state['orders'].values() if o['position']==position and o['status']!='REJECTED'))
        try:
            reports=[self._sync_proposal(p,position) for p in proposals]
            return self.health.record_reports(position_id=position,reports=reports,now=self.now)
        except Exception as exc:
            self.health.record_conflict(position_id=position,now=self.now,reason='paper_native_sync_failed: '+str(exc))
            raise

    def reconcile_terminal(self,order_id,status,*,now):
        self.advance(now)
        if status not in {'FILLED','CANCELED','REJECTED'}: raise Blocked('Terminal order status required')
        order=self.state['orders'][order_id]
        if order['terminal']:
            if order['terminal']!=status: raise Blocked('Conflicting terminal status')
            return
        q,gross,fee=self._totals(order)
        if status=='FILLED' and q!=D(order['quantity']): raise Blocked('FILLED requires full executable quantity')
        if status=='REJECTED' and q: raise Blocked('Rejected order cannot have fills')
        self._sync_proposal(order['proposal'],order['position'])
        order['status']=status; order['terminal']=status
        if order['kind']=='SEED' and status=='FILLED':
            cohort=self.state['cohorts'][order['cohort']]
            seed_orders=[o for o in self.state['orders'].values() if o['kind']=='SEED' and o['cohort']==cohort['id']]
            seed_q=sum((self._totals(o)[0] for o in seed_orders),ZERO)
            seed_gross=sum((self._totals(o)[1] for o in seed_orders),ZERO)
            anchor=seed_gross/seed_q
            cohort['anchor']=str(anchor)
            cohort['acquired_at']=max(datetime.fromisoformat(f['timestamp']).timestamp() for o in seed_orders for f in self._receipts(o['id']))
            cohort['anchor_history']=[{'generation':1,'price':str(anchor),'receipt_orders':[o['id'] for o in seed_orders],'at':self.now,'acquired_at':cohort['acquired_at']}]
        if order['kind']=='REPAIR':
            cohort=self.state['cohorts'][order['cohort']]; rung=cohort['rung']
            actual=sum((self._totals(self.state['orders'][oid])[1]+self._totals(self.state['orders'][oid])[2] for oid in rung['orders']),ZERO)
            target=D(rung['requested_target']); shortfall=target-actual
            rung.update(actual_added=str(actual),shortfall=str(shortfall))
            threshold=self.config.all_in(self.config.rules[order['pair']].base_step,D(rung['reference']))+self.config.fee_quantum
            if status=='FILLED' and q==D(rung['quantity']) and ZERO<=shortfall<threshold:
                rung['status']='COMPLETE' if shortfall==0 else 'ROUNDING_COMPLETED'; cohort['last_rung']=rung['level']
            else: rung['status']='INCOMPLETE'
        if order['side']=='SELL': self._release_sold_cohort(order['cohort'])
        self._save('terminal_reconciliation',{'order':order_id,'status':status})
        self.refresh_accounting(order['pair'],now=self.now)
        if order['kind'] in {'PSM_EXIT','PSM_REBUILD'}: self.psm.sync()

    def cancel_requested(self,order_id,*,now):
        self.advance(now); order=self.state['orders'][order_id]
        if order['status'] not in {'OPEN','PARTIAL'}: raise Blocked('No cancellable order')
        order['status']='CANCEL_REQUESTED'; self._save('cancel_requested',{'order':order_id})

    def observe_open(self,order_id,opening:ObservedOpen,*,fill_fraction=ONE):
        """Causal delayed open model. It deliberately has no execution-volume input."""
        self.advance(opening.observed_at); order=self.state['orders'][order_id]
        if order['status'] not in {'OPEN','PARTIAL'}: raise Blocked('Order is not pending')
        expected_open=order['decision_at']+self.config.execution_delay_seconds
        if opening.opened_at<expected_open: raise Blocked('Execution open is not delayed after decision')
        if opening.opened_at>expected_open:
            self.reconcile_terminal(order_id,'CANCELED',now=self.now); return None
        if opening.observed_at-order['decision_at']>self.config.max_order_age_seconds:
            self.reconcile_terminal(order_id,'CANCELED',now=self.now); return None
        if order['kind'] in {'PSM_EXIT','PSM_REBUILD'}: self.psm.validate_open(order_id,opening)
        fraction=decimal(fill_fraction)
        if fraction>1: raise Blocked('Invalid synthetic fill fraction')
        rule=self.config.rules[order['pair']]
        slip=self.config.expected_slippage_bps/BPS
        raw_price=opening.price*(ONE+slip if order['side']=='BUY' else ONE-slip)
        mode=ROUND_UP if order['side']=='BUY' else ROUND_DOWN
        execution_price=(raw_price/rule.price_tick).to_integral_value(rounding=mode)*rule.price_tick
        if order['kind']=='PSM_EXIT' and execution_price<D(order['reference']):
            self.reconcile_terminal(order_id,'CANCELED',now=self.now); return None
        if not order.get('execution_frozen'):
            if order['side']=='BUY' and not order.get('fixed_quantity'):
                quantity=self.config.quantity_for_budget(order['pair'],D(order['target']),execution_price)
                if quantity<rule.min_base or quantity*execution_price<rule.min_quote:
                    self.reconcile_terminal(order_id,'CANCELED',now=self.now); return None
                order['quantity']=str(quantity)
                order['reference']=str(execution_price)
            elif order['side']=='BUY' and execution_price>D(order['reference']):
                self.reconcile_terminal(order_id,'CANCELED',now=self.now); return None
            order['execution_frozen']=True
            order['execution_price']=str(execution_price)
            order['execution_observed_at']=opening.observed_at
            if order['kind']=='REPAIR':
                rung=self.state['cohorts'][order['cohort']]['rung']
                rung['quantity']=order['quantity']; rung['reference']=str(execution_price)
            self._save('executable_quantity_frozen',{'order':order_id,'quantity':order['quantity'],'price':str(execution_price)})
        elif execution_price!=D(order['execution_price']):
            raise Blocked('Synthetic partial fills must use the original observed open')
        q,_,_=self._totals(order)
        quantity=floor_step((D(order['quantity'])-q)*fraction,self.config.rules[order['pair']].base_step)
        if quantity<=0: return None
        trade_id=self._id(order_id+':fill')
        self.record_fill(order_id,trade_id=trade_id,quantity=quantity,price=execution_price,fee=self.expected_fill_fee(order_id,quantity*execution_price),occurred_at=self.now)
        if self._totals(order)[0]==D(order['quantity']): self.reconcile_terminal(order_id,'FILLED',now=self.now)
        return trade_id

    def _release_sold_cohort(self,cid):
        if not cid: return
        cohort=self.state['cohorts'][cid]
        if self._cohort_lots(cohort): return
        if any(o['cohort']==cid for o in self._pending()): return
        cohort['status']='SOLD'
        key=f'{self.config.owner}:{cid}:generation:{cohort.get("generation",1)}:lifecycle'
        lifecycle=self.lifecycle.load(key)
        if lifecycle is not None:
            N.vcap_lifecycle.checkpoint_vcap_exit_result(state=lifecycle,store=self.lifecycle,recovery_budget_released=True,now=self.now)
        if self._asset(cohort['pair'])['selected']==cid:
            self._asset(cohort['pair'])['selected']=None
            self._asset(cohort['pair'])['selection_cursor']=cohort['ordinal']%self.config.seed_count+1

    @per_call_snapshot
    def audit(self,*,require_marks=True):
        pools=self.cash_pools()
        if any(not v.is_finite() or v<0 for v in pools.values()): raise Blocked('Negative or nonfinite owner cash')
        if pools['protected']!=self.config.protected_cash: raise Blocked('Protected reserve was spent')
        if any(v<0 for v in self.funding_cash().values()): raise Blocked('Negative source-funding-owner cash')
        if self.cash()-self.holds()-self._unsettled_sales()<self.config.protected_cash: raise Blocked('Cash holds breach reserve')
        receipt_units={pair:ZERO for pair in self.config.rules}
        for f in self._receipts():
            order=self.state['orders'][f['order_id']]
            receipt_units[order['pair']]+=D(f['amount'])*(ONE if order['side']=='BUY' else -ONE)
        for pair in self.config.rules:
            native_units=sum((l.base_remaining for l in self._lots(pair)),ZERO)
            if native_units!=receipt_units[pair] and not self._receipts(applied=False): raise Blocked('Paper/native units disagree')
        lotclaims={}
        for c in self.state['cohorts'].values():
            for lid in c['lot_ids']:
                if lid in lotclaims: raise Blocked('Native lot is claimed by multiple cohorts')
                lotclaims[lid]=c['id']
        active_ids={l.lot_id for l in self._lots()}
        if not active_ids<=set(lotclaims) and not self._receipts(applied=False): raise Blocked('Unclaimed native inventory')
        realized=sum((self.ledger.snapshot(pid).realized_pnl_quote for a in self.state['assets'].values() for pid in a['positions']),ZERO)
        basis=self.deployed_basis()
        commitments=self.capital_commitments()
        if abs(self.cash()+basis-self.config.initial_cash-realized)>D('1e-20'): raise Blocked('Cash/basis/lifetime realized PnL do not conserve')
        value=ZERO; ages={}; missing=[]
        for pair in self.state['assets']:
            mark=self.state['marks'].get(pair)
            if not mark:
                if require_marks: raise Blocked('Missing mark')
                missing.append(pair)
                continue
            age=self.now-mark['completed_at']; ages[pair]=age
            if require_marks and (age<0 or age>self.config.max_mark_age_seconds): raise Blocked('Stale or future mark')
            value+=sum((l.base_remaining for l in self._lots(pair)),ZERO)*D(mark['close'])
        return {'cash':self.cash(),'owner_cash':pools,'reserved_cash':self.holds(),'unsettled_sale_cash':self._unsettled_sales(),'deployed_basis':basis,'market_exposure':value,'realized_pnl':realized,'unrealized_pnl':value-basis,'equity':self.cash()+value,'total_pnl':self.cash()+value-self.config.initial_cash,'mark_age_seconds':ages,'quarantine':tuple(self.state['quarantine']),'valuation_complete':not missing,'missing_marks':missing}

    def _recover(self):
        for order in list(self.state['orders'].values()):
            if order['status']=='PREPARING': self._authorize(order)
        for raw in self._receipts(applied=False):
            try: self._apply_receipt(raw)
            except Exception as exc:
                self.state['quarantine'].append({'trade_id':raw['trade_id'],'reason':'restart: '+str(exc),'at':self.now})
                self.state['breaker']='HALTED'
        for order in self.state['orders'].values():
            if order['kind']=='REPAIR':
                rung=self.state['cohorts'][order['cohort']]['rung']
                if rung and order.get('repair_rung')==rung['level'] and order.get('cohort_generation')==self.state['cohorts'][order['cohort']].get('generation',1) and order['id'] not in rung['orders']: rung['orders'].append(order['id'])
        self._save('restart_reconciled')
        if self.state['psm']:
            try: self.psm.sync()
            except Exception as exc:
                self.state['quarantine'].append({'reason':'PSM restart: '+str(exc),'at':self.now}); self.state['breaker']='HALTED'; self._save()

    def aggregate_diagnostic(self,pair,*,fingerprint,now):
        """Real persisted native lifecycle for health/profit protection.

        Aggregate recovery authority is zero: cohort sizing is a separate, explicitly
        new policy, never an overridden/relabelled aggregate native tranche.
        """
        self.advance(now); bar=self._bar(pair)
        position=self._current_position(pair); snapshot=self.ledger.snapshot(position)
        if not snapshot.has_active_inventory: raise Blocked('No native inventory for lifecycle')
        key=position+':native-lifecycle'
        state=self.lifecycle.load(key)
        if state is None:
            state=N.vcap_state.VCAPLifecycleState(envelope_proposal_id=key,pair=pair,next_sequence=1,remaining_authorized_quote=ZERO,recovery_proposal_ids=(),position_base=snapshot.active_base,cost_basis_quote=snapshot.active_cost_basis_quote,last_known_price=bar.close,profit_state=None,status=N.vcap_state.VCAPLifecycleStatus.ACTIVE,updated_at=self.now)
        health=self.health.assess(position_id=position,now=self.now)
        decision=N.vcap_lifecycle.plan_vcap_lifecycle_from_ledger(position_id=position,ledger=self.ledger,state=state,store=self.lifecycle,fingerprint=fingerprint,trading_rule=self._native_rule(pair),current_price=bar.close,seed_quote=self.config.seed_ticket,expected_exit_fee_bps=self.config.fee_rate*BPS,slippage_allowance_bps=self.config.expected_slippage_bps,now=self.now,profit_policy=N.vcap_profit_exit.VCAPProfitExitPolicy(target_net_profit_bps=self.config.target_net_profit_bps),governor=None,accounting_health=health)
        self._asset(pair)['aggregate_diagnostic']={'action':decision.action.value,'at':self.now,'completed_bar':bar.completed_at,'reasons':list(decision.reasons),'aggregate_recovery_authority':'0'}
        self._save('native_aggregate_diagnostic',self._asset(pair)['aggregate_diagnostic'])
        return decision

    def cohort_profit(self,cohort_id,*,now,fingerprint=None):
        self.advance(now); cohort=self.state['cohorts'][cohort_id]; bar=self._bar(cohort['pair'])
        lots=self._cohort_lots(cohort)
        quantity=sum((l.base_remaining for l in lots),ZERO)
        principal=sum((l.cost_basis_remaining_quote for l in lots),ZERO)
        fees=sum((l.entry_fees_remaining_quote for l in lots),ZERO)
        if not quantity or not principal: raise Blocked('Cohort has no owned inventory')
        old=cohort['profit_state']
        state=None if old is None else N.vcap_profit_exit.VCAPProfitState(updated_at=old['updated_at'],profit_floor_reached_at=old['profit_floor_reached_at'],peak_net_profit_bps=D(old['peak_net_profit_bps']),peak_at=old['peak_at'])
        result=N.vcap_profit_exit.plan_vcap_profit_exit(N.recovery_fingerprint.VCAPPositionState(pair=cohort['pair'],position_base=quantity,cost_basis_quote=principal,current_price=bar.close),costs=N.vcap_profit_exit.VCAPExitCostModel(accrued_fees_quote=fees,expected_exit_fee_bps=self.config.fee_rate*BPS,slippage_allowance_bps=self.config.expected_slippage_bps),now=self.now,fingerprint=fingerprint,previous_state=state,policy=N.vcap_profit_exit.VCAPProfitExitPolicy(target_net_profit_bps=self.config.target_net_profit_bps))
        # Preview only: the authoritative persisted profit state belongs to
        # selected_cohort_cycle and its real native VCAPStateStore.
        return result

    def request_cohort_exit(self,pair,*,now,fingerprint=None):
        self.advance(now); self._assert_clean(pair)
        if self._pending(pair) or self._asset(pair).get('exit'): raise Blocked('Pending asset action blocks new exit')
        if pair in self.state['psm'] and self.state['psm'][pair].get('mode')!='REPAIR': raise Blocked('PSM owns asset claim')
        cid=self._asset(pair)['selected']
        if cid is None: raise Blocked('No selected repair cohort')
        decision=self.selected_cohort_cycle(pair,now=self.now,fingerprint=fingerprint)
        profit=decision.profit_decision
        if decision.action!=N.vcap_lifecycle.VCAPLifecycleAction.EXIT: raise Blocked('Native fee-aware cohort profit exit is not eligible')
        cohort=self.state['cohorts'][cid]
        owners=N.economic_exit._group_owners(lots=self._cohort_lots(cohort),governor=self.governor,pair=pair)
        manifest={'id':self._id('cohort-exit'),'cohort':cid,'pair':pair,'scope':'SELECTED_COHORT_NEW_POLICY','created_at':self.now,'owners':[serial(asdict(o)) for o in owners],'status':'OPEN','profit_reason':list(profit.reasons)}
        self._asset(pair)['exit']=manifest; self._save('cohort_exit_manifest',manifest)
        return self.continue_exit(pair,now=self.now)

    def continue_exit(self,pair,*,now):
        self.advance(now); self._assert_clean(pair)
        if self._pending(pair): raise Blocked('Asset exit child is still pending')
        manifest=self._asset(pair).get('exit')
        if not manifest: raise Blocked('No selected exit manifest')
        bar=self._bar(pair); rule=self.config.rules[pair]
        for owner in manifest['owners']:
            eligible=tuple(lid for lid in owner['lot_ids'] if self.ledger.get_lot(lid).base_remaining>0)
            if not eligible: continue
            live=self.ledger.active_lot_ids_for_provenance(position_id=self._current_position(pair),proposal_id=owner['proposal_id'],executor_id=owner['executor_id'])
            if set(live)!=set(eligible): raise Blocked('Cohort owner scope no longer matches native provenance')
            quantity=floor_step(sum((self.ledger.get_lot(lid).base_remaining for lid in eligible),ZERO),rule.base_step)
            if quantity<=0 or quantity<rule.min_base or quantity*bar.close<rule.min_quote:
                manifest['status']='DUST_BLOCKED'; self._save('cohort_dust_retained',{'manifest':manifest['id']}); return None
            if bar.completed_at!=self.now or bar.completed_at<=manifest.get('last_child_bar',0): raise Blocked('Each next exit child requires a fresh completed decision bar')
            cost=quantity*bar.close
            self._admit_liquidity(pair,cost,bar)
            manifest['last_child_bar']=bar.completed_at
            oid=self._id('paper-sell')
            original=next(o for o in self.state['orders'].values() if o['proposal']==owner['proposal_id'] and o['side']=='BUY')
            order={'id':oid,'pair':pair,'position':self._current_position(pair),'cohort':manifest['cohort'],'kind':'COHORT_EXIT','side':'SELL','pool':original['pool'],'funding_owner':original['funding_owner'],'status':'OPEN','proposal':owner['proposal_id'],'executor':owner['executor_id'],'intent':original['intent'],'target':'0','principal':'0','quantity':str(quantity),'reference':str(bar.close),'decision_at':self.now,'created_at':self.now,'capacity':str(cost),'terminal':None,'eligible_lot_ids':list(eligible),'manifest':manifest['id']}
            order['cohort_generation']=self.state['cohorts'][manifest['cohort']].get('generation',1)
            self.state['orders'][oid]=order; self._save('cohort_sell_intent',order)
            self._sync_proposal(order['proposal'],order['position'])
            return dict(order)
        manifest['status']='COMPLETED'; self._save('cohort_exit_complete',manifest)
        self._asset(pair)['exit']=None
        self._release_sold_cohort(manifest['cohort']); self._save()
        return None

    def _admit_liquidity(self,pair,amount,bar):
        key=f'{pair}:{bar.completed_at}'
        budgets=self.state.setdefault('liquidity',{})
        used=D(budgets.get(key,'0'))
        capacity=bar.quote_volume*self.config.participation
        if used+amount>capacity: raise Blocked('Shared asset/bar liquidity budget insufficient; no clipping')
        budgets[key]=str(used+amount)
        # Only the current completed bar can be used again. Keep a bounded audit
        # summary here; immutable order records retain individual admissions.
        for old in list(budgets):
            if old.startswith(pair+':') and old!=key: del budgets[old]
        self._save('liquidity_admission',{'pair':pair,'completed_at':bar.completed_at,'amount':str(amount),'used':str(used+amount),'capacity':str(capacity)})

    def expected_fill_fee(self,order_id,gross_increment):
        """Declared research convention: round cumulative order fees, then delta."""
        _,gross,fees=self._totals(self.state['orders'][order_id])
        return self.config.fee(gross+gross_increment)-fees

    def retry_repair(self,pair,*,now):
        """Retry only the unfilled remainder of the same target, never another double."""
        self.advance(now); self._assert_clean(pair)
        if self._pending(pair) or self._asset(pair).get('exit'): raise Blocked('Pending action blocks repair retry')
        if pair in self.state['psm'] and self.state['psm'][pair].get('mode')!='REPAIR': raise Blocked('PSM owns asset')
        bar=self._bar(pair); asset=self._asset(pair); cid=asset['selected']
        if not cid: raise Blocked('No selected cohort to retry')
        cohort=self.state['cohorts'][cid]; rung=cohort['rung']
        if not rung or rung['status'] not in {'INCOMPLETE','BLOCKED'}: raise Blocked('No incomplete rung')
        self.native_cycle(pair,fingerprint=self._fingerprint(pair),now=self.now)
        if asset['last_native_decision']['action'] in {'EXIT','PROTECT_PROFIT'}: raise Blocked('Native profit protection blocks repair retry')
        if self.cohort_profit(cid,now=self.now).profit_floor_reached: raise Blocked('Cohort profit floor blocks repair retry')
        actual=sum((self._totals(self.state['orders'][oid])[1]+self._totals(self.state['orders'][oid])[2] for oid in rung['orders']),ZERO)
        remaining=D(rung['requested_target'])-actual
        if remaining<=0: raise Blocked('No remaining target')
        self._budget_admission(pair,remaining,'repair')
        quantity=self.config.quantity_for_budget(pair,remaining,bar.close)
        rule=self.config.rules[pair]
        if quantity<rule.min_base or quantity*bar.close<rule.min_quote: raise Blocked('Incomplete target residual is below minimum and remains locked')
        self._admit_liquidity(pair,remaining,bar)
        order=self._new_buy(pair,cid,'REPAIR','repair',remaining,bar.close,quantity,decision_at=self.now,capacity=bar.quote_volume*self.config.participation)
        rung['orders'].append(order['id']); rung['status']='PENDING'; rung['quantity']=str(quantity); rung['reference']=str(bar.close)
        self._save('same_target_retry',{'cohort':cid,'rung':rung['level'],'remaining':str(remaining)})
        return dict(order)

    def ingest_observed_fill(self,order_id,*,trade_id,quantity,price,fee,occurred_at):
        """Authoritative exception evidence path, distinct from synthetic generation.

        Unexpected/late/over-budget fills remain in the physical receipt journal.
        Unreconcilable native ownership produces a signed off-ledger discrepancy;
        no cash liability or realized loss is clipped away.
        """
        try:
            return self.record_fill(order_id,trade_id=trade_id,quantity=quantity,price=price,fee=fee,occurred_at=occurred_at)
        except (Blocked,ValueError,RuntimeError) as exc:
            reason=str(exc)
        order=self.state['orders'][order_id]
        quantity=decimal(quantity,positive=True); price=decimal(price,positive=True); fee=decimal(fee); when=timestamp(occurred_at)
        raw={'trade_id':trade_id,'order_id':order_id,'account_name':self.config.account,'connector_name':self.config.connector,'trading_pair':order['pair'],'trade_type':order['side'],'amount':str(quantity),'price':str(price),'fee_paid':str(fee),'fee_currency':'USD','timestamp':iso(when)}
        payload=encoded(raw)
        with self._db() as db:
            existing=db.execute('SELECT payload FROM receipts WHERE id=?',(trade_id,)).fetchone()
            if existing and existing[0]!=payload:
                db.execute('INSERT INTO audit_events(at,kind,payload) VALUES (?,?,?)',(self.now,'conflicting_observed_receipt',payload))
            elif not existing:
                db.execute('INSERT INTO receipts(id,order_id,payload) VALUES (?,?,?)',(trade_id,order_id,payload))
        self.state['quarantine'].append({'trade_id':trade_id,'reason':reason,'at':self.now})
        self.state['breaker']='HALTED'; self._save('unexpected_observed_fill',{'trade_id':trade_id,'reason':reason})
        if not existing:
            try: self._apply_receipt(raw)
            except Exception as native_error:
                self._save('unreconciled_native_receipt',{'trade_id':trade_id,'reason':str(native_error)})
        return self.report()

    @per_call_snapshot
    def report(self):
        """Always retain stale marked inventory and signed cash, even in quarantine."""
        try:
            report=self.audit(require_marks=False)
            report['accounting_status']='QUARANTINED' if self.state['quarantine'] else 'RECONCILED'
        except Exception as exc:
            basis=self.deployed_basis()
            value=sum((l.base_remaining*D(self.state['marks'][l.pair]['close']) for l in self._lots() if l.pair in self.state['marks']),ZERO)
            realized=sum((self.ledger.snapshot(pid).realized_pnl_quote for a in self.state['assets'].values() for pid in a['positions']),ZERO)
            report={'cash':self.cash(),'owner_cash':self.cash_pools(),'reserved_cash':self.holds(),'unsettled_sale_cash':self._unsettled_sales(),'deployed_basis':basis,'market_exposure':value,'realized_pnl':realized,'unrealized_pnl':value-basis,'equity':self.cash()+value,'total_pnl':self.cash()+value-self.config.initial_cash,'accounting_status':'QUARANTINED','accounting_error':str(exc),'mark_age_seconds':{p:self.now-m['completed_at'] for p,m in self.state['marks'].items()},'quarantine':tuple(self.state['quarantine']),'valuation_complete':all(l.pair in self.state['marks'] for l in self._lots())}
        report['stale_marks']={p:age for p,age in report['mark_age_seconds'].items() if age>self.config.max_mark_age_seconds or age<0}
        report['capital_commitments']=self.capital_commitments()
        commitments=report['capital_commitments']
        report['capital_admission_blocks']=(["TOTAL_CAP"] if commitments['total']>self.config.total_budget_cap else [])+(["RECOVERY_CAP"] if commitments['recovery']>self.config.recovery_cap else [])+["PAIR_CAP:"+p for p,v in commitments['pairs'].items() if v>self.config.pair_budget_cap]
        report['capital_admission_status']='BLOCKED_RETAINED_COMMITMENT' if report['capital_admission_blocks'] else 'WITHIN_CAPS'
        report['funding_owner_cash']=self.funding_cash()
        report['cash_deficit']=min(ZERO,self.cash())
        report['owner_cash_deficits']={p:v for p,v in self.cash_pools().items() if v<0}
        report['unapplied_receipt_ids']=[f['trade_id'] for f in self._receipts(applied=False)]
        journal_units={p:ZERO for p in self.config.rules}
        for f in self._receipts(): journal_units[f['trading_pair']]+=D(f['amount'])*(ONE if f['trade_type']=='BUY' else -ONE)
        report['native_inventory_discrepancy']={p:journal_units[p]-sum((l.base_remaining for l in self._lots(p)),ZERO) for p in journal_units}
        report['receipt_implied_market_exposure']=sum((units*D(self.state['marks'][pair]['close']) for pair,units in journal_units.items() if pair in self.state['marks']),ZERO)
        report['receipt_implied_equity']=self.cash()+report['receipt_implied_market_exposure']
        report['receipt_implied_total_pnl']=report['receipt_implied_equity']-self.config.initial_cash
        report['valuation_scope']='UNRECONCILED_NATIVE_ONLY' if any(report['native_inventory_discrepancy'].values()) else 'RECONCILED_NATIVE'
        report['seed_complete']=self.seed_complete()
        return report

    @property
    def psm(self):
        from .psm_bridge import PSMBridge
        if not hasattr(self,'_psm_bridge'): self._psm_bridge=PSMBridge(self)
        return self._psm_bridge

    def selected_cohort_cycle(self,pair,*,now,cohort_id=None,fingerprint=None):
        """Unchanged native lifecycle on exact native lot-derived cohort geometry.

        Whole-position ledger health is mandatory. Cohort/generation scope is new
        orchestration; aggregate recovery sizing has zero capital authority.
        """
        self.advance(now); self._assert_clean(pair)
        bar=self._bar(pair); asset=self._asset(pair)
        if bar.completed_at!=self.now: raise Blocked('Native selected lifecycle requires a current completed decision bar')
        cid=cohort_id or asset['selected']
        if not cid: raise Blocked('No selected cohort lifecycle')
        cohort=self.state['cohorts'][cid]
        if cohort['pair']!=pair: raise Blocked('Cohort belongs to another asset')
        if asset['selected'] not in {None,cid}: raise Blocked('Waiting cohort has no lifecycle mutation authority')
        lots=self._cohort_lots(cohort)
        # Real native provenance guard, not a synthetic ownership view.
        N.economic_exit._group_owners(lots=lots,governor=self.governor,pair=pair)
        quantity=sum((l.base_remaining for l in lots),ZERO)
        principal=sum((l.cost_basis_remaining_quote for l in lots),ZERO)
        fees=sum((l.entry_fees_remaining_quote for l in lots),ZERO)
        key=f'{self.config.owner}:{cid}:generation:{cohort.get("generation",1)}:lifecycle'
        state=self.lifecycle.load(key)
        if state is None:
            state=N.vcap_state.VCAPLifecycleState(envelope_proposal_id=key,pair=pair,next_sequence=1,remaining_authorized_quote=ZERO,recovery_proposal_ids=(),position_base=quantity,cost_basis_quote=principal,last_known_price=bar.close,profit_state=None,status=N.vcap_state.VCAPLifecycleStatus.ACTIVE,updated_at=self.now)
        decision=N.vcap_lifecycle.plan_vcap_lifecycle(state=state,store=self.lifecycle,position=N.recovery_fingerprint.VCAPPositionState(pair=pair,position_base=quantity,cost_basis_quote=principal,current_price=bar.close),fingerprint=fingerprint or self._fingerprint(pair),trading_rule=self._native_rule(pair),seed_quote=self.config.seed_ticket,costs=N.vcap_profit_exit.VCAPExitCostModel(accrued_fees_quote=fees,expected_exit_fee_bps=self.config.fee_rate*BPS,slippage_allowance_bps=self.config.expected_slippage_bps),now=self.now,profit_policy=N.vcap_profit_exit.VCAPProfitExitPolicy(target_net_profit_bps=self.config.target_net_profit_bps))
        cohort['profit_state']=None if decision.state.profit_state is None else serial(asdict(decision.state.profit_state))
        asset['last_native_decision']={'action':decision.action.value,'at':self.now,'completed_bar':bar.completed_at,'cohort_id':cid,'generation':cohort.get('generation',1),'scope':'SELECTED_COHORT','reasons':list(decision.reasons),'aggregate_recovery_authority':'0'}
        self._save('native_selected_cohort_lifecycle',asset['last_native_decision'])
        return decision

    def native_cycle(self,pair,*,now,fingerprint=None):
        return self.selected_cohort_cycle(pair,now=now,fingerprint=fingerprint)

    def refresh_accounting_all(self,*,now):
        self.advance(now)
        return {pair:self.refresh_accounting(pair,now=self.now) for pair in self.state['assets']}

    @per_call_snapshot
    def repair_feasibility(self,pair,*,now,rung=None):
        """Structured read-only policy evidence; no latch/reservation/selection write.

        Advancing the observation clock is allowed. Capital blocks are whitelisted
        only after health, freshness, ownership, due-rung and profit guards pass.
        """
        self.advance(now)
        evidence={'pair':pair,'at':self.now,'cohort_id':None,'generation':None,'next_rung':None,'requested_rung':rung,'trigger':None,'completed_bar':None,'target':None,'eligible':False,'capital_blocked':False,'reason_code':None,'reason':None,'guards':{'health':False,'freshness':False,'pending_clear':False,'seed_complete':False,'rung_due':False,'profit_clear':False,'ownership':False,'liquidity_valid':False}}
        def blocked(code,reason):
            evidence.update(reason_code=code,reason=reason); return evidence
        try: self._assert_clean(pair)
        except Exception as exc: return blocked('ACCOUNTING',str(exc))
        evidence['guards']['health']=True
        if not self.seed_complete(): return blocked('SEED_INCOMPLETE','All assets must cross seed-completion barrier')
        evidence['guards']['seed_complete']=True
        asset=self._asset(pair)
        if self._pending(pair) or asset.get('exit'): return blocked('PENDING','Asset already has pending action or exit manifest')
        if pair in self.state['psm'] and self.state['psm'][pair].get('mode')!='REPAIR': return blocked('PSM_OWNERSHIP','PSM owns this account/asset claim')
        evidence['guards']['pending_clear']=True
        try: bar=self._bar(pair)
        except Exception as exc: return blocked('MARK_STALE',str(exc))
        evidence['guards']['freshness']=True; evidence['completed_bar']=bar.completed_at
        if asset.get('last_repair_bar')==bar.completed_at: return blocked('SAME_BAR','Only one new rung per completed decision bar')
        if bar.completed_at!=self.now: return blocked('DECISION_BOUNDARY','New repair decision requires the current completed-bar boundary')
        cid=asset['selected']
        if cid is None:
            candidates=[c for c in self.state['cohorts'].values() if c['pair']==pair and self._cohort_lots(c)]
            if not candidates: return blocked('NO_INVENTORY','No owned cohort')
            cursor=asset.get('selection_cursor',1)
            cid=min(candidates,key=lambda c:((c['ordinal']-cursor)%self.config.seed_count,c.get('generation',1),c['id']))['id']
        c=self.state['cohorts'][cid]; lots=self._cohort_lots(c)
        evidence['cohort_id']=cid; evidence['generation']=c.get('generation',1)
        evidence['acquired_at']=c.get('acquired_at')
        if c.get('acquired_at') is None or bar.opened_at<c['acquired_at']:
            return blocked('PRE_ACQUISITION_BAR','Repair requires a fully post-acquisition completed bar')
        try: N.economic_exit._group_owners(lots=lots,governor=self.governor,pair=pair)
        except Exception as exc: return blocked('OWNERSHIP',str(exc))
        evidence['guards']['ownership']=True
        if c['rung'] and c['rung']['status'] not in {'COMPLETE','ROUNDING_COMPLETED'}: return blocked('INCOMPLETE_RUNG','Prior selected-cohort rung is incomplete')
        next_rung=c['last_rung']+1; evidence['next_rung']=next_rung
        if rung is not None and (type(rung) is not int or rung!=next_rung): return blocked('RUNG_SEQUENCE','Only the next distinct ladder rung is eligible')
        trigger=D(c['anchor'])*(ONE-self.config.ladder_fraction)**next_rung
        target=self.cohort_basis(cid); evidence['trigger']=str(trigger); evidence['target']=str(target)
        if bar.close>trigger: return blocked('RUNG_NOT_DUE','Completed close has not reached next causal rung')
        evidence['guards']['rung_due']=True
        principal=sum((l.cost_basis_remaining_quote for l in lots),ZERO); fees=sum((l.entry_fees_remaining_quote for l in lots),ZERO); units=sum((l.base_remaining for l in lots),ZERO)
        key=f'{self.config.owner}:{cid}:generation:{c.get("generation",1)}:lifecycle'
        state=self.lifecycle.load(key)
        profit=N.vcap_profit_exit.plan_vcap_profit_exit(N.recovery_fingerprint.VCAPPositionState(pair=pair,position_base=units,cost_basis_quote=principal,current_price=bar.close),costs=N.vcap_profit_exit.VCAPExitCostModel(accrued_fees_quote=fees,expected_exit_fee_bps=self.config.fee_rate*BPS,slippage_allowance_bps=self.config.expected_slippage_bps),now=self.now,fingerprint=self._fingerprint(pair),previous_state=None if state is None else state.profit_state,policy=N.vcap_profit_exit.VCAPProfitExitPolicy(target_net_profit_bps=self.config.target_net_profit_bps))
        if profit.profit_floor_reached: return blocked('PROFIT_PROTECTED','Selected cohort native profit protection blocks repair')
        evidence['guards']['profit_clear']=True
        used=D(self.state.get('liquidity',{}).get(f'{pair}:{bar.completed_at}','0'))
        evidence['guards']['liquidity_valid']=used+target<=bar.quote_volume*self.config.participation
        evidence['capital_calculation']={'repair_owner_available':str(self.available_pool('repair')),'physical_cash':str(self.cash()),'all_pending_holds':str(self.holds()),'unsettled_sale_cash':str(self._unsettled_sales()),'protected_cash_floor':str(self.config.protected_cash),'total_native_basis':str(self.deployed_basis()),'pair_native_basis':str(self.deployed_basis(pair)),'total_cap':str(self.config.total_budget_cap),'pair_cap':str(self.config.pair_budget_cap),'full_target':str(target),'prior_bar_capacity':str(bar.quote_volume*self.config.participation),'prior_bar_already_admitted':str(used)}
        try: self._budget_admission(pair,target,'repair')
        except CapitalBlocked as exc:
            evidence['capital_blocked']=True; return blocked(exc.code,str(exc))
        # Liquidity failure is not a capital fallback: require full un-clipped target.
        used=D(self.state.get('liquidity',{}).get(f'{pair}:{bar.completed_at}','0'))
        if used+target>bar.quote_volume*self.config.participation: return blocked('LIQUIDITY','Full requested repair exceeds shared causal liquidity')
        q=self.config.quantity_for_budget(pair,target,bar.close); rule=self.config.rules[pair]
        if q<rule.min_base or q*bar.close<rule.min_quote: return blocked('MINIMUM','Rounded full repair below exchange minimum')
        evidence.update(eligible=True,reason_code='ADMISSIBLE',reason='Full target admitted without clipping')
        return evidence

    def missing_open(self,order_id,*,expected_at,now):
        """Explicit missing-bar cancellation; no carry forward to a favorable open."""
        self.advance(now); order=self.state['orders'][order_id]
        if expected_at!=order['decision_at']+self.config.execution_delay_seconds or now<expected_at:
            raise Blocked('Missing-open evidence has wrong or future boundary')
        self.reconcile_terminal(order_id,'CANCELED',now=self.now)

    def resume_seed_asset(self,pair,*,now):
        """Idempotently finish a durable ten-ticket plan after process interruption.

        Within the original decision timestamp the already admitted batch liquidity
        is reused exactly once. Later decisions re-admit only missing ticket budgets
        against the fresh prior bar; old timing is never silently extended.
        """
        self.advance(now); self._assert_clean(health=False)
        asset=self._asset(pair); batch=asset.get('seed_batch')
        if not batch: raise Blocked('No durable seed batch specification')
        if self.state.get('seed_completion_receipt'): return []
        missing=[]
        for index in range(batch['expected_count']):
            cid=f'{pair}:seed:{index+1:02d}'
            orders=[o for o in self.state['orders'].values() if o['kind']=='SEED' and o['cohort']==cid]
            if not orders: missing.append((index,cid))
        if not missing: return [dict(o) for o in self._pending(pair) if o['kind']=='SEED']
        if self.now==batch['decision_at']:
            price=D(batch['reference']); decision_bar=batch['completed_bar']
        else:
            bar=self._bar(pair); price=bar.close; decision_bar=bar.completed_at
            self._budget_admission(pair,self.config.seed_ticket*len(missing),'seed')
            self._admit_liquidity(pair,self.config.seed_ticket*len(missing),bar)
        result=[]
        for index,cid in missing:
            if cid not in self.state['cohorts']:
                self.state['cohorts'][cid]={'id':cid,'pair':pair,'ordinal':index+1,'generation':1,'origin':'SEED','anchor':None,'anchor_history':[],'lot_ids':[],'last_rung':0,'rung':None,'status':'OWNED','profit_state':None}
            quantity=self.config.quantity_for_budget(pair,self.config.seed_ticket,price)
            order=self._new_buy(pair,cid,'SEED','seed',self.config.seed_ticket,price,quantity,decision_at=self.now,capacity=None)
            order['decision_bar']=decision_bar; self._save(); result.append(dict(order))
        return result

    def retry_seed(self,cohort_id,*,now):
        self.advance(now); self._assert_clean(health=False)
        if self.state.get('seed_completion_receipt'): raise Blocked('Initialization already completed')
        cohort=self.state['cohorts'][cohort_id]
        if cohort.get('origin')!='SEED': raise Blocked('Not an original seed cohort')
        prior=[o for o in self.state['orders'].values() if o['cohort']==cohort_id and o['kind']=='SEED']
        if not prior or any(o['terminal'] is None for o in prior): raise Blocked('Seed intent still pending')
        if prior[-1]['terminal']=='FILLED': raise Blocked('Seed cohort already completed')
        spent=sum((self._totals(o)[1]+self._totals(o)[2] for o in prior),ZERO)
        target=self.config.seed_ticket-spent; pair=cohort['pair']; bar=self._bar(pair)
        self._budget_admission(pair,target,'seed')
        quantity=self.config.quantity_for_budget(pair,target,bar.close); rule=self.config.rules[pair]
        if quantity<rule.min_base or quantity*bar.close<rule.min_quote: raise Blocked('Seed residual below executable minimum; barrier remains incomplete')
        self._admit_liquidity(pair,target,bar)
        order=self._new_buy(pair,cohort_id,'SEED','seed',target,bar.close,quantity,decision_at=self.now,capacity=None)
        order['decision_bar']=bar.completed_at; self._save(); return dict(order)

    def policy_repair_evidence(self,pair,*,now,rung=None):
        from .model import RepairEvidence
        e=self.repair_feasibility(pair,now=now,rung=rung)
        mapping={'OWNER_CASH':'REPAIR_OWNER_CASH','RESERVE':'PROTECTED_CASH_FLOOR','PAIR_CAP':'PAIR_BASIS_CAP','TOTAL_CAP':'TOTAL_BASIS_CAP','RECOVERY_CAP':'RECOVERY_BUDGET_CAP'}
        guards=e['guards']; mark=self.state['marks'].get(pair)
        return RepairEvidence(pair=pair,decision_at=int(e['at']),completed_bar_at=int(e['completed_bar'] or 0),cohort_id=e['cohort_id'] or '',next_rung=e['next_rung'] or 0,trigger_price=D(e['trigger'] or '0'),completed_close=D(mark['close']) if mark else ZERO,requested_target=D(e['target'] or '0'),reason=mapping.get(e['reason_code'],e['reason_code']),accounting_healthy=guards['health'],no_pending_action=guards['pending_clear'],mark_fresh=guards['freshness'],prior_bar_liquidity_valid=guards['liquidity_valid'],cohort_profit_allows_fallback=guards['profit_clear'],seed_barrier_complete=guards['seed_complete'])

    def recovery_principal(self):
        return sum((l.cost_basis_remaining_quote for l in self._lots() if self._provenance(l.proposal_id).intent==N.models.IntentKind.RECOVERY),ZERO)

    def recovery_basis(self):
        return sum((l.cost_basis_remaining_quote+l.entry_fees_remaining_quote for l in self._lots() if self._provenance(l.proposal_id).intent==N.models.IntentKind.RECOVERY),ZERO)

    def recovery_psm_cash(self):
        cash=ZERO
        for order in self.state['orders'].values():
            if not order['pool'].startswith('psm:') or order.get('funding_owner')!='repair': continue
            _,gross,fee=self._totals(order)
            cash+=gross-fee if order['side']=='SELL' else -(gross+fee)
        return cash

    def funding_cash(self):
        result={'seed':D('1000'),'repair':D('500'),'protected':D('500')}
        for order in self.state['orders'].values():
            funding=order.get('funding_owner')
            if funding not in {'seed','repair'}:
                if self._receipts(order['id']): raise Blocked('Receipt lost original source funding owner')
                continue
            _,gross,fee=self._totals(order)
            result[funding]+=gross-fee if order['side']=='SELL' else -(gross+fee)
        return result

    @per_call_snapshot
    def capital_commitments(self):
        pools=self.cash_pools()
        ordinary_holds=[o for o in self._pending() if not o['pool'].startswith('psm:')]
        total=self.deployed_basis()+sum((v for p,v in pools.items() if p.startswith('psm:')),ZERO)+sum((self._hold(o) for o in ordinary_holds),ZERO)
        pairs={p:self.deployed_basis(p)+pools.get('psm:'+p,ZERO)+sum((self._hold(o) for o in ordinary_holds if o['pair']==p),ZERO) for p in self.config.rules}
        recovery=self.recovery_basis()+self.recovery_psm_cash()+sum((self._hold(o) for o in ordinary_holds if o.get('funding_owner')=='repair'),ZERO)
        return {'total':total,'pairs':pairs,'recovery':recovery,'native_recovery_principal':self.recovery_principal(),'segregated_recovery_psm_cash':self.recovery_psm_cash()}
