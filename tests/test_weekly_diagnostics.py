from decimal import Decimal as D
from types import SimpleNamespace as NS
from datetime import datetime,timezone
import pytest
from research.weekly_reversal.diagnostics import inventory_report,cycle_report
from research.weekly_reversal import psm_kernel as K


def inventory_fixture():
    pair='BTC-USD'
    lots=[NS(lot_id=f'l{i}',pair=pair,base_remaining=D('.6'),cost_basis_remaining_quote=D(5),
             entry_fees_remaining_quote=D('.01'),proposal_id=f'p{i}',executor_id=f'e{i}') for i in range(2)]
    state={'marks':{pair:{'completed_at':300,'close':'10'}},'cohorts':{
        'root':{'id':'root','pair':pair,'lot_ids':[],'origin':'SEED','status':'SOLD'},
        'successor':{'id':'successor','pair':pair,'lot_ids':['l0','l1'],'origin':'PSM_REBUILD',
                     'root_cohort_id':'root','generation':2,'status':'OWNED'}}}
    return NS(_lots=lambda:lots,state=state,config=NS(rules={pair:NS(base_step=D('.1'),min_base=D(1),min_quote=D(10))}))


def test_inventory_reports_exact_root_and_owner_dust_without_cross_subsidy():
    a=inventory_fixture();result=inventory_report(a,at=300)
    assert result['assets']['BTC-USD']['units']==D('1.2')
    assert result['assets']['BTC-USD']['below_execution_minimum'] is False
    assert result['logical_roots']['root']['fee_inclusive_remaining_basis']==D('10.02')
    assert result['logical_roots']['root']['current_unrealized_pnl']==D('1.98')
    assert result['owner_partition_dust_units']['BTC-USD']==D('1.2')
    assert all(x['below_execution_minimum'] for x in result['native_owners'])
    assert result['cohorts']['successor']['origin']=='PSM_REBUILD'


def test_inventory_missing_exact_mark_never_uses_stale_current_value():
    result=inventory_report(inventory_fixture(),at=600)
    row=result['assets']['BTC-USD']
    assert row['current_market_value'] is None and row['current_unrealized_pnl'] is None
    assert row['last_observed_mark']==10 and row['mark_at']==300
    assert row['minimum_diagnostic_uses_last_observed_mark']


def cycle_fixture(*,complete):
    b=K.ResearchBook();pos='episode1';owner='owner';at=1000
    def apply(kind,**values):
        nonlocal at
        at+=1
        e={'id':str(at),'kind':kind,'at':at,'position':pos,'owner':owner};e.update(values);b.apply(e)
    quote={'price':'10','quote_at':1000,'max_age':300,'step':'.001','min_units':'.001','min_quote':'.5','max_fee_rate':'.01'}
    apply('snapshot',account='paper',asset='BTC/USD',provenance='fixture',inventory_bucket='ACTIVE_TRADE',
          reconciled=True,pending_orders=[],units='2',basis='20')
    apply('handoff',vcap_eligible=False,falling_knife=True,reconciled=True,pending_orders=[],confirmed_units='2',decision_ref='fixture')
    apply('intent',order='sell',side='sell',**quote)
    apply('fill',order='sell',fill='s',units='2',price='10',fee='.2',fee_currency='quote')
    sell_at=at
    apply('reconcile',order='sell',status='FILLED',complete_fill_history=True,filled_units='2',gross='20',fees='.2')
    apply('close_exit',**quote)
    rows=[{'order_id':'sell','trade_type':'SELL','amount':'2','price':'10','fee_paid':'.2',
           'timestamp':datetime.fromtimestamp(sell_at,timezone.utc).isoformat()}]
    if complete:
        apply('rebuild_signal',qualified=True,decision_ref='fixture',expires_at=2000)
        apply('intent',order='buy',side='buy',min_unit_gain='0',**dict(quote,price='9'))
        apply('fill',order='buy',fill='b',units='2.1',price='9',fee='.189',fee_currency='quote')
        rows.append({'order_id':'buy','trade_type':'BUY','amount':'2.1','price':'9','fee_paid':'.189',
                     'timestamp':datetime.fromtimestamp(at,timezone.utc).isoformat()})
        apply('reconcile',order='buy',status='CANCELED',complete_fill_history=True,filled_units='2.1',gross='18.9',fees='.189')
        apply('complete')
    entry={'position':pos,'episode':1,'journal':b.dumps(),'completed_at':at}
    state={'orders':{'sell':{'psm_position':pos},'buy':{'psm_position':pos}},'psm':{'BTC-USD':{
        'position':pos,'episode':1,'journal':b.dumps(),'archived_episodes':[entry] if complete else []}}}
    return NS(state=state),rows


def test_completed_cycle_uses_actual_receipts_fees_and_units():
    a,rows=cycle_fixture(complete=True);result=cycle_report(a,rows);cycle=result['episodes'][0]
    assert result['completed_cycles']==1 and result['price_adverse_completed_roundtrips']==0
    assert cycle['sold_units']==2 and cycle['rebuilt_units_filled']==D('2.1')
    assert cycle['unit_change']==D('.1') and cycle['remaining_episode_cash']==D('.711')
    assert cycle['actual_sale_fees']+cycle['actual_buy_fees']==D('.389')
    assert cycle['sell_vwap_after_fees']==D('9.9') and cycle['buy_vwap_including_fees']==D('9.09')


def test_incomplete_cash_wait_is_not_fabricated_roundtrip():
    a,rows=cycle_fixture(complete=False);result=cycle_report(a,rows)
    assert result['completed_cycles']==0 and result['episodes'][0]['buy_fill_count']==0
    assert result['episodes'][0]['buy_vwap_including_fees'] is None
    assert result['episodes'][0]['actual_sell_to_last_buy_seconds'] is None
    assert result['episodes'][0]['remaining_episode_cash']==D('19.8')
