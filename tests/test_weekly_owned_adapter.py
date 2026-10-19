from decimal import Decimal as D
import pytest

from research.weekly_reversal.owned_spot.adapter import OwnedSpotAdapter
from research.weekly_reversal.owned_spot.model import AdapterConfig, TradingRule, CompletedBar, ObservedOpen, Blocked

ASSETS = ('BTC','ETH','SOL','BCH','LTC','DOGE','AVAX','LINK','DOT','SHIB','XLM','HBAR','ADA','AAVE','XTZ')


def config(assets=ASSETS):
    return AdapterConfig(fee_rate=D('.008'), fee_quantum=D('.00001'),
        rules={p+'-USD':TradingRule(D('.00001'),D('.001'),D('.5'),D('.01')) for p in assets},
        seed_count=4, seed_ticket=D('16.66666666'), execution_delay_seconds=300,
        max_order_age_seconds=300, expected_slippage_bps=D('15'))


def seed(adapter):
    for pair in adapter.config.rules:
        adapter.observe(CompletedBar(pair,700,1000,D('10'),D('1000000'),1000),now=1000)
    orders=[]
    for pair in adapter.config.rules:
        orders.extend(adapter.prepare_seed_asset(pair,now=1000))
    for order in orders:
        adapter.observe_open(order['id'],ObservedOpen(D('10'),1300,1300))
    adapter.refresh_accounting_all(now=1300)
    return orders


def test_fifteen_asset_native_seed_conserves_and_restarts(tmp_path):
    cfg=config()
    with OwnedSpotAdapter(tmp_path,cfg,now=1000) as a:
        orders=seed(a)
        assert len(orders)==60
        assert a.seed_complete()
        assert len(a._receipts())==60
        assert a.cash_pools()['protected']==500
        assert a.cash_pools()['repair']==500
        assert a.cash()+a.deployed_basis()==2000
        assert a.deployed_basis()<1000
        before=a.report()
    with OwnedSpotAdapter(tmp_path,cfg,now=1300) as a:
        assert a.report()==before
        assert len(a._receipts())==60
        assert a.seed_complete()


def test_seed_allocation_overflow_rejected():
    with pytest.raises(Blocked,match='seed pool'):
        AdapterConfig(fee_rate=D('.008'),fee_quantum=D('.00001'),
            rules={p+'-USD':TradingRule(D('.00001'),D('.001'),D('.5')) for p in ASSETS})


def test_reference_roster_only():
    with pytest.raises(Blocked,match='reference roster'):
        config(('UNDECLARED',))
