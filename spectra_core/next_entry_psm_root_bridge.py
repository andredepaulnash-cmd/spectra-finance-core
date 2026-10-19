"""Default-off archived-root cash extension, isolated V2.3 research draft.

Archived PSM kernels/journals never change. Post-handback ordinary profits and
base FUND/RETURN legs are verified in the paper receipt transaction domain.
"""
from copy import deepcopy
from dataclasses import asdict
from .next_entry_allocator import CommitProof, ReleaseProof
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path

from .next_entry_funding_bridge import NativeEntryFundingBridge
from .next_entry_profit_bridge import NativeSettledProfitBridge, ProfitBridgeBlocked, _check, _decimal, _digest, _json, _time

ZERO = Decimal(0)


def _exact(function):
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 400
            return function(*args, **kwargs)
    return wrapped

FUNDING_V22_SHA256 = 'bea46fb6054c111da212471dd98a4b217bbf2022c3d0486ed9f0f612d15ea38a'


class NativePSMRootFundingBridge(NativeEntryFundingBridge):
    def __init__(self, *, root_cash_enabled=False, **kwargs):
        self.root_cash_enabled = root_cash_enabled is True
        super().__init__(**kwargs)
        if self.enabled and self.root_cash_enabled:
            with self._paper() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS next_entry_psm_root_cash (
                    trade_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_digest TEXT NOT NULL)''')
                db.execute('''CREATE TABLE IF NOT EXISTS next_entry_terminal_health_scope (
                    entry_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_digest TEXT NOT NULL)''')

    def _extension_identity(self):
        from research.weekly_reversal import psm_root_cash, psm_root_partition
        result = super()._extension_identity()
        _check(hashlib.sha256(Path(__file__).with_name('next_entry_funding_bridge.py').read_bytes()).hexdigest()
               == FUNDING_V22_SHA256, 'Frozen V2.2 funding source changed')
        result.update(implementation='native-psm-root-funding-v2.3', root_cash_enabled=self.root_cash_enabled,
                      root_bridge_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      root_helper=psm_root_cash.identity(), root_partition=psm_root_partition.identity())
        return result

    def _root_enabled(self):
        self._require_funding()
        _check(self.root_cash_enabled, 'Archived root cash funding is disabled')

    def _root_of(self, state, order):
        cohort = state['cohorts'][order['cohort']]
        root = cohort.get('root_cohort_id', cohort['id'])
        original = state['cohorts'][root]
        _check(cohort['pair'] == order['pair'] == original['pair'] and original.get('origin') == 'SEED'
               and original['id'] == f"{order['pair']}:seed:{original['ordinal']:02d}",
               'PSM root is not an original configured seed slot')
        return root

    def _source_pool_proof(self, state, sale_order, acquired_order, trade_id):
        if self.root_cash_enabled and sale_order.get('kind')=='PSM_EXIT':
            from research.weekly_reversal.psm_root_partition import verify_transition
            dedicated=(acquired_order['pool'].startswith('next_entry:') or
                       acquired_order['pool']=='psm:'+acquired_order['pair'] and acquired_order['kind']=='REENTRY')
            funding=self._psm_acquisition_funding(state,acquired_order,trade_id) if dedicated else None
            return verify_transition(state,sale_order,acquired_order,trade_id,acquisition_funding=funding)
        if acquired_order.get('pool') != 'psm:' + acquired_order['pair'] or sale_order.get('kind') == 'PSM_EXIT':
            return super()._source_pool_proof(state, sale_order, acquired_order, trade_id)
        self._root_enabled()
        NativeSettledProfitBridge._source_pool_proof(self, state, sale_order, acquired_order, trade_id)
        from research.weekly_reversal import psm_root_cash
        if acquired_order['kind'] == 'PSM_REBUILD':
            return psm_root_cash.verify_sale(state, sale_order, acquired_order, trade_id)
        _check(acquired_order['kind'] == 'REENTRY' and acquired_order['funding_owner'] == 'seed'
               and sale_order['kind'] == 'COHORT_EXIT' and sale_order['funding_owner'] == 'seed'
               and all(sale_order[k] == acquired_order[k] for k in
                       ('pair','position','proposal','executor','cohort','cohort_generation')),
               'Unsupported retained PSM pool acquisition/owner')
        proof = acquired_order.get('base_entry_proof')
        _check(isinstance(proof, dict) and proof['source_pool'] == proof['entry_pool'] == acquired_order['pool']
               and _decimal(proof['base_entry']['approved_profit_increment']) == ZERO,
               'Retained PSM acquisition lacks exact base-only proof')
        with self._paper() as db:
            self.admission.validate_base_entry(proof, self._state(db)['orders'][acquired_order['id']],
                now=_time(state['now']), replay=True, paper_connection=db)
            original = proof['evidence']['root_source']
            self.validate_root_source_proof(original, now=_time(state['now']), paper_connection=db)
        _check(acquired_order['activation']['proof_sha256'] == proof['proof_sha256']
               and acquired_order['source_owner'] == proof['source_owner']
               and acquired_order['cohort_generation'] == original['latest_generation'] + 1,
               'Retained PSM base activation/root generation changed')
        result = {'schema': 1, 'kind': 'RETAINED_PSM_ROOT_SALE', 'pair': acquired_order['pair'],
                  'root_cohort_id': original['root_cohort_id'], 'funding_owner': 'seed',
                  'source_pool': acquired_order['pool'], 'acquisition_order_id': acquired_order['id'],
                  'sale_order_id': sale_order['id'], 'trade_id': trade_id,
                  'base_entry_proof_sha256': proof['proof_sha256'],
                  'root_source_sha256': original['proof_sha256'],
                  'acquired_generation': acquired_order['cohort_generation']}
        result['proof_sha256'] = _digest(_json(result))
        return result

    def _dedicated_acquisition_proof(self,state,acquired_order,trade_id):
        if acquired_order['pool']=='psm:'+acquired_order['pair']:
            proof=acquired_order.get('base_entry_proof')
            _check(isinstance(proof,dict) and acquired_order['kind']=='REENTRY'
                   and acquired_order['funding_owner']=='seed' and proof['source_pool']==proof['entry_pool']==acquired_order['pool']
                   and _decimal(proof['base_entry']['approved_profit_increment'])==ZERO,
                   'Retained PSM acquisition lacks original zero-profit base admission')
            with self._paper() as db:
                self.admission.validate_base_entry(proof,self._state(db)['orders'][acquired_order['id']],
                    now=_time(state['now']),replay=True,paper_connection=db)
                self.validate_root_source_proof(proof['evidence']['root_source'],now=_time(state['now']),paper_connection=db)
            _check(acquired_order['activation']['proof_sha256']==proof['proof_sha256'],
                   'Retained PSM activation changed')
            return {'schema':2,'kind':'RETAINED_PSM_ROOT_ENTRY','order_id':acquired_order['id'],
                'pair':acquired_order['pair'],'cohort':acquired_order['cohort'],
                'cohort_generation':acquired_order['cohort_generation'],'funding_owner':'seed',
                'entry_pool':acquired_order['pool'],'base_entry_proof_sha256':proof['proof_sha256'],
                'root_source_sha256':proof['evidence']['root_source']['proof_sha256'],
                'source_owner':proof['source_owner']}
        lineage=super()._dedicated_acquisition_proof(state,acquired_order,trade_id)
        if str(acquired_order.get('source_pool','')).startswith('psm:'):
            with self._paper() as db:
                _,proof=self._funding_row(db,acquired_order['entry_id'])
                self.validate_root_source_proof(proof['evidence']['root_source'],now=_time(state['now']),paper_connection=db)
                _check(proof['source_owner']==acquired_order['source_owner'],'Closed PSM-funded source owner changed')
                lineage['psm_root_source']=proof['source_owner']
        return lineage

    def _psm_acquisition_funding(self,state,acquisition,trade_id):
        """Verify only the complete historical funding ancestor, never descendants."""
        lineage=self._dedicated_acquisition_proof(state,acquisition,trade_id)
        with self._paper() as db:
            persisted=self._state(db);actual=persisted['orders'][acquisition['id']]
            _check(actual==acquisition,'PSM root acquisition differs from persisted authority')
            self._clean(db,persisted)
            self._cash_native_coverage(db,persisted)
            self._validate_funding_allocator(db,self.allocator.export_research_state())
            entry_id=(lineage['entry_id'] if lineage['kind']=='CLOSED_SAME_ROOT_ENTRY_POOL' else
                      lineage.get('root_entry_id'))
            if entry_id is not None:
                entry,admission=self._funding_row(db,entry_id)
                _check(entry['status']=='CLOSED','Historical funding ancestor is not CLOSED')
                terminal=self._terminal_payload(entry)
                spent=_decimal(json.loads(terminal['closure_source_payload'])['batch']['total_fee_inclusive_debit'])
                base=_decimal(admission['permit']['base_entry_budget']);profit=_decimal(admission['permit']['approved_profit_increment'])
                transfers=set()
                for kind in ('FUND','RETURN'):
                    row=self._one(db,'next_entry_cash_transfers','transfer_id',self._key('entry-cash-'+kind.lower(),entry_id))
                    _check(_digest(row['payload'])==row['payload_digest'],'Historical cash leg changed')
                    payload=json.loads(row['payload'])
                    expected={'kind':kind,'entry_id':entry_id,'source_pool':admission['source_pool'],
                        'entry_pool':admission['entry_pool'],'base':str(base if kind=='FUND' else base-min(base,spent)),
                        'profit':str(profit if kind=='FUND' else profit-max(ZERO,spent-base)),
                        'admission_sha256':admission['proof_sha256'],
                        'source_evidence':{'reservation_id':entry['reservation_id'],'operation_id':self._key('entry-reserve',entry_id)} if kind=='FUND' else terminal}
                    if admission['source_pool'].startswith('psm:'):expected['source_owner']=admission['source_owner']
                    _check(payload==expected and _json(payload)==row['payload'],'Historical funding source legs differ from exact original budgets')
                    transfers.add((entry_id,kind))
                self._audit_historical_funding_entry(db,persisted,transfers,entry_id)
            activation=actual.get('activation')
            expected=(lineage['admission_sha256'] if lineage['kind']=='CLOSED_SAME_ROOT_ENTRY_POOL' else lineage['base_entry_proof_sha256'])
            _check(isinstance(activation,dict) and activation.get('proof_sha256')==expected,
                   'Historical PSM acquisition activation changed')
            proof={'schema':1,'kind':'VERIFIED_DEDICATED_ACQUISITION','acquisition_order_id':actual['id'],
                'entry_pool':actual['pool'],'pair':actual['pair'],'cohort':actual['cohort'],
                'cohort_generation':actual['cohort_generation'],'funding_owner':actual['funding_owner'],
                'native_position':actual['position'],'proposal_id':actual['proposal'],'executor_id':actual['executor'],
                'activation_sha256':activation['proof_sha256'],'lineage':lineage,'lineage_sha256':_digest(_json(lineage))}
            proof['proof_sha256']=_digest(_json(proof))
            return proof

    def _decorate_source(self, state, source):
        source = super()._decorate_source(state, source)
        sale = state['orders'][source['order']['id']]
        if sale['kind']=='PSM_EXIT' and self.root_cash_enabled:
            from research.weekly_reversal.psm_root_partition import identity
            source['psm_helper_identity']=identity()
            return source
        if sale['pool'] != 'psm:' + sale['pair']:
            return source
        self._root_enabled()
        from research.weekly_reversal import psm_root_cash
        proofs = {}
        for acquired in source['acquisitions']:
            proof = acquired['pool_transition']
            _check(proof and proof['kind'] in {'ARCHIVED_PSM_ROOT_SALE','RETAINED_PSM_ROOT_SALE'},
                   'Ordinary PSM pool profit has no exact root ownership')
            if proof['kind'] == 'ARCHIVED_PSM_ROOT_SALE':
                psm_root_cash.verify_acquisition_receipt(proof, acquired)
            key = acquired['order']['id']
            _check(key not in proofs or proofs[key] == proof, 'Conflicting ordinary PSM root source')
            proofs[key] = proof
        _check(proofs and len({(p['root_cohort_id'],p['funding_owner']) for p in proofs.values()}) == 1,
               'One ordinary native sale cannot combine PSM root/funding owners')
        first = next(iter(proofs.values()))
        source['post_psm_root'] = {'schema': 1, 'root_cohort_id': first['root_cohort_id'],
            'funding_owner': first['funding_owner'], 'pair': sale['pair'],
            'proofs': [proofs[k] for k in sorted(proofs)], 'helper_identity': psm_root_cash.identity()}
        return source

    def _stage_earmark(self, db, source, event, snapshot):
        if self.root_cash_enabled and source.get('psm_ownership'):
            from research.weekly_reversal.psm_root_partition import identity,stage_distribution
            _check(source['psm_helper_identity']==identity(),'Root partition helper source changed')
            state=self._state(db);sale=state['orders'][source['order']['id']]
            for item in source['acquisitions']:
                current=self._source_pool_proof(state,sale,state['orders'][item['order']['id']],source['receipt']['id'])
                _check(current==item['pool_transition'],'Root acquisition source changed before PSM distribution')
            updated=stage_distribution(state,source,event['fee_net_realized_profit'],event['verified_at'],
                                       event['event_id'],event['cash_backing_id'],snapshot['snapshot_id'])
            db.execute('UPDATE state SET value=? WHERE id=1',(_json(updated),))
            return NativeSettledProfitBridge._stage_earmark(self,db,source,event,snapshot)
        root = source.get('post_psm_root')
        if root is not None:
            self._root_enabled()
            state = self._state(db)
            sale = state['orders'][source['order']['id']]
            for acquired in source['acquisitions']:
                current = self._source_pool_proof(state, sale, state['orders'][acquired['order']['id']], source['receipt']['id'])
                _check(current == acquired['pool_transition'], 'Ordinary root source changed before cash distribution')
            amount = _decimal(event['fee_net_realized_profit'])
            balances = self._root_balances(db, state, root['pair'], staged_trade_id=source['receipt']['id'])
            key = root['root_cohort_id'] + '|' + root['funding_owner']
            _check(amount > 0 and balances.get(key, ZERO) >= amount,
                   'Ordinary profit exceeds its own settled PSM root cash')
            payload = {'schema': 1, 'kind': 'POST_HANDBACK_REALIZED_PROFIT', 'trade_id': source['receipt']['id'],
                       'root_cohort_id': root['root_cohort_id'], 'funding_owner': root['funding_owner'],
                       'pair': root['pair'], 'source_pool': sale['pool'], 'amount': str(amount),
                       'source_digest': _digest(_json(source)), 'allocation_event_id': event['event_id'],
                       'cash_backing_id': event['cash_backing_id'], 'snapshot_id': snapshot['snapshot_id']}
            encoded = _json(payload)
            db.execute('INSERT INTO next_entry_psm_root_cash VALUES (?,?,?)',
                       (source['receipt']['id'], encoded, _digest(encoded)))
        super()._stage_earmark(db, source, event, snapshot)

    def _root_adjustments(self, db, state, pair, *, staged_trade_id=None):
        """Read validated signed owner deltas; no archive mutation or pool netting."""
        result = {}
        for row in db.execute('SELECT * FROM next_entry_psm_root_cash ORDER BY trade_id'):
            _check(_digest(row['payload']) == row['payload_digest'], 'Post-handback root journal changed')
            payload = json.loads(row['payload'])
            _check(_json(payload) == row['payload'], 'Noncanonical post-handback root journal')
            outbox = self._one(db, 'next_profit_outbox', 'trade_id', row['trade_id'])
            actual_source,*_=self._verify_source(db,row['trade_id'],_time(state['now']))
            _check(actual_source==outbox['source_payload'], 'Post-handback native source changed')
            source = json.loads(outbox['source_payload'])
            root = source.get('post_psm_root')
            event = json.loads(outbox['event_payload'])
            _check(root is not None and source['receipt']['id'] == row['trade_id']
                   and outbox['source_digest'] == payload['source_digest'] == _digest(outbox['source_payload'])
                   and payload == {'schema': 1,'kind':'POST_HANDBACK_REALIZED_PROFIT','trade_id':row['trade_id'],
                       'root_cohort_id':root['root_cohort_id'],'funding_owner':root['funding_owner'],
                       'pair':root['pair'],'source_pool':source['order']['pool'],
                       'amount':str(_decimal(event['fee_net_realized_profit'])),
                       'source_digest':outbox['source_digest'],'allocation_event_id':event['event_id'],
                       'cash_backing_id':event['cash_backing_id'],'snapshot_id':json.loads(outbox['snapshot_payload'])['snapshot_id']},
                   'Post-handback profit differs from immutable native cash outbox')
            cash = self._one(db,'next_profit_cash','trade_id',row['trade_id'])
            _check(_decimal(payload['amount']) > ZERO and cash['source_pool'] == payload['source_pool']
                   and _decimal(cash['source_debit']) == _decimal(cash['escrow_credit']) == _decimal(payload['amount']),
                   'Post-handback root debit lacks equal physical escrow credit')
            if payload['pair'] == pair:
                key=payload['root_cohort_id']+'|'+payload['funding_owner']
                result[key]=result.get(key,ZERO)-_decimal(payload['amount'])
        for row in db.execute('SELECT * FROM next_profit_outbox WHERE event_payload IS NOT NULL'):
            source=json.loads(row['source_payload'])
            if source.get('post_psm_root') and row['trade_id'] != staged_trade_id:
                _check(db.execute('SELECT 1 FROM next_entry_psm_root_cash WHERE trade_id=?',(row['trade_id'],)).fetchone(),
                       'Ordinary root profit lacks its atomic subowner journal')
        for row in db.execute('SELECT * FROM next_entry_cash_transfers'):
            _check(_digest(row['payload']) == row['payload_digest'], 'Root cash transfer digest changed')
            payload=json.loads(row['payload'])
            if not str(payload['source_pool']).startswith('psm:'):
                continue
            _, proof=self._funding_row(db,row['entry_id'])
            owner=proof.get('source_owner')
            _check(isinstance(owner,dict) and payload.get('source_owner') == owner
                   and owner['source_pool'] == payload['source_pool'] == 'psm:'+owner['pair']
                   and owner['funding_owner'] == 'seed' and owner['root_cohort_id'] == proof['evidence']['root_source']['root_cohort_id']
                   and owner['ownership_proof_sha256'] == proof['evidence']['root_source']['proof_sha256'],
                   'PSM entry transfer lost exact root/seed source proof')
            _check(payload['kind'] in {'FUND','RETURN'} and payload['entry_id']==row['entry_id']
                   and row['transfer_id']==self._key('entry-cash-'+payload['kind'].lower(),row['entry_id'])
                   and payload['admission_sha256']==proof['proof_sha256']
                   and payload['entry_pool']==proof['entry_pool'], 'Unknown or changed root cash transfer kind')
            self.validate_root_source_proof(proof['evidence']['root_source'],now=_time(state['now']),paper_connection=db)
            base=_decimal(proof['permit']['base_entry_budget'])
            profit=_decimal(proof['permit']['approved_profit_increment'])
            if payload['kind']=='RETURN':
                entry,_=self._funding_row(db,row['entry_id']);terminal=self._terminal_payload(entry)
                spent=_decimal(json.loads(terminal['closure_source_payload'])['batch']['total_fee_inclusive_debit'])
                base-=min(base,spent);profit-=max(ZERO,spent-_decimal(proof['permit']['base_entry_budget']))
                _check(payload['source_evidence']==terminal, 'Root cash return source changed')
            else:
                entry,_=self._funding_row(db,row['entry_id'])
                _check(payload['source_evidence']=={'reservation_id':entry['reservation_id'],
                    'operation_id':self._key('entry-reserve',row['entry_id'])}, 'Root funding source changed')
            _check(_decimal(payload['base'])==base and _decimal(payload['profit'])==profit,
                   'Root funding amount differs from original authorized/unused cash')
            if owner['pair'] == pair:
                key=owner['root_cohort_id']+'|seed'
                amount=_decimal(payload['base'])
                _check(amount>=ZERO, 'Negative root cash transfer')
                result[key]=result.get(key,ZERO)+(-amount if payload['kind']=='FUND' else amount)
        return result

    def _root_balances(self, db, state, pair, *, staged_trade_id=None):
        from research.weekly_reversal.psm_profit_partition import distribution_debits
        result={}
        for row in db.execute('SELECT * FROM receipts'):
            order=state['orders'][row['order_id']]
            if order['pool']!='psm:'+pair:
                continue
            _check(row['applied']==1, 'Unapplied PSM root cash receipt')
            raw=json.loads(row['payload'])
            _check(raw['order_id']==order['id'] and raw['trade_id']==row['id'], 'Unbound root cash receipt')
            funding=order.get('funding_owner')
            _check(funding in {'seed','repair'}, 'Unknown root cash funding owner')
            key=self._root_of(state,order)+'|'+funding
            gross=_decimal(raw['amount'])*_decimal(raw['price']);fee=_decimal(raw['fee_paid'])
            result[key]=result.get(key,ZERO)+(gross-fee if order['side']=='SELL' else -(gross+fee))
        for key,value in distribution_debits(state,pair).items():
            result[key]=result.get(key,ZERO)-value
        for key,value in self._root_adjustments(db,state,pair,staged_trade_id=staged_trade_id).items():
            result[key]=result.get(key,ZERO)+value
        _check(all(v>=ZERO for v in result.values()), 'One PSM root/funding owner subsidizes another')
        return result

    def psm_funding_cash_adjustments(self, pair):
        if not self.root_cash_enabled:
            return {}
        with localcontext() as ctx, self._paper() as db:
            ctx.prec=400
            state=self._state(db)
            self._cash_native_coverage(db,state)
            self._validate_funding_allocator(db,self.allocator.export_research_state())
            return self._root_adjustments(db,state,pair)

    def _funding_projection(self, db, state, pools, escrow):
        result,escrow=super()._funding_projection(db,state,pools,escrow)
        if self.root_cash_enabled:
            terminal_ids={r[0] for r in db.execute('SELECT entry_id FROM next_entry_funding WHERE terminal_payload IS NOT NULL')}
            _check({r[0] for r in db.execute('SELECT entry_id FROM next_entry_terminal_health_scope')}==terminal_ids,
                   'Missing or orphan terminal native membership snapshot')
            for pair in state.get('psm',{}):
                balance=self._root_balances(db,state,pair)
                _check(sum(balance.values(),ZERO)==result.get('psm:'+pair,ZERO),
                       'Root/funding subcash does not conserve the physical PSM pool')
        return result,escrow

    def _transfer(self, db, entry_id, kind, proof, base, profit, source_evidence):
        if self.root_cash_enabled and kind == 'RETURN':
            self._freeze_terminal_health_scope(db, entry_id, source_evidence)
        if not proof['source_pool'].startswith('psm:'):
            return super()._transfer(db,entry_id,kind,proof,base,profit,source_evidence)
        self._root_enabled()
        owner=proof.get('source_owner')
        _check(isinstance(owner,dict) and owner['funding_owner']=='seed'
               and owner['source_pool']==proof['source_pool']=='psm:'+owner['pair'], 'Missing exact root cash owner')
        self.validate_root_source_proof(proof['evidence']['root_source'],now=_time(self._state(db)['now']),paper_connection=db)
        old=db.execute('SELECT payload FROM next_entry_cash_transfers WHERE transfer_id=?',
                       (self._key('entry-cash-'+kind.lower(),entry_id),)).fetchone()
        if kind=='FUND' and old is None:
            balance=self._root_balances(db,self._state(db),owner['pair'])
            _check(balance.get(owner['root_cohort_id']+'|seed',ZERO)>=base, 'Exact PSM root seed cash insufficient')
        payload={'kind':kind,'entry_id':entry_id,'source_pool':proof['source_pool'],'entry_pool':proof['entry_pool'],
                 'base':str(base),'profit':str(profit),'admission_sha256':proof['proof_sha256'],
                 'source_evidence':source_evidence,'source_owner':owner}
        encoded=_json(payload)
        _check(base>=ZERO and profit>=ZERO and (old is None or old[0]==encoded), 'Changed root funding transfer')
        db.execute('INSERT OR IGNORE INTO next_entry_cash_transfers VALUES (?,?,?,?)',
                   (self._key('entry-cash-'+kind.lower(),entry_id),entry_id,encoded,_digest(encoded)))

    def _root_components(self, db, state, root_id, *, acquisition_ids=None, disposal_ids=None):
        """Re-read immutable native history, optionally only a frozen predecessor set."""
        from research.weekly_reversal import psm_root_cash
        root=state['cohorts'][root_id];pair=root['pair']
        _check(root.get('origin')=='SEED' and root_id==f"{pair}:seed:{root['ordinal']:02d}",
               'Only an original configured partial root may own source cash')
        receipts={row['id']:dict(row) for row in db.execute('SELECT * FROM receipts')}
        root_orders={o['id']:o for o in state['orders'].values()
                     if o['pair']==pair and self._root_of(state,o)==root_id}
        if acquisition_ids is None:
            acquisition_ids=sorted(t for t,r in receipts.items() if r['order_id'] in root_orders
                                   and root_orders[r['order_id']]['side']=='BUY')
        if disposal_ids is None:
            disposal_ids=sorted(t for t,r in receipts.items() if r['order_id'] in root_orders
                                and root_orders[r['order_id']]['side']=='SELL')
        acquisitions=[];disposals=[];episodes={}
        with self._native() as native:
            for trade_id in acquisition_ids:
                receipt=receipts[trade_id];order=root_orders[receipt['order_id']]
                _check(order['side']=='BUY' and receipt['applied']==1
                       and order['status']==order['terminal'] and order['terminal'] in {'FILLED','CANCELED'},
                       'Root predecessor acquisition is not exact and terminal')
                reconciled=self._one(native,'economic_trade_fill_reconciliation','trade_id',trade_id)
                lot=self._one(native,'economic_lots','lot_id','trade-fill:'+trade_id)
                original_lot={k:lot[k] for k in ('lot_id','position_id','pair','ownership','source','base_original',
                    'cost_basis_original_quote','entry_fees_original_quote','proposal_id','executor_id','created_at')}
                ownership=None
                if order['kind']=='PSM_REBUILD':
                    ownership=psm_root_cash.acquisition(state,order)
                    psm_root_cash.verify_acquisition_receipt(ownership,{'receipt':receipt})
                    generation=ownership['acquired_generation']
                    archive=ownership['archive'];position=archive['position']
                    audits=[dict(r) for r in db.execute("SELECT * FROM audit_events WHERE kind='psm_episode_handback'")
                            if json.loads(r['payload'])=={'pair':pair,'episode':archive['episode']}]
                    _check(len(audits)==1 and _time(audits[0]['at'])==_time(archive['completed_at']),
                           'Archived root handback lacks exact paper audit')
                    evidence={'archive':archive,'handback_audit':audits[0]}
                    _check(position not in episodes or episodes[position]==evidence, 'Conflicting root archive evidence')
                    episodes[position]=evidence
                else:
                    generation=order.get('cohort_generation')
                _check(type(generation) is int and generation>0, 'Root acquisition generation missing')
                binding=self._one(native,'economic_fill_bindings','order_id',order['id'])
                acquisitions.append({'trade_id':trade_id,'order_id':order['id'],'generation':generation,
                    'cohort_id':order['cohort'],'funding_owner':order['funding_owner'],'pool':order['pool'],
                    'order':{k:order.get(k) for k in ('id','pair','cohort','cohort_generation','kind','side','pool',
                        'funding_owner','intent','position','proposal','executor','quantity','decision_at','created_at',
                        'terminal','psm_order','psm_position','psm_root_cohort','source_owner')},
                    'receipt':receipt,'native_reconciliation':reconciled,'original_lot':original_lot,
                    'native_binding':binding,'native_provenance':self._one(native,'execution_provenance','proposal_id',order['proposal']),
                    'native_execution':self._one(native,'execution_receipts','proposal_id',order['proposal']),
                    'ownership':ownership})
            for trade_id in disposal_ids:
                receipt=receipts[trade_id];order=root_orders[receipt['order_id']]
                _check(order['side']=='SELL' and receipt['applied']==1, 'Root disposal changed source')
                outbox=self._one(db,'next_profit_outbox','trade_id',trade_id)
                source,*_=self._verify_source(db,trade_id,_time(state['now']))
                _check(source==outbox['source_payload'] and _digest(source)==outbox['source_digest']
                       and outbox['status'] in {'ACKNOWLEDGED','INELIGIBLE'},
                       'Root sale has unresolved or changed settled profit evidence')
                parsed=json.loads(source)
                disposals.append({'trade_id':trade_id,'order_id':order['id'],'cohort_id':order['cohort'],
                    'source_digest':outbox['source_digest'],'source_payload':source,
                    'terminal_at':parsed['terminal']['at']})
        return acquisitions,disposals,[episodes[k] for k in sorted(episodes)]

    @_exact
    def root_source_proof(self, root_cohort_id, *, now, paper_connection):
        self._root_enabled()
        db=paper_connection
        state=self.admission._state(db,None)
        _check(_time(state['now'])==_time(now),'Root source requires current paper clock')
        self._clean(db,state)
        self._cash_native_coverage(db,state)
        self._validate_funding_allocator(db,self.allocator.export_research_state())
        acquisitions,disposals,episodes=self._root_components(db,state,root_cohort_id)
        _check(acquisitions and disposals and episodes,'Latest root lacks actual PSM acquisition/disposal history')
        latest=max(a['generation'] for a in acquisitions)
        latest_seed=[a for a in acquisitions if a['generation']==latest and a['funding_owner']=='seed']
        pair=state['cohorts'][root_cohort_id]['pair']
        _check(latest_seed and all(a['pool']=='psm:'+pair and a['order']['kind'] in {'PSM_REBUILD','REENTRY'}
                                 for a in latest_seed), 'Latest root principal is not owned PSM seed subcash')
        with self._native() as native:
            for item in acquisitions:
                lot=self._one(native,'economic_lots','lot_id',item['original_lot']['lot_id'])
                _check(_decimal(lot['base_remaining'])==ZERO,'Latest root still owns native inventory or dust')
        latest_lots={a['original_lot']['lot_id'] for a in acquisitions if a['generation']==latest}
        latest_disposals=[d for d in disposals if latest_lots &
                          {a['lot_id'] for a in json.loads(d['source_payload'])['allocations']}]
        _check(latest_disposals,'Missing actual latest-root terminal disposals')
        latest_sold_at=max(_time(d['terminal_at']) for d in latest_disposals)
        proof={'schema':1,'kind':'COMPLETED_PSM_ROOT_DISPOSAL','root_cohort_id':root_cohort_id,'pair':pair,
               'latest_generation':latest,'latest_cohort_ids':sorted({a['cohort_id'] for a in latest_seed}),
               'source_pool':'psm:'+pair,'source_subowner':root_cohort_id+'|seed',
               'latest_sold_at':latest_sold_at,'acquisitions':acquisitions,'disposals':disposals,
               'completed_episodes':episodes}
        proof['proof_sha256']=_digest(_json(proof))
        owner={'schema':1,'pair':pair,'root_cohort_id':root_cohort_id,'funding_owner':'seed',
               'source_pool':'psm:'+pair,'latest_generation':latest,'ownership_proof_sha256':proof['proof_sha256']}
        balances=self._root_balances(db,state,pair)
        return {'source_owner':owner,'ownership_proof':proof,
                'available_base_cash':balances.get(root_cohort_id+'|seed',ZERO)}

    @_exact
    def validate_root_source_proof(self,ownership_proof,*,now,paper_connection):
        signature=ownership_proof.get('proof_sha256') if isinstance(ownership_proof,dict) else None
        active=getattr(self,'_root_proof_active',set())
        _check(signature is not None and signature not in active,'Cyclic or malformed root source ancestry')
        self._root_proof_active=active
        active.add(signature)
        try:
            return self._validate_root_source_proof(ownership_proof,now=now,paper_connection=paper_connection)
        finally:
            active.remove(signature)

    def _validate_root_source_proof(self, ownership_proof, *, now, paper_connection):
        self._root_enabled()
        frozen=deepcopy(ownership_proof);signature=frozen.pop('proof_sha256',None)
        _check(signature==_digest(_json(frozen)) and frozen['schema']==1
               and frozen['kind']=='COMPLETED_PSM_ROOT_DISPOSAL','Frozen root source identity changed')
        db=paper_connection;state=self.admission._state(db,None)
        _check(_time(state['now'])<=_time(now),'Future frozen root verification')
        acquisitions,disposals,episodes=self._root_components(db,state,frozen['root_cohort_id'],
            acquisition_ids=[a['trade_id'] for a in frozen['acquisitions']],
            disposal_ids=[d['trade_id'] for d in frozen['disposals']])
        _check(acquisitions==frozen['acquisitions'] and disposals==frozen['disposals']
               and episodes==frozen['completed_episodes'],'Frozen original root source evidence changed')
        latest=max(a['generation'] for a in acquisitions)
        latest_lots={a['original_lot']['lot_id'] for a in acquisitions if a['generation']==latest}
        latest_sales=[d for d in disposals if latest_lots &
                      {a['lot_id'] for a in json.loads(d['source_payload'])['allocations']}]
        _check(frozen['latest_generation']==latest and frozen['latest_cohort_ids']==sorted(
               {a['cohort_id'] for a in acquisitions if a['generation']==latest and a['funding_owner']=='seed'})
               and latest_sales and frozen['latest_sold_at']==max(_time(d['terminal_at']) for d in latest_sales)
               and frozen['source_pool']=='psm:'+frozen['pair']
               and frozen['source_subowner']==frozen['root_cohort_id']+'|seed',
               'Frozen latest-root facts changed')
        return deepcopy(ownership_proof)

    def _freeze_terminal_health_scope(self, db, entry_id, terminal):
        """Freeze actual closure membership before a later same-clock authority.

        The native health row is mutable and timestamps are not sequence numbers.
        This snapshot is written atomically with RETURN; its digest is part of the
        original terminal cash evidence and every subsequent allocator release.
        """
        old=db.execute('SELECT * FROM next_entry_terminal_health_scope WHERE entry_id=?',(entry_id,)).fetchone()
        if old is not None:
            _check(terminal.get('health_scope_sha256')==old['payload_digest']
                   and _digest(old['payload'])==old['payload_digest'], 'Frozen closure membership changed')
            return
        closure=json.loads(terminal['closure_source_payload']);context=closure['context']
        state=self._state(db);position=context['position_id']
        with self._native() as native:
            bindings=[dict(r) for r in native.execute(
                'SELECT * FROM economic_fill_bindings WHERE position_id=? ORDER BY order_id',(position,))]
            fills=[dict(r) for r in native.execute(
                'SELECT * FROM economic_trade_fill_reconciliation WHERE position_id=? ORDER BY trade_id',(position,))]
            _check(self._one(native,'economic_accounting_health','position_id',position)==closure['native_health'],
                   'Closure native health changed before durable membership freeze')
        order_ids={b['order_id'] for b in bindings}
        orders=[{k:state['orders'][oid][k] for k in
                 ('id','pair','position','proposal','executor','side','created_at')}
                for oid in sorted(order_ids)]
        audits=[dict(r) for r in db.execute('SELECT * FROM audit_events ORDER BY seq')]
        _check(audits, 'Closure membership lacks durable paper audit boundary')
        scope={'schema':1,'entry_id':entry_id,'closure_source_digest':terminal['closure_source_digest'],
               'bindings':bindings,'fills':fills,'orders':orders,'audit_max_seq':audits[-1]['seq'],
               'audit_prefix_sha256':_digest(_json(audits))}
        payload=_json(scope);signature=_digest(payload)
        terminal['health_scope_sha256']=signature
        db.execute('INSERT INTO next_entry_terminal_health_scope VALUES (?,?,?)',(entry_id,payload,signature))
        db.execute('UPDATE next_entry_funding SET terminal_payload=? WHERE entry_id=?',(_json(terminal),entry_id))

    def _audit_terminal_authority(self, db, state, context, terminal):
        if not self.root_cash_enabled:
            return super()._audit_terminal_authority(db,state,context,terminal)
        closure=json.loads(terminal['closure_source_payload']);health=closure['native_health']
        verified=_time(terminal['verified_at']);ended=_time(terminal['terminal_at'])
        minimum=terminal['minimum_verified_after']
        _check(health['position_id']==context.position_id and health['recorded_state']=='HEALTHY'
               and json.loads(health['unresolved_order_ids_json'])==[]
               and json.loads(health['unresolved_trade_ids_json'])==[]
               and _time(health['last_success_at'])==_time(health['last_attempt_at'])==verified
               and context.reserved_at<=ended<=verified<=_time(state['now'])
               and (minimum is None or verified>_time(minimum)), 'Invalid frozen native terminal health')
        row=self._one(db,'next_entry_terminal_health_scope','entry_id',context.entry_id)
        _check(_digest(row['payload'])==row['payload_digest']==terminal.get('health_scope_sha256'),
               'Frozen closure membership digest changed')
        scope=json.loads(row['payload'])
        _check(_json(scope)==row['payload'] and scope['schema']==1 and scope['entry_id']==context.entry_id
               and scope['closure_source_digest']==terminal['closure_source_digest'],
               'Frozen closure membership identity changed')
        audits=[dict(r) for r in db.execute('SELECT * FROM audit_events WHERE seq<=? ORDER BY seq',(scope['audit_max_seq'],))]
        _check(audits and audits[-1]['seq']==scope['audit_max_seq']
               and _digest(_json(audits))==scope['audit_prefix_sha256'], 'Closure causal paper audit prefix changed')
        later=[dict(r) for r in db.execute('SELECT * FROM audit_events WHERE seq>? ORDER BY seq',(scope['audit_max_seq'],))]
        bindings=scope['bindings'];fills=scope['fills'];orders=scope['orders']
        ids={o['id'] for o in orders};proposals={o['proposal'] for o in orders}
        _check(len(ids)==len(orders) and context.order_id in ids
               and bindings==sorted(bindings,key=lambda b:b['order_id'])
               and len(bindings)==len(ids) and {b['order_id'] for b in bindings}==ids
               and len({f['trade_id'] for f in fills})==len(fills)
               and fills==sorted(fills,key=lambda f:f['trade_id'])
               and all(f['order_id'] in ids for f in fills)
               and health['proposal_count']==health['fully_reconciled_proposals']==len(proposals)>0
               and health['known_orders']==health['bound_orders']==len(ids)
               and health['confirmed_fills']==health['ledgered_fills']==len(fills),
               'Frozen native closure scope counts changed')
        for order in orders:
            _check({k:state['orders'][order['id']][k] for k in order}==order,
                   'Frozen closure order identity changed')
        with self._native() as native:
            authority={'binding':self._one(native,'economic_fill_bindings','order_id',context.order_id),
                'provenance':self._one(native,'execution_provenance','proposal_id',context.proposal_id),
                'execution':self._one(native,'execution_receipts','proposal_id',context.proposal_id),
                'reservation':self._one(native,'capital_reservations','proposal_id',context.proposal_id)}
            authority['reservation']={key:authority['reservation'][key] for key in
                ('proposal_id','pair','intent','amount_quote','leverage','created_at')}
            _check(authority==closure['native_authority'], 'Frozen terminal native authority changed')
            _check(_time(authority['reservation']['created_at'])<=_time(authority['provenance']['created_at'])
                   ==_time(authority['execution']['created_at'])<=_time(authority['binding']['created_at'])<=ended
                   and _time(state['orders'][context.order_id]['created_at'])<=_time(authority['binding']['created_at']),
                   'Terminal native authority chronology changed')
            current_bindings=[dict(r) for r in native.execute(
                'SELECT * FROM economic_fill_bindings WHERE position_id=?',(context.position_id,))]
            current_fills=[dict(r) for r in native.execute(
                'SELECT * FROM economic_trade_fill_reconciliation WHERE position_id=?',(context.position_id,))]
            for binding in bindings:
                order=state['orders'][binding['order_id']]
                _check(binding in current_bindings and _time(binding['created_at'])<=verified
                       and binding['proposal_id']==order['proposal'] and binding['executor_id']==order['executor']
                       and binding['pair']==order['pair'] and binding['account_name']==context.account_id
                       and binding['connector_name']==context.connector, 'Frozen closure native binding changed')
            for fill in fills:
                _check(fill in current_fills and _time(fill['reconciled_at'])<=verified,
                       'Frozen closure native reconciliation changed')
            for binding in current_bindings:
                if binding['order_id'] in ids:continue
                at=_time(binding['created_at'])
                _check(at>=verified, 'Unexplained older native binding absent from frozen closure')
                if at==verified:
                    oid=binding['order_id']
                    anchors=[r for r in later if r['kind'] in {'buy_authorized','cohort_sell_intent','psm_sell_batch_sealed'}
                             and (json.loads(r['payload']).get('id')==oid
                                  or oid in json.loads(r['payload']).get('children',[]))]
                    _check(len(anchors)==1 and _time(anchors[0]['at'])==at,
                           'Same-clock native binding lacks later causal admission audit')
            frozen_trades={f['trade_id'] for f in fills}
            for fill in current_fills:
                if fill['trade_id'] in frozen_trades:continue
                at=_time(fill['reconciled_at'])
                _check(at>=verified, 'Unexplained older native fill absent from frozen closure')
                if at==verified:
                    anchors=[r for r in later if r['kind']=='receipt_reconciled'
                             and json.loads(r['payload'])=={'trade_id':fill['trade_id']}]
                    _check(len(anchors)==1 and _time(anchors[0]['at'])==at,
                           'Same-clock native fill lacks later causal receipt audit')

    def _audit_historical_funding_entry(self, db, state, transfers, historical_entry_id):
        """Re-derive every debit and closed cash return, including acknowledged rows.

        Hashes detect accidental byte changes; native receipts and deterministic
        attribution establish authority. An internally rehashed journal is not a
        substitute for its original paper/native source.
        """
        ledger = self.allocator.export_research_state()
        now = _time(state["now"])
        known_entries = {row[0] for row in db.execute("SELECT entry_id FROM next_entry_funding")}
        for table in ("next_entry_funding_outbox", "next_entry_cash_transfers",
                      "next_entry_execution_contexts", "next_entry_observed_receipts"):
            _check({row[0] for row in db.execute("SELECT DISTINCT entry_id FROM " + table)} <= known_entries,
                   "Orphan funding journal evidence")
        _check(historical_entry_id in known_entries, "Missing historical funding source")
        for entry_id in (historical_entry_id,):
            entry, admission = self._funding_row(db, entry_id)
            accepted = ledger["reservations"].get(entry["reservation_id"])
            operations = [self._outbox(db, row[0]) for row in db.execute(
                "SELECT operation_id FROM next_entry_funding_outbox WHERE entry_id=? ORDER BY rowid", (entry_id,))]
            reserves = [(row, payload) for row, payload in operations if row["action"] == "RESERVE"]
            _check(len(reserves) == 1 and operations[0] == reserves[0], "Entry lacks unique first reserve operation")
            reserve, payload = reserves[0]
            _check(reserve["operation_id"] == self._key("entry-reserve", entry_id)
                   and payload == {"snapshot_id": admission["snapshot_id"],
                       "reservation_id": entry["reservation_id"], "permit": admission["permit"]},
                   "Reserve operation identity changed")
            if accepted is None:
                _check(entry["status"] == "PREPARED" and len(operations) == 1
                       and reserve["status"] == "PREPARED" and not any(item[0] == entry_id for item in transfers),
                       "Cash funded without allocator reservation")
                continue
            # Replay validation never grants a new order or refreshes a permit.
            self.admission.validate(admission, state["orders"][entry["order_id"]], now=now,
                                    replay=True, reconciliation_only=True, paper_connection=db)
            _check(reserve["status"] != "ACKNOWLEDGED" or
                   {k: v for k, v in json.loads(reserve["acknowledgement"]).items() if k not in {"operation_id", "sequence"}} == accepted["reserve_receipt"],
                   "Reserve acknowledgement changed")
            _check((entry["status"] == "PREPARED") == ((entry_id, "FUND") not in transfers),
                   "Funding state and cash legs disagree")
            if entry["status"] == "PREPARED":
                _check(len(operations) == 1, "Unfunded entry has spending operations")
                continue
            if state["orders"][entry["order_id"]]["status"] == "FUNDING_PREPARED":
                _check(len(operations) == 1 and entry["status"] == "FUNDED"
                       and db.execute("SELECT 1 FROM receipts WHERE order_id=?", (entry["order_id"],)).fetchone() is None
                       and db.execute("SELECT 1 FROM next_entry_execution_contexts WHERE entry_id=?", (entry_id,)).fetchone() is None
                       and entry["terminal_payload"] is None and (entry_id, "RETURN") not in transfers,
                       "Non-executable prepared entry acquired cash evidence")
                continue
            context, batch = self._verified_batch(db, entry, admission, now)
            actual = {json.loads(item)["settlement_id"]: item for item in batch.receipts}
            order_ids = list(batch.settlement_ids)
            consumed = []
            used_base = used_profit = ZERO
            releases = []
            permit = admission["permit"]
            base_budget = _decimal(permit["base_entry_budget"])
            for operation, payload in operations[1:]:
                if operation["action"] == "RELEASE":
                    releases.append((operation, payload))
                    continue
                _check(operation["action"] == "COMMIT" and not releases, "Invalid funding operation ordering")
                facts = payload["receipt_payloads"]
                ids = payload["proof"]["settlement_ids"]
                _check(ids == sorted(set(ids)) and set(ids) == set(facts)
                       and not (set(ids) & set(consumed))
                       and all(actual.get(key) == value for key, value in facts.items()),
                       "Commit native source coverage changed")
                consumed.extend(key for key in order_ids if key in facts)
                _check(consumed == order_ids[:len(consumed)], "Commit skipped earlier native debit")
                spent = sum((_decimal(json.loads(actual[key])["fee_inclusive_debit"]) for key in consumed), ZERO)
                base, profit = min(base_budget, spent), max(ZERO, spent-base_budget)
                raw = payload["proof"]
                verified_at = _time(raw["verified_at"])
                expected = asdict(CommitProof(
                    proof_id=self._key("entry-debit-proof", entry_id, sorted(ids)),
                    native_reservation_id=permit["native_reservation_id"], entry_id=entry_id,
                    account_id=self.account_id, currency="USD", allocation_id=permit["allocation_id"], owner_id=permit["owner_id"],
                    settlement_ids=tuple(sorted(ids)), profit_cash_used=profit-used_profit,
                    base_cash_used=base-used_base, total_fee_inclusive_cash_debit=spent-used_base-used_profit,
                    settled_at=max(json.loads(value)["reconciled_at"] for value in facts.values()),
                    verified_at=verified_at, expires_at=verified_at+self.allocator.policy.max_proof_age_seconds,
                    confirmed=True, settled=True, verified=True))
                _check(_json(raw) == _json(expected) and profit > used_profit
                       and expected["settled_at"] <= verified_at <= now
                       and operation["operation_id"] == self._key("entry-commit", entry_id, sorted(ids)),
                       "Commit attribution is not the exact base-first native debit")
                used_base, used_profit = base, profit
            _check(len(releases) <= 1, "Duplicate terminal release")
            if entry["terminal_payload"] is not None:
                terminal = self._terminal_payload(entry)
                closure = json.loads(terminal["closure_source_payload"])
                _check(batch.source_payload == terminal["batch_source_payload"]
                       and closure["context"] == context.as_dict()
                       and state["orders"][entry["order_id"]]["status"] == terminal["terminal_status"]
                       == state["orders"][entry["order_id"]]["terminal"], "Closed entry source or status changed")
                self._audit_terminal_authority(db, state, context, terminal)
                # Immutable terminal/audit rows are replayed without requiring a
                # fresh watermark forever after a legitimate completed closure.
                terminal_audit = closure["terminal_audit"]
                _check(dict(db.execute("SELECT * FROM audit_events WHERE seq=?", (terminal_audit["seq"],)).fetchone() or {})
                       == terminal_audit, "Frozen terminal audit changed")
                for audit in closure["receipt_audits"]:
                    _check(dict(db.execute("SELECT * FROM audit_events WHERE seq=?", (audit["seq"],)).fetchone() or {}) == audit,
                           "Frozen applied receipt audit changed")
                _check((entry_id, "RETURN") in transfers and entry["status"] in {"CLOSING", "CLOSED"},
                       "Terminal cash return or state missing")
                total_profit = max(ZERO, batch.total_fee_inclusive_debit-base_budget)
                _check(used_profit == total_profit, "Terminal actual profit debit not committed")
                left = _decimal(permit["approved_profit_increment"])-total_profit
                _check(bool(releases) == (left > 0), "Unused profit release coverage mismatch")
                if releases:
                    operation, payload = releases[0]
                    expected = asdict(ReleaseProof(
                        proof_id=self._key("entry-release-proof", entry_id, terminal["closure_source_digest"]),
                        native_reservation_id=permit["native_reservation_id"], entry_id=entry_id,
                        account_id=self.account_id, currency="USD", allocation_id=permit["allocation_id"], owner_id=permit["owner_id"],
                        verified_at=terminal["verified_at"],
                        expires_at=terminal["verified_at"]+self.allocator.policy.max_proof_age_seconds,
                        native_reservation_closed=True, no_unreconciled_fills=True,
                        no_remaining_order_liability=True, verified=True))
                    _check(payload == {**terminal, "proof": expected}
                           and operation["operation_id"] == self._key("entry-release", entry_id),
                           "Release differs from exact frozen terminal evidence")
                _check(entry["status"] != "CLOSED" or
                       all(op["status"] == "ACKNOWLEDGED" for op, _ in operations),
                       "Closed entry has unknown allocator outcome")
            else:
                _check(entry["status"] == "FUNDED" and (entry_id, "RETURN") not in transfers and not releases,
                       "Nonterminal entry attempted a cash return")

