"""Read-only final inventory and completed-cycle diagnostics; no order authority."""
from datetime import datetime
from decimal import Decimal as D
from .owned_spot.model import ZERO, floor_step, Blocked


def inventory_report(adapter, *, at):
    lots=adapter._lots();by_asset={};by_cohort={};by_root={}
    def measure(pair, selected):
        units=sum((l.base_remaining for l in selected),ZERO)
        basis=sum((l.cost_basis_remaining_quote+l.entry_fees_remaining_quote for l in selected),ZERO)
        mark=adapter.state['marks'].get(pair);rule=adapter.config.rules[pair]
        current=mark is not None and mark['completed_at']==at
        price=D(mark['close']) if mark else None
        rounded=floor_step(units,rule.base_step)
        tradable=(rounded>0 and rounded>=rule.min_base and price is not None and rounded*price>=rule.min_quote)
        value=units*price if current else None
        return {'units':units,'fee_inclusive_remaining_basis':basis,
            'entry_fees_remaining':sum((l.entry_fees_remaining_quote for l in selected),ZERO),
            'current_market_value':value,'current_unrealized_pnl':None if value is None else value-basis,
            'last_observed_mark':price,'mark_at':mark['completed_at'] if mark else None,'mark_is_exact_current':current,
            'lot_rounded_quantity':rounded,'rounding_dust_units':units-rounded,
            'below_execution_minimum':units>0 and not tradable,
            'minimum_diagnostic_uses_last_observed_mark':not current,
            'minimum_scope':'AGGREGATE_ONLY_NOT_CROSS_OWNER_EXECUTION_AUTHORITY',
            'lot_ids':[l.lot_id for l in selected]}
    for pair in adapter.config.rules:
        by_asset[pair]=measure(pair,[l for l in lots if l.pair==pair])
    root_lots={}
    for cid,cohort in adapter.state['cohorts'].items():
        selected=[l for l in lots if l.lot_id in cohort['lot_ids']]
        root=cohort.get('root_cohort_id',cid)
        by_cohort[cid]=dict(measure(cohort['pair'],selected),pair=cohort['pair'],root_cohort_id=root,
            origin=cohort.get('origin','UNCLASSIFIED_LEGACY'),generation=cohort.get('generation',1),status=cohort['status'])
        record=root_lots.setdefault(root,{'pair':cohort['pair'],'lots':[],'cohort_ids':[]})
        if record['pair']!=cohort['pair']:raise Blocked('Logical root crosses asset ownership')
        record['lots'].extend(selected);record['cohort_ids'].append(cid)
    for root,record in root_lots.items():
        if len({l.lot_id for l in record['lots']})!=len(record['lots']):raise Blocked('Logical root repeats native lot')
        by_root[root]=dict(measure(record['pair'],record['lots']),pair=record['pair'],cohort_ids=record['cohort_ids'])
    owners={}
    for lot in lots:
        key=(lot.pair,lot.proposal_id,lot.executor_id)
        owners.setdefault(key,[]).append(lot)
    by_owner=[]
    for (pair,proposal,executor),selected in sorted(owners.items()):
        item=measure(pair,selected)
        item['minimum_scope']='EXACT_NATIVE_PROPOSAL_EXECUTOR_OWNER'
        by_owner.append(dict(item,pair=pair,proposal_id=proposal,executor_id=executor))
    return {'assets':by_asset,'cohorts':by_cohort,'logical_roots':by_root,
            'native_owners':by_owner,
            'owner_partition_dust_units':{pair:sum((r['units'] for r in by_owner
                if r['pair']==pair and r['below_execution_minimum']),ZERO) for pair in adapter.config.rules}}



def cycle_report(adapter,receipts):
    """Fee-inclusive adverse-price roundtrip flag, with no tuned time threshold.

    A price-adverse completed roundtrip has buy VWAP including buy fees at least
    as high as sell VWAP after sell fees. An incomplete parked-cash episode is
    reported separately; no hypothetical buy is inserted to create a roundtrip.
    """
    from .owned_spot.psm_bridge import PSM
    out=[]
    for pair,meta in adapter.state.get('psm',{}).items():
        completed={entry['position']:entry for entry in meta.get('archived_episodes',[])}
        episodes=dict(completed)
        if meta['position'] not in episodes:
            episodes[meta['position']]={'position':meta['position'],'episode':meta['episode'],'journal':meta['journal'],'completed_at':None}
        for position,entry in sorted(episodes.items(),key=lambda item:item[1]['episode']):
            rows=[r for r in receipts if adapter.state['orders'][r['order_id']].get('psm_position')==position]
            sells=[r for r in rows if r['trade_type']=='SELL'];buys=[r for r in rows if r['trade_type']=='BUY']
            def aggregate(items):
                units=sum((D(r['amount']) for r in items),ZERO)
                gross=sum((D(r['amount'])*D(r['price']) for r in items),ZERO)
                fees=sum((D(r['fee_paid']) for r in items),ZERO)
                return units,gross,fees
            su,sg,sf=aggregate(sells);bu,bg,bf=aggregate(buys)
            sell_net=(sg-sf)/su if su else None;buy_cost=(bg+bf)/bu if bu else None
            first=min((datetime.fromisoformat(r['timestamp']).timestamp() for r in sells),default=None)
            last=max((datetime.fromisoformat(r['timestamp']).timestamp() for r in buys),default=None)
            book=PSM.ResearchBook.loads(entry['journal']);p=book.positions[position]
            done=position in completed
            out.append({'pair':pair,'episode':entry['episode'],'position':position,'completed':done,
                'phase':p['phase'],'completed_at':entry.get('completed_at'),
                'first_actual_sell_at':first,'last_actual_buy_at':last,
                'actual_sell_to_last_buy_seconds':last-first if first is not None and last is not None else None,
                'sale_fill_count':len(sells),'buy_fill_count':len(buys),'sold_units':su,'rebuilt_units_filled':bu,
                'gross_sale_proceeds':sg,'actual_sale_fees':sf,'gross_rebuild_cost':bg,'actual_buy_fees':bf,
                'sell_vwap_after_fees':sell_net,'buy_vwap_including_fees':buy_cost,
                'price_adverse_completed_roundtrip':bool(done and su and bu and buy_cost>=sell_net),
                'snapshot_units':p['snapshot_units'],'ending_episode_units':p['original_units']+p['rebuilt_units'],
                'unit_change':p['original_units']+p['rebuilt_units']-p['snapshot_units'],
                'remaining_episode_cash':p['cash'],'actual_profit_distributed_out':p['cumulative_distributions'],
                'reducer_basis_is_diagnostic_only':True})
    return {'definition':'price-adverse completed roundtrip: fee-inclusive buy VWAP >= fee-net sell VWAP; no time threshold',
        'structural_limitation':'Canonical own-cash unit improvement generally prevents this adverse completed roundtrip; zero is not evidence of no whipsaw risk. Blocked reversals and time in cash expose uncompleted adverse switches.',
        'completed_cycles':sum(r['completed'] for r in out),
        'price_adverse_completed_roundtrips':sum(r['price_adverse_completed_roundtrip'] for r in out),
        'episodes':out}
