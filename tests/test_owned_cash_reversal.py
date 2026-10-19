from datetime import datetime, timezone
from decimal import Decimal as D
from dataclasses import replace
import json
import pytest

from spectra_core.reversal_signals import HourBar, SignalObservation
from research.weekly_reversal.owned_cash_reversal import OwnedCashReversal


def sale(at=0, trade="actual-sale", pair="BTC-USD"):
    return dict(trade_id=trade, order_id="actual-order", trade_type="SELL", trading_pair=pair,
        account_name="paper", connector_name="paper", amount="1", price="100", fee_paid=".8",
        fee_currency="USD", timestamp=datetime.fromtimestamp(at, timezone.utc).isoformat())


def reference(at=0):
    return OwnedCashReversal.from_settled_sales(cycle_id="psm:episode:1", pair="BTC-USD",
        sale_receipts=[sale(at)], sealed_at=at)


def hour(n, flags=("positive_completed_hour", "higher_completed_low", "above_ema8")):
    bar = HourBar("BTC-USD", (n-1)*3600, 90, 92, 89, 91, 100, 10)
    obs = SignalObservation("BTC-USD", n*3600, n*3600+300, n*3600+3900,
        300, 60, 50, 3, 90, .01, -.02, (), flags, 91, 89)
    return bar, obs


def test_qualified_reference_is_no_short_fill_or_profit():
    current = reference()
    for n in (1, 2):
        bar, obs = hour(n)
        result = current.observe(bar, obs, atr=D(3), now=n*3600)
    assert result["eligible"]
    assert result["semantic_scope"] == "OWNED_SPOT_REBUILD_SIGNAL_ONLY"
    assert result["derivative_pnl"] is None
    assert current.state.covered_quantity == 0
    assert all(type(fill).__name__ == "EntryFill" for fill in current.state.fills)
    assert all(attempt.dispatched_at is None for attempt in current.state.intent.attempts)


def test_actual_sale_overlap_is_not_used_as_postsale_hour():
    current = reference(300)
    bar, obs = hour(1)
    assert current.observe(bar, obs, atr=D(3), now=3600)["reason"] == "hour_overlaps_actual_sale"
    assert current.state.last_bar_close_at is None
    bar, obs = hour(2)
    current.observe(bar, obs, atr=D(3), now=7200)
    assert current.state.halted_reason is None


def test_reference_restart_retains_owned_sources_and_no_duplicate_intent():
    current = reference()
    for n in (1, 2):
        b, o = hour(n); current.observe(b, o, atr=D(3), now=n*3600)
    payload = current.dumps()
    restarted = OwnedCashReversal.loads(payload)
    assert restarted.dumps() == payload
    intent = restarted.state.intent.intent_id
    b, o = hour(3); restarted.observe(b, o, atr=D(3), now=10800)
    assert restarted.state.intent.intent_id == intent


def test_stale_qualification_blocks_until_new_hour():
    current = reference()
    for n in (1, 2):
        b, o = hour(n); current.observe(b, o, atr=D(3), now=n*3600)
    assert current.qualification(now=7500)["eligible"]
    assert not current.qualification(now=7501)["eligible"]


def test_gap_halts_instead_of_skipping_missing_market_path():
    current = reference()
    b, o = hour(1); current.observe(b, o, atr=D(3), now=3600)
    b, o = hour(3); result = current.observe(b, o, atr=D(3), now=10800)
    assert not result["eligible"] and "gapped" in result["reason"]


@pytest.mark.parametrize("change", [dict(trade_type="BUY"), dict(trading_pair="ETH-USD"),
                                   dict(fee_currency="BTC"), dict(amount="0")])
def test_non_owned_sale_reference_refused(change):
    with pytest.raises(ValueError):
        OwnedCashReversal.from_settled_sales(cycle_id="c", pair="BTC-USD",
            sale_receipts=[{**sale(), **change}], sealed_at=0)


def test_actual_completion_is_separate_from_unexecuted_cover_intent():
    current = reference()
    for n in (1, 2):
        b, o = hour(n); current.observe(b, o, atr=D(3), now=n*3600)
    current.complete_owned_rebuild(at=7500, actual_rebuild_receipt_ids=("actual-buy",))
    assert not current.qualification(now=7500)["eligible"]
    assert current.state.covered_quantity == 0
    assert OwnedCashReversal.loads(current.dumps()).dumps() == current.dumps()
    with pytest.raises(ValueError, match="conflicting"):
        current.complete_owned_rebuild(at=7501, actual_rebuild_receipt_ids=("other",))
