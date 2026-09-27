import json
from decimal import Decimal
from pathlib import Path

from spectra_core.position_hold_accounting import position_hold_accounting_from_raw


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "spectra-stack.lock.json"


def test_locked_hummingbot_api_exact_position_hold_contract():
    lock = json.loads(LOCK.read_text())
    api = lock["components"]["spectra_hummingbot_api"]

    assert api["revision"] == "fb8651c05d0bf128e27c4ac2a70c51a8fb9d90c7"
    assert api["contracts"]["exact_position_hold_accounting"] == 1

    # This row mirrors the exact PositionHold accounting payload proven in the
    # locked API revision's regression test. Core must continue accepting this
    # exact-decimal, exact-scope contract without coercing through floats.
    raw = {
        "account_name": "master_account",
        "connector_name": "kraken",
        "trading_pair": "BTC-USD",
        "controller_id": "spectra",
        "status": "ACTIVE",
        "buy_amount_base": "0.001234567890123456",
        "buy_amount_quote": "190.000000000000000001",
        "sell_amount_base": "0.000234567890123456",
        "sell_amount_quote": "40.000000000000000001",
        "realized_pnl_quote": "-1.250000000000000001",
        "cum_fees_quote": "0.220000000000000001",
        "executor_ids": ["exec-recovery-1"],
        "last_updated": "2026-09-27T00:00:00+00:00",
    }

    row = position_hold_accounting_from_raw(
        raw,
        expected_account_name="master_account",
        expected_connector_name="kraken",
        expected_trading_pair="BTC-USD",
        expected_controller_id="spectra",
    )

    assert row.buy_amount_quote == Decimal("190.000000000000000001")
    assert row.sell_amount_quote == Decimal("40.000000000000000001")
    assert row.realized_pnl_quote == Decimal("-1.250000000000000001")
    assert row.cum_fees_quote == Decimal("0.220000000000000001")
    assert row.executor_ids == ("exec-recovery-1",)
