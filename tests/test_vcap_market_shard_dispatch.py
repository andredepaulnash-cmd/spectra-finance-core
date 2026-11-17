from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from mcp_servers.hummingbot_api.tools import vcap_execution
from spectra_core.models import CapitalProposal, IntentKind
from spectra_core.vcap_execution import VCAPExecutionDecision, VCAPLoopAction

D = Decimal


class Governor:
    def get_execution_receipt(self, proposal_id):
        return None


def decision(*, connector_name: str) -> VCAPExecutionDecision:
    proposal = CapitalProposal(
        proposal_id="spectra-recovery:vcap:0001",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("20"),
        leverage=D("1"),
        account_name="master_account",
        connector_name=connector_name,
        quote_asset="USD",
    )
    return VCAPExecutionDecision(
        envelope_proposal_id="spectra-recovery",
        sequence=1,
        action=VCAPLoopAction.EXECUTE,
        remaining_authorized_quote=D("80"),
        current_price=D("95"),
        recovery_plan=None,
        tranche_plan=None,
        next_tranche=SimpleNamespace(base_amount=D("0.2")),
        next_proposal=proposal,
        reasons=(),
    )


def test_paper_vcap_shard_uses_governed_market_buy_without_price_cap(monkeypatch):
    monkeypatch.setattr(vcap_execution.settings, "paper_only", True)
    monkeypatch.setattr(vcap_execution, "carve_next_vcap_tranche", lambda *args: True)
    calls = {}

    async def market_buy(client, **kwargs):
        calls.update(kwargs)
        return {"executor_id": "paper-vcap-shard-1"}

    async def forbidden_dca(*args, **kwargs):
        raise AssertionError("paper VCAP must not use the fixed-price DCA path")

    monkeypatch.setattr(vcap_execution, "create_order_executor", market_buy)
    monkeypatch.setattr(vcap_execution, "create_dca_executor", forbidden_dca)

    result = asyncio.run(
        vcap_execution.execute_vcap_decision(
            object(),
            decision=decision(connector_name="kraken_paper_trade"),
            governor=Governor(),
            position_id="position-1",
            buy_fee_allowance_bps=D("60"),
        )
    )

    assert result["executed"] is True
    assert result["result"]["executor_id"] == "paper-vcap-shard-1"
    assert calls["connector_name"] == "kraken_paper_trade"
    assert calls["trading_pair"] == "BTC-USD"
    assert calls["side"] == 1
    assert calls["execution_strategy"] == "MARKET"
    assert calls["amount"] is None
    assert calls["price"] is None
    assert calls["spectra_proposal_id"] == "spectra-recovery:vcap:0001"
    assert calls["spectra_intent"] == "RECOVERY"
    assert calls["spectra_position_id"] == "position-1"
    assert calls["spectra_fee_allowance_bps"] == D("60")
    assert "spectra_max_price_slippage_bps" not in calls


def test_live_vcap_keeps_governed_limit_dca_route(monkeypatch):
    monkeypatch.setattr(vcap_execution.settings, "paper_only", False)
    monkeypatch.setattr(vcap_execution, "carve_next_vcap_tranche", lambda *args: True)
    calls = {}

    async def dca_buy(client, **kwargs):
        calls.update(kwargs)
        return {"executor_id": "live-vcap-limit-1"}

    async def forbidden_market(*args, **kwargs):
        raise AssertionError("live VCAP must retain the verified LIMIT route")

    monkeypatch.setattr(vcap_execution, "create_dca_executor", dca_buy)
    monkeypatch.setattr(vcap_execution, "create_order_executor", forbidden_market)

    result = asyncio.run(
        vcap_execution.execute_vcap_decision(
            object(),
            decision=decision(connector_name="kraken"),
            governor=Governor(),
            position_id="position-1",
            buy_max_price_slippage_bps=D("10"),
            buy_fee_allowance_bps=D("60"),
        )
    )

    assert result["executed"] is True
    assert calls["connector_name"] == "kraken"
    assert calls["amounts_quote"] == ["20"]
    assert calls["prices"] == ["95"]
    assert calls["spectra_max_price_slippage_bps"] == D("10")
    assert calls["spectra_fee_allowance_bps"] == D("60")
