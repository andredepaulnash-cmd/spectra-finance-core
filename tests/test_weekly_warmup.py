from decimal import Decimal as D
import numpy as np
import pytest

from research.weekly_reversal.warmup import prime_empty_account
from research.weekly_reversal.causal_tape import CausalTape
from research.weekly_reversal.owned_spot.adapter import OwnedSpotAdapter
from research.weekly_reversal.owned_spot.model import CompletedBar, Blocked
from test_weekly_owned_adapter import config


def tape():
    values=np.zeros((72,2,6),dtype=np.float64)
    for i in range(72):
        values[i]=[10+i/100,10.5+i/100,9.5+i/100,10.1+i/100,1000+i,10]
    values[4,0]=np.nan;values[17,1]=np.nan
    return CausalTape(values,data_start=3600,warmup_start=3600,start=21600,end=25200,roster=('BTC','ETH'),synthetic=True)


def test_warmup_native_state_exactly_matches_every_original_observe(tmp_path):
    source=tape();cfg=config(('BTC','ETH'))
    with OwnedSpotAdapter(tmp_path/'naive',cfg,now=3600) as a:
        for at,bars in source.warmup_bars():
            a.advance(at)
            for bar in bars:
                if bar is not None:
                    a.observe(CompletedBar(bar.pair,bar.opened_at,bar.completed_at,bar.close,bar.quote_volume_proxy,bar.completed_at),now=at)
        expected={key:a.state[key] for key in ('marks','volume_history','fingerprints','now')}
    with OwnedSpotAdapter(tmp_path/'batched',cfg,now=3600) as a:
        result=prime_empty_account(a,source)
        assert {key:a.state[key] for key in expected}==expected
        assert set(result)==set(cfg.rules)
        assert a._receipts()==[] and a.state['orders']=={} and a.cash()==2000
        with pytest.raises(Blocked,match='untouched'):
            prime_empty_account(a,source)
    with OwnedSpotAdapter(tmp_path/'batched',cfg,now=21600) as a:
        assert {key:a.state[key] for key in expected}==expected
        assert a.state['warmup_receipt']['end']==21600
