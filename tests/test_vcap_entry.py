from decimal import Decimal
from types import SimpleNamespace

import pytest

from spectra_core import vcap_entry
from spectra_core.models import (
    CapitalDecision,
    CapitalProposal,
    DecisionStatus,
    IntentKind,
)


def scan_result(*, as_of=1_000.0, pair="BTC-USD", **candidate_changes):
    candidate = {
        "trading_pair": pair,
        "quote_asset": "USD",
        "current_price": 100.0,
        "volume_24h_usd": 25_000_000.0,
        "selection_score": 87.5,
        "eligible": True,
        "capital_authorized": False,
    }
    candidate.update(candidate_changes)
    return {
        "source": "Kraken spot market data via Hummingbot API",
        "as_of": as_of,
        "paper_only": True,
        "orders_created": 0,
        "lists": {"mean_reversion_candidates": [candidate]},
    }


def authorization():
    proposal = CapitalProposal(
        proposal_id="spectra-entry-test",
        pair="BTC-USD",
        intent=IntentKind.ENTRY,
        amount_quote=Decimal("20"),
        leverage=Decimal("1"),
        account_name="master_account",
        connector_name="kraken_paper_trade",
        quote_asset="USD",
    )
    decision = CapitalDecision(
        proposal_id=proposal.proposal_id,
        status=DecisionStatus.RESERVED,
        reason="reserved by test Governor",
        reserved_quote=Decimal("20"),
        available_after_quote=Decimal("480"),
    )
    return SimpleNamespace(
        proposal=proposal,
        decision=decision,
        economic_position_id="spectra-position-spectra-entry-test",
    )


def configure(monkeypatch, *, approved=True):
    monkeypatch.setattr(vcap_entry, "spectra_paper_only_enabled", lambda: True)
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setattr(
        vcap_entry,
        "load_approved_kraken_assets",
        lambda: SimpleNamespace(
            configured=approved,
            pairs=frozenset({"BTC-USD"}) if approved else frozenset(),
        ),
    )


@pytest.mark.asyncio
async def test_fresh_shortlist_candidate_reserves_exactly_one_twenty_dollar_paper_entry(
    monkeypatch,
):
    configure(monkeypatch)
    calls = []

    async def reserve(*args, **kwargs):
        calls.append(kwargs)
        return authorization()

    monkeypatch.setattr(vcap_entry, "reserve_kraken_capital", reserve)

    result = await vcap_entry.reserve_vcap_candidate_entry(
        object(),
        scan_result=scan_result(),
        trading_pair="xbt-usd",
        account_name="master_account",
        now=1_000.0,
    )

    assert result.pair == "BTC-USD"
    assert result.proposal_id == "spectra-entry-test"
    assert result.position_id == "spectra-position-spectra-entry-test"
    assert result.amount_quote == Decimal("20")
    assert result.decision_status == DecisionStatus.RESERVED
    assert result.paper_only is True
    assert result.order_dispatched is False
    assert len(calls) == 1
    assert calls[0]["intent"] == IntentKind.ENTRY
    assert calls[0]["amount_quote"] == Decimal("20")


@pytest.mark.asyncio
async def test_stale_shortlist_is_rejected_before_governor_reservation(monkeypatch):
    configure(monkeypatch)
    calls = []

    async def reserve(*args, **kwargs):
        calls.append(kwargs)
        return authorization()

    monkeypatch.setattr(vcap_entry, "reserve_kraken_capital", reserve)

    with pytest.raises(ValueError, match="stale"):
        await vcap_entry.reserve_vcap_candidate_entry(
            object(),
            scan_result(as_of=600.0),
            trading_pair="BTC-USD",
            account_name="master_account",
            now=1_000.0,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_non_candidate_and_ineligible_assets_are_rejected(monkeypatch):
    configure(monkeypatch)

    async def reserve(*args, **kwargs):
        raise AssertionError("Governor must not be called for a rejected candidate")

    monkeypatch.setattr(vcap_entry, "reserve_kraken_capital", reserve)

    with pytest.raises(ValueError, match="not in the current"):
        await vcap_entry.reserve_vcap_candidate_entry(
            object(),
            scan_result(),
            trading_pair="ETH-USD",
            account_name="master_account",
            now=1_000.0,
        )

    with pytest.raises(ValueError, match="eligibility"):
        await vcap_entry.reserve_vcap_candidate_entry(
            object(),
            scan_result(eligible=False),
            trading_pair="BTC-USD",
            account_name="master_account",
            now=1_000.0,
        )


@pytest.mark.asyncio
async def test_candidate_requires_explicit_governor_asset_approval(monkeypatch):
    configure(monkeypatch, approved=False)

    async def reserve(*args, **kwargs):
        raise AssertionError("Governor must not be called without asset approval")

    monkeypatch.setattr(vcap_entry, "reserve_kraken_capital", reserve)

    with pytest.raises(ValueError, match="explicit Governor-approved"):
        await vcap_entry.reserve_vcap_candidate_entry(
            object(),
            scan_result(),
            trading_pair="BTC-USD",
            account_name="master_account",
            now=1_000.0,
        )


@pytest.mark.asyncio
async def test_live_execution_flag_blocks_vcap_candidate_admission(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "true")

    async def reserve(*args, **kwargs):
        raise AssertionError("Governor must not be called when live execution is enabled")

    monkeypatch.setattr(vcap_entry, "reserve_kraken_capital", reserve)

    with pytest.raises(ValueError, match="live execution"):
        await vcap_entry.reserve_vcap_candidate_entry(
            object(),
            scan_result(),
            trading_pair="BTC-USD",
            account_name="master_account",
            now=1_000.0,
        )
