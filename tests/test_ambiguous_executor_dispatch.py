from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from spectra_core.models import BreakerMode
from mcp_servers.hummingbot_api.tools import executor_create as executor_module


class FakeGovernor:
    def __init__(self):
        self.state = "RESERVED"
        self.release_calls = 0

    def cash_authority_snapshot(self):
        return SimpleNamespace(blocked=False, reason="")

    def protected_buy_scope_rejection(self, **kwargs):
        return None

    def commit_entry_submission_if_matches(self, *args, **kwargs):
        self.state = "COMMITTED"
        return True

    def get_decision(self, proposal_id):
        return SimpleNamespace(reserved_quote=Decimal("25"))

    def release(self, *args, **kwargs):
        self.release_calls += 1
        self.state = "RELEASED"

    def reconcile_execution(self, **kwargs):
        raise AssertionError("ambiguous dispatch must not be reconciled as a confirmed executor")


class FakeLedger:
    path = Path("/tmp/spectra-ambiguous-dispatch-ledger.sqlite")

    def selected_recovery_proposal_allowed(self, pair, proposal_id):
        return True


class FakeDustLifecycle:
    def assert_dispatch(self, **kwargs):
        return None


@pytest.mark.parametrize("outcome", ["timeout_after_submit", "success_without_executor_id"])
@pytest.mark.asyncio
async def test_ambiguous_dispatch_keeps_capital_committed_and_never_releases(
    monkeypatch, outcome
):
    governor = FakeGovernor()
    ledger = FakeLedger()
    dust = FakeDustLifecycle()

    monkeypatch.setattr(
        executor_module,
        "settings",
        SimpleNamespace(agent_slug="spectra", paper_only=True),
    )
    monkeypatch.setattr(
        executor_module,
        "executor_preferences",
        SimpleNamespace(merge_with_defaults=lambda executor_type, config: dict(config)),
    )
    monkeypatch.setattr(executor_module, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    monkeypatch.setattr(executor_module, "get_capital_governor", lambda: governor)
    monkeypatch.setattr(executor_module, "get_economic_position_ledger", lambda: ledger)
    monkeypatch.setattr(
        executor_module,
        "get_dust_lifecycle",
        lambda governor=None: dust,
    )
    monkeypatch.setattr(
        executor_module,
        "require_approved_kraken_pair",
        lambda pair, connector_name: None,
    )
    monkeypatch.setattr(
        executor_module,
        "validate_dispatch_transport",
        lambda client: None,
    )

    async def bounded_buy(client, executor_type, config, **kwargs):
        return dict(config), {"protected_buy": True}, {"min_order_size": "0.0001"}

    monkeypatch.setattr(executor_module, "_bounded_protected_buy", bounded_buy)

    async def dispatch(*args, **kwargs):
        if outcome == "timeout_after_submit":
            raise TimeoutError("response lost after request submission")
        return {"status": "accepted"}, SimpleNamespace(receipt_id="receipt-ambiguous")

    monkeypatch.setattr(executor_module, "dispatch_with_receipt", dispatch)

    result = await executor_module.create_executor(
        object(),
        "grid_executor",
        {
            "connector_name": "kraken_paper_trade",
            "trading_pair": "BTC-USD",
            "total_amount_quote": "25",
            "leverage": 1,
            "start_price": 99,
            "end_price": 101,
            "limit_price": 98,
        },
        account_name="master_account",
        controller_id="spectra",
        spectra_proposal_id="proposal-ambiguous",
        spectra_intent="ENTRY",
        spectra_position_id="position-ambiguous",
        spectra_max_price_slippage_bps="10",
        spectra_fee_allowance_bps="60",
    )

    assert result.get("error")
    assert governor.state == "COMMITTED"
    assert governor.release_calls == 0
    if outcome == "success_without_executor_id":
        assert "ambiguous" in result["error"].lower()
        assert result["spectra_dispatch_receipt_id"] == "receipt-ambiguous"
    else:
        assert "response lost after request submission" in result["error"]
