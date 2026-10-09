"""Independent ledger reconstruction of saved completed parked-cash arms.

Reads small outputs only. Does not execute the strategy or hydrate a checkpoint.
"""
from collections import Counter
from decimal import Decimal as D, getcontext
from pathlib import Path
import argparse
import csv
import json
import hashlib

getcontext().prec=60
ZERO=D(0); TOL=D('1e-18')

def load(path): return json.loads(Path(path).read_text())
def close(a,b,label):
    if abs(D(a)-D(b))>TOL: raise ArithmeticError(f'{label}: {a} differs from {b}')
def decimal(row,key):return D(row[key])
def native_lot(lot):
    return dict(lot_id=lot['lot_id'],parent=str(lot['position_id']),pair=lot['pair'],
        kind='RECOVERY' if str(lot['source']).endswith('VCAP_RECOVERY') else 'ENTRY',
        quantity=decimal(lot,'base_remaining'),basis=decimal(lot,'cost_basis_remaining_quote')+decimal(lot,'entry_fees_remaining_quote'),parked=ZERO)

def audit(directory,protocol,prepared=None,partial=False):
    directory=Path(directory)
    aliases={'new_fills.json':'partial_new_fills.json','release_events.json':'partial_release_events.json',
        'terminal_release.json':'partial_release.json','terminal_active_lots.json':'partial_active_lots.json',
        'terminal_positions.json':'partial_positions.json'} if partial else {}
    record_path=lambda name:directory/aliases.get(name,name)
    expected_frames=protocol['frames']
    saved_result=(directory/'result.json').exists()
    if partial:
        failure=load(directory/'failure_receipt.json')
        if failure['exception']!='TimeoutError' or not failure['no_performance_claim']:
            raise ArithmeticError('Partial source audit requires expected operational timeout')
        expected_frames=failure['completed_frames']
        saved_rows=list(csv.DictReader((directory/'window_accounts.csv').open()))
        if len(saved_rows)!=expected_frames:raise ArithmeticError('Incomplete partial evidence rows')
        last=saved_rows[-1]
        start=load(directory/'hydration_receipt.json')['initial_boundary_audit']['observed']
        # Internal observed terminal scalars, not a completed strategy result.
        result=dict(arm=failure['arm'],ending_cash=last['cash'],parked_cash=last['parked'],
            open_basis=last['committed_basis'],new_realized_profit=str(D(last['realized'])-D(start['realized_pnl'])),
            marked_inventory=last['marked_inventory'],ending_equity=last['equity'])
    elif saved_result:

        result=load(directory/'result.json')
        if result['status']!='PASS':raise ArithmeticError('Incomplete or failing result')
    else:
        # A completed OFF had a reporting-only AttributeError after preserving
        # every fill/row/terminal file and a passing exact archived control audit.
        # Audit saved actual outputs directly; never rerun or invent execution.
        control=load(directory/'control_archive_audit.json')
        if not control['passed']:raise ArithmeticError('Missing completed summary and no proven OFF control')
        rows=list(csv.DictReader((directory/'window_accounts.csv').open()))
        if len(rows)!=protocol['frames']:raise ArithmeticError('Incomplete saved OFF rows')
        last=rows[-1]
        start=load(directory/'hydration_receipt.json')['initial_boundary_audit']['observed']
        result=dict(status='PASS',arm='OFF',ending_cash=last['cash'],parked_cash=last['parked'],
            open_basis=last['committed_basis'],new_realized_profit=str(D(last['realized'])-D(start['realized_pnl'])),
            marked_inventory=last['marked_inventory'],ending_equity=last['equity'])
    arm=result['arm'];arm_spec=next(x for x in protocol['arms'] if x['name']==arm)
    cap=D(arm_spec['outstanding_cap_quote']);allowed=set(arm_spec['allowed_purposes'])
    provenance=load(directory/'source_receipt.json')
    for key,name in [('compiled_source_sha256','executed_native_body.py'),('adapter_sha256','executed_release_adapter.py'),('runner_sha256','executed_run_comparison.py')]:
        path=directory/name
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=provenance[key]:
            raise ArithmeticError(f'Executed source pin mismatch: {key}')
    if provenance['native_strategy_sha256']!=protocol['native_strategy_sha256']:
        raise ArithmeticError('Native strategy source pin mismatch')
    fills=load(record_path('new_fills.json'));events=load(record_path('release_events.json'))
    fill_map={f['order_id']:f for f in fills}
    if len(fill_map)!=len(fills):raise ArithmeticError('Duplicate actual fill order ID')
    lots={x['lot_id']:native_lot(x) for x in load(directory/'initial_active_lots.json')}
    claims={};terminal=set();source_actions={};checks=Counter();peak=ZERO
    spent=retired=whole_returned=partial_returned=ZERO
    by_purpose=Counter();owned_by_purpose=Counter()
    for event in events:
        kind=event['event'];oid=event['order_id']
        if kind=='CLAIM':
            if oid in claims or oid in terminal:raise ArithmeticError('Duplicate/terminal funding claim')
            amount=decimal(event,'amount');ordinary=decimal(event,'ordinary');parked=decimal(event,'parked')
            close(amount,ordinary+parked,'Claim source sum')
            if min(amount,ordinary,parked)<ZERO:raise ArithmeticError('Negative claimed source')
            if parked>ZERO and event['kind'] not in allowed:raise ArithmeticError('Wrong-purpose parked claim')
            close(event['total_cash_added'],ZERO,'Claim cash mint')
            close(event['cap'],cap,'Claim cap pin')
            claims[oid]=event;checks['claim_source_identity']+=1
        elif kind=='CLAIM_ZERO_FILL_RELEASE':
            claim=claims.pop(oid,None)
            if oid in terminal:raise ArithmeticError('Duplicate claim terminal event')
            terminal.add(oid)
            close(event['parked_cash_debited'],ZERO,'Rejected claim cash movement')
            close(event['released_parked_claim'],ZERO if claim is None else claim['parked'],'Rejected claim amount')
            checks['zero_fill_claim_release']+=1
        elif kind=='BUY_SOURCE_COMMIT':
            if oid in source_actions or oid in terminal:raise ArithmeticError('Duplicate source buy disposal')
            fill=fill_map[oid]
            if fill['kind'] not in {'ENTRY','RECOVERY'} or event['kind']!=fill['kind']:raise ArithmeticError('Wrong actual source buy kind')
            total=decimal(fill,'gross_quote')+decimal(fill,'fee_quote');parked=decimal(event,'parked_used');ordinary=decimal(event,'ordinary_used')
            close(total,event['actual_all_in'],'Source buy amount')
            close(total,parked+ordinary,'Source buy component identity')
            close(event['parked_principal_used'],parked-decimal(event,'parked_profit_used'),'Source principal/profit split')
            close(event['parked_before'],decimal(event,'parked_after')+parked,'Source debit')
            close(event['source_owned_quantity'],decimal(fill,'quantity')*parked/total,'Source quantity attribution')
            close(event['total_cash_added'],ZERO,'Buy cash minted')
            if parked>ZERO and fill['kind'] not in allowed:raise ArithmeticError('Wrong-purpose actual source buy')
            if arm!='OFF':
                claim=claims.pop(oid)
                close(claim['parked'],parked,'Consumed source claim')
                close(claim['ordinary'],ordinary,'Consumed ordinary claim')
            elif parked!=ZERO:raise ArithmeticError('OFF used parked source')
            lot_id=event['lot_id']
            if lot_id in lots:raise ArithmeticError('Duplicate owned source lot')
            lots[lot_id]=dict(lot_id=lot_id,parent=str(fill['position_id']),pair=fill['pair'],kind=fill['kind'],
                quantity=decimal(fill,'quantity'),basis=total,parked=parked)
            terminal.add(oid);source_actions[oid]=event;spent+=parked
            by_purpose[fill['kind']]+=parked
            checks['actual_buy_source_identity']+=1
        elif kind=='SOURCE_SALE_FINAL':
            if oid in source_actions:raise ArithmeticError('Duplicate source sale event')
            fill=fill_map[oid]
            if fill['kind']!=event['kind'] or fill['kind'] not in {'EXIT','PARTIAL_RECYCLE'}:raise ArithmeticError('Wrong source sale kind')
            rows=event['lots'];ids=[r['lot_id'] for r in rows]
            if len(set(ids))!=len(ids):raise ArithmeticError('Duplicate sold lot')
            if fill['kind']=='EXIT' and set(ids)!={key for key,lot in lots.items() if lot['parent']==str(fill['position_id'])}:raise ArithmeticError('Whole sale omitted or added parent lots')
            close(sum((decimal(r,'quantity') for r in rows),ZERO),fill['quantity'],'Sale exact quantity')
            close(sum((decimal(r,'native_basis') for r in rows),ZERO),fill['basis_released'],'Sale exact basis')
            net=decimal(fill,'gross_quote')-decimal(fill,'fee_quote');remaining=net
            for i,row in enumerate(rows):
                lot=lots.pop(row['lot_id'])
                if lot['parent']!=str(fill['position_id']):raise ArithmeticError('Cross-parent disposal')
                if fill['kind']=='PARTIAL_RECYCLE' and lot['kind']!='RECOVERY':raise ArithmeticError('Partial disposed ENTRY source')
                close(row['quantity'],lot['quantity'],'Source sale lot quantity')
                close(row['native_basis'],lot['basis'],'Source sale lot basis')
                close(row['parked_basis'],lot['parked'],'Source sale acquisition basis retired')
                actual=remaining if i==len(rows)-1 else net*lot['quantity']/decimal(fill,'quantity')
                remaining-=actual
                close(row['actual_net'],actual,'Quantity-based receipt')
                expected=ZERO if lot['parked']==ZERO else actual*lot['parked']/lot['basis']
                close(row['parked_net'],expected,'Source net receipt')
                close(row['ordinary_net'],actual-expected,'Ordinary receipt')
                retired+=lot['parked']
            source_actions[oid]=event
            pnet=sum((decimal(row,'parked_net') for row in rows),ZERO)
            if fill['kind']=='EXIT':whole_returned+=pnet
            else:partial_returned+=pnet
            checks['actual_sale_exact_ownership']+=1
        else:raise ArithmeticError(f'Unknown source event {kind}')
        exposure=sum((lot['parked'] for lot in lots.values()),ZERO)+sum((decimal(claim,'parked') for claim in claims.values()),ZERO)
        if exposure>cap+TOL:raise ArithmeticError('Outstanding basis/claim cap exceeded')
        peak=max(peak,exposure);checks['source_cap_event_checks']+=1
    if claims and not partial:raise ArithmeticError('Final unresolved source claims')
    if set(source_actions)!=set(fill_map):raise ArithmeticError('Actual/source finalized fill sets differ')
    owned=sum((lot['parked'] for lot in lots.values()),ZERO)
    close(spent,retired+owned,'Spent/retired/owned conservation')
    final=load(record_path('terminal_release.json'))
    close(final['owned_parked_basis'],owned,'Terminal source basis')
    close(final['parked_spent'],spent,'Terminal spent')
    close(final['parked_basis_retired'],retired,'Terminal retired')
    close(final['parked_whole_returned'],whole_returned,'Terminal whole parked receipt')
    close(final['parked_partial_returned'],partial_returned,'Terminal partial parked receipt')
    close(final['peak_owned_basis_plus_claims'],peak,'Terminal peak source commitment')
    close(final['reserved_parked'],sum((decimal(c,'parked') for c in claims.values()),ZERO),'Remaining parked claims')
    close(final['reserved_ordinary'],sum((decimal(c,'ordinary') for c in claims.values()),ZERO),'Remaining ordinary claims')
    close(final['reserved_total'],sum((decimal(c,'amount') for c in claims.values()),ZERO),'Remaining total claims')
    close(final['total_cash_minted'],ZERO,'Cash mint')
    if {c['order_id'] for c in final['pending_claims']}!=set(claims):raise ArithmeticError('Remaining source claim identities differ')

    terminal_native={row['lot_id']:native_lot(row) for row in load(record_path('terminal_active_lots.json'))}
    terminal_source={row['lot_id']:row for row in final['active_source_lots']}
    if set(lots)!=set(terminal_native) or set(lots)!=set(terminal_source):raise ArithmeticError('Terminal native/source lot sets differ')
    for lot_id,lot in lots.items():
        close(terminal_native[lot_id]['quantity'],lot['quantity'],'Terminal lot quantity')
        close(terminal_native[lot_id]['basis'],lot['basis'],'Terminal lot basis')
        close(terminal_source[lot_id]['parked_basis'],lot['parked'],'Terminal lot source basis')
        owned_by_purpose[lot['kind']]+=lot['parked']
    # Independently fold actual fills into every completed-close account row.
    receipt=load(directory/'hydration_receipt.json');start=receipt['initial_boundary_audit']['observed']
    cash=D(start['cash']);basis=D(start['committed_basis']);realized=D(start['realized_pnl']);parked=D(start['parked_cash'])
    previous=None;cursor=0;rows=0
    with (directory/'window_accounts.csv').open() as f:
        for row in csv.DictReader(f):
            at=int(row['timestamp'])
            if previous is not None and at-previous!=300:raise ArithmeticError('Missing or duplicate account timestamp')
            previous=at
            while cursor<len(fills) and int(fills[cursor]['timestamp'])<=at:
                fill=fills[cursor];cursor+=1
                action=source_actions[fill['order_id']]
                gross=decimal(fill,'gross_quote');fee=decimal(fill,'fee_quote')
                if fill['kind'] in {'ENTRY','RECOVERY'}:
                    cash-=gross+fee;basis+=gross+fee;parked-=decimal(action,'parked_used')
                else:
                    net=gross-fee;cash+=net;basis-=decimal(fill,'basis_released');realized+=decimal(fill,'realized_pnl')
                    parked+=net if fill['kind']=='PARTIAL_RECYCLE' else sum((decimal(x,'parked_net') for x in action['lots']),ZERO)
                    if decimal(fill,'realized_pnl')+TOL<decimal(fill,'basis_released')*D('.01'):raise ArithmeticError('Exit breached hard net1% floor')
            close(row['cash'],cash,'Folded cash');close(row['committed_basis'],basis,'Folded basis')
            close(row['realized'],realized,'Folded realized');close(row['parked'],parked,'Folded parked cash')
            close(row['ordinary'],cash-parked,'Ordinary cash')
            close(cash+basis,D(protocol['initial_capital'])+realized,'Account acquisition identity')
            close(row['equity'],cash+decimal(row,'marked_inventory'),'Marked account identity')
            if min(cash,parked,cash-parked)<-TOL:raise ArithmeticError('Negative source cash')
            if int(row['active_positions'])>protocol['cash_policy']['maximum_distinct_assets']:raise ArithmeticError('Position count cap')
            rows+=1
    if cursor!=len(fills) or rows!=expected_frames:raise ArithmeticError('Account/fill coverage mismatch')
    close(result['ending_cash'],cash,'Reported cash');close(result['parked_cash'],parked,'Reported P cash')
    close(result['open_basis'],basis,'Reported basis');close(result['new_realized_profit'],realized-D(start['realized_pnl']),'Reported window realized')
    new_open=D(result['marked_inventory'])-basis
    close(result['ending_equity'],D(start['equity'])+realized-D(start['realized_pnl'])+new_open-D(start['unrealized_pnl']),'Window return identity')
    close(sum((lot['basis'] for lot in lots.values()),ZERO),basis,'Terminal lot/account basis')
    positions=load(record_path('terminal_positions.json'))
    by_parent={}
    for lot in lots.values():
        acc=by_parent.setdefault(lot['parent'],dict(pair=lot['pair'],quantity=ZERO,basis=ZERO))
        if acc['pair']!=lot['pair']:raise ArithmeticError('Parent contains cross-pair source lot')
        acc['quantity']+=lot['quantity'];acc['basis']+=lot['basis']
    if {str(p['position_id']) for p in positions}!=set(by_parent):raise ArithmeticError('Parent/source terminal sets differ')
    if len({p['pair'] for p in positions})!=len(positions):raise ArithmeticError('Duplicate final active asset')
    for position in positions:
        parent=by_parent[str(position['position_id'])]
        if position['pair']!=parent['pair']:raise ArithmeticError('Terminal parent pair differs')
        close(position['quantity'],parent['quantity'],'Terminal parent exact quantity')
        close(position['basis'],parent['basis'],'Terminal parent exact basis')
    if prepared is not None:
        import numpy as np
        metadata=load(Path(prepared)/'metadata.json')
        tape=np.load(Path(prepared)/'ohlcv.npy',mmap_mode='r')
        pair_index={pair:i for i,pair in enumerate(metadata['pairs'])}
        fee_rate=D(protocol['unchanged']['fee_bps_per_side'])/D(10000)
        slip=D(protocol['unchanged']['slippage_bps'])/D(10000)
        participation=D(protocol['unchanged']['volume_participation_fraction'])
        minimum=D(protocol['unchanged']['generic_minimum_quote'])
        dec=lambda raw,k:D(str(float(raw[k])))
        for fill in fills:
            timestamp=int(fill['timestamp'])
            elapsed=timestamp-metadata['start']
            if elapsed%metadata['interval_seconds']:
                raise ArithmeticError('Fill outside canonical execution interval')
            # Native fills are recorded at execution candle completion; their
            # modeled execution price is that immediate candle OPEN.
            index=elapsed//metadata['interval_seconds']-1
            if index<1 or index>=tape.shape[0]:raise ArithmeticError('Execution candle outside canonical tape')
            pair=pair_index[fill['pair']]
            current=tape[index,pair];previous=tape[index-1,pair]
            if not np.isfinite(current).all() or dec(current,4)<=ZERO:
                raise ArithmeticError('Actual fill lacks authentic immediate execution candle/volume')
            prior_turnover=ZERO if not np.isfinite(previous[0]) else dec(previous,3)*dec(previous,4)
            capacity=min(prior_turnover,dec(current,3)*dec(current,4))*participation
            expected_price=dec(current,0)*(D(1)+slip if fill['kind'] in {'ENTRY','RECOVERY'} else D(1)-slip)
            close(fill['price'],expected_price,'Genuine next-open execution price')
            gross=decimal(fill,'gross_quote')
            close(fill['fee_quote'],gross*fee_rate,'Genuine modeled fee')
            close(gross,decimal(fill,'quantity')*expected_price,'Execution quantity/price')
            if gross>capacity+TOL:raise ArithmeticError('Authentic FOK turnover capacity exceeded')
            if gross<minimum-TOL:raise ArithmeticError('Authentic modeled quote minimum failed')
            checks['genuine_execution_bar_price_fee_quantity_capacity_minimum']+=1
        checks['genuine_tape_validation_is_modeled_not_venue_execution']=True
    checks['independent_terminal_parents']=len(positions)
    checks['independent_account_rows']=rows;checks['account_field_comparisons']=rows*7
    return dict(passed=True,arm=arm,actual_fills=len(fills),partial_only=partial,complete_window=not partial,requested_window_frames=protocol['frames'],pending_contingent_ids=final['contingent_order_ids'],saved_result_json_present=saved_result,checks=dict(checks),
        independently_reconstructed_cap_peak=str(peak),owned_parked_basis=str(owned),
        source_spent_by_purpose={k:str(v) for k,v in by_purpose.items()},
        surviving_source_basis_by_purpose={k:str(v) for k,v in owned_by_purpose.items()},
        scope='Independent saved-output source/lot/cash reconstruction; no second strategy execution or live validation. Partial mode checks observed prefix only and makes no completed-window performance claim.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--protocol',type=Path,required=True);parser.add_argument('--run',type=Path,action='append',required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--prepared',type=Path);parser.add_argument('--partial',action='store_true');args=parser.parse_args()
    protocol=load(args.protocol)
    audits=[]
    for directory in args.run:audits.append(audit(directory,protocol,args.prepared,args.partial))
    args.output.write_text(json.dumps(dict(passed=all(x['passed'] for x in audits),arms=audits),indent=2)+'\n')
    print(json.dumps(dict(passed=True,arms=[x['arm'] for x in audits],account_rows=sum(x['checks']['independent_account_rows'] for x in audits))))
