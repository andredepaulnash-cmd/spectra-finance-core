"""Read-only-source audit regressions; generated market data only, no history."""
from pathlib import Path
from decimal import Decimal as D
import json, shutil
import numpy as np
import pytest
from research.weekly_reversal.replay import WeeklyReplay
from research.weekly_reversal.checkpoint import save_completed_boundary, restore_checkpoint
from research.weekly_reversal.owned_spot.model import CompletedBar
from research.weekly_reversal.compounding_adapter import WeeklyCompoundingAdapter
from spectra_core.reversal_signals import SignalPolicy
from test_weekly_replay import source
from test_weekly_owned_adapter import config, seed
from test_weekly_compounding_adapter import members, profitable_sale, reentry_boundary
from test_weekly_checkpoint import pins


def make(root, tape):
    cfg=config(('BTC','ETH'))
    return WeeklyReplay(root,tape=tape,config=cfg,membership=members(cfg),
        signal_policy=SignalPolicy(enabled=True,bbwp_minimum=20),protocol_sha256='synthetic-only')


def test_missing_final_owned_bar_must_not_claim_complete_current_nav(tmp_path):
    tape=source()
    tape.array[(tape.end-tape.data_start)//300-1,0,:]=np.nan
    r=make(tmp_path/'run',tape)
    try:
        for event in tape.boundaries():r.step(event)
        report=r.finish()
        final=json.loads((r.root/'marks.jsonl').read_text().splitlines()[-1])
        print('MISSING_FINAL_MARK', {'final_mark':final,'completed':report['completed'],
            'valuation_complete':report['valuation_complete'],'equity':str(report['equity']),
            'stale_marks':report['stale_marks'],'missing_marks':report['missing_marks'],
            'mark_age_seconds':report['mark_age_seconds']})
        assert not final['complete'] and final['equity'] is None
        assert report['valuation_complete'] is False
    finally:r.close()


def test_canceled_unfilled_reentry_must_keep_slot_retryable(tmp_path):
    cfg=config(('BTC',))
    with WeeklyCompoundingAdapter(tmp_path/'account',cfg,now=1000,membership=members(cfg),enabled=True) as a:
        seed(a);profitable_sale(a);a.sync_profit_cash(now=7500);reentry_boundary(a)
        cid='BTC-USD:seed:01';profit=a.available_profit('BTC-USD')
        order=a.request_reentry(cid,now=36000)
        a.observe(CompletedBar('BTC-USD',36000,36300,D('11'),D('1000000'),36300),now=36300)
        a.refresh_accounting_all(now=36300)
        a.missing_open(order['id'],expected_at=36300,now=36300)
        a.sync_profit_cash(now=36300)
        cohort=a.state['cohorts'][cid]
        print('CANCELED_REENTRY', {'order_status':a.state['orders'][order['id']]['status'],
            'generation':cohort['generation'],'status':cohort['status'],'anchor':cohort['anchor'],
            'lots':len(a._cohort_lots(cohort)),'pending':len(a._pending()),
            'profit_before':str(profit),'profit_after':str(a.available_profit('BTC-USD'))})
        assert a.state['orders'][order['id']]['status']=='CANCELED'
        assert not a._cohort_lots(cohort) and not a._pending()
        assert cohort['status']=='SOLD'


def test_checkpoint_resume_must_reject_changed_market_input(tmp_path):
    root=tmp_path/'run';tape=source();r=make(root,tape)
    try:
        r.step(tape.boundary(tape.start))
        receipt=save_completed_boundary(r,tmp_path/'checkpoint',source_pins=pins())
    finally:r.close()
    shutil.move(root,tmp_path/'preserved')
    restore_checkpoint(tmp_path/'checkpoint',root,expected_manifest_sha256=receipt['manifest_sha256'])
    altered=source();idx=(altered.start+300-altered.data_start)//300
    altered.array[idx,:,0:4]=[20,20.1,19.9,20]
    cfg=config(('BTC','ETH'))
    resumed=None
    try:
        with pytest.raises(ValueError,match='input|tape|identity|scope',check=None):
            resumed=WeeklyReplay.resume_restored(root,tape=altered,config=cfg,membership=members(cfg),protocol_sha256='synthetic-only')
        print('Changed market input correctly refused')
    finally:
        if resumed is not None:
            resumed.step(altered.boundary(altered.start+300))
            print('CHANGED_TAPE_ACCEPTED', {'receipts':len(resumed.a._receipts()),
                'fill_prices':sorted(set(f['price'] for f in resumed.a._receipts()))})
            resumed.close()
