import asyncio
from decimal import Decimal

import pytest

from spectra_core import (
    CapitalGovernor,
    CapitalPolicy,
    DecisionStatus,
    IntentKind,
)
from spectra_core.kraken_authorization import reserve_kraken_capital


D = Decimal


class Portfolio:
    def __init__(self, state):
        self.state = state
        self.calls = []

    async def get_state(self, **kwargs):
        self.calls.append(kwargs)
        return self.state


class Client:
    def __init__(self, state):
        self.portfolio = Portfolio(state)


def governor(tmp_path, *, recovery="100"):
    return CapitalGovernor(
        tmp_path / "capital.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("100"),
            max_total_committed_quote=D("500"),
            max_pair_quote=D("150"),
            max_position_quote=D("150"),
            max_recovery_quote=D(recovery),
            max_leverage=D("1"),
        ),
    )


def usd_client(available=500):
    return Client({
        "master_account": {
            "kraken": [{
                "token": "USD",
                "units": available,
                "available_units": available,
                "value": available,
            }]
        }
    })


def test_live_snapshot_can_create_reserved_entry_claim(tmp_path):
    g = governor(tmp_path)
    client = usd_client(500)

    auth = asyncio.run(
        reserve_kraken_capital(
            client,
            account_name="master_account",
            trading_pair="btc-usd",
            intent="ENTRY",
            amount_quote="50",
            governor=g,
        )
    )

    assert auth.proposal.proposal_id.startswith("spectra-entry-")
    assert auth.proposal.pair == "BTC-USD"
    assert auth.proposal.intent == IntentKind.ENTRY
    assert auth.proposal.amount_quote == D("50")
    assert auth.proposal.leverage == D("1")

    assert auth.snapshot.free_quote == D("500")
    assert auth.decision.status == DecisionStatus.RESERVED
    assert g.get_decision(auth.proposal.proposal_id).status == DecisionStatus.RESERVED


def test_live_snapshot_can_be_rejected_by_reserve_floor(tmp_path):
    g = governor(tmp_path)
    client = usd_client(120)

    auth = asyncio.run(
        reserve_kraken_capital(
            client,
            account_name="master_account",
            trading_pair="BTC-USD",
            intent="ENTRY",
            amount_quote="50",
            governor=g,
        )
    )

    assert auth.decision.status == DecisionStatus.REJECTED
    assert "reserve floor" in auth.decision.reason


def test_recovery_claim_obeys_recovery_budget(tmp_path):
    g = governor(tmp_path, recovery="40")
    client = usd_client(500)

    auth = asyncio.run(
        reserve_kraken_capital(
            client,
            account_name="master_account",
            trading_pair="BTC-USD",
            intent="RECOVERY",
            amount_quote="50",
            governor=g,
        )
    )

    assert auth.proposal.intent == IntentKind.RECOVERY
    assert auth.decision.status == DecisionStatus.REJECTED
    assert "recovery budget" in auth.decision.reason


def test_non_usd_quote_is_refused_before_account_read(tmp_path):
    g = governor(tmp_path)
    client = usd_client(500)

    with pytest.raises(ValueError, match="USD quote pairs only"):
        asyncio.run(
            reserve_kraken_capital(
                client,
                account_name="master_account",
                trading_pair="BTC-USDC",
                intent="ENTRY",
                amount_quote="50",
                governor=g,
            )
        )

    assert client.portfolio.calls == []


def test_authorization_threads_governor_recovery_attribution(
    monkeypatch,
    tmp_path,
):
    g = governor(tmp_path)
    client = usd_client(500)

    monkeypatch.setattr(
        g,
        "current_recovery_exposure_quote",
        lambda: D("35"),
    )

    auth = asyncio.run(
        reserve_kraken_capital(
            client,
            account_name="master_account",
            trading_pair="BTC-USD",
            intent="ENTRY",
            amount_quote="50",
            governor=g,
        )
    )

    assert auth.snapshot.recovery_exposure_quote == D("35")
    assert auth.snapshot.committed_quote == D("0")
    assert auth.snapshot.pair_exposure_quote == D("0")
    assert auth.snapshot.position_exposure_quote == D("0")
    assert auth.decision.status == DecisionStatus.RESERVED
