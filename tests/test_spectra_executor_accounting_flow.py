import asyncio
from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools.executors import (
    fetch_and_reconcile_executor_accounting,
)
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)


D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "capital.sqlite",
        CapitalPolicy(
            reserve_floor_quote=D("10"),
            max_total_committed_quote=D("1000"),
            max_pair_quote=D("500"),
            max_position_quote=D("500"),
            max_recovery_quote=D("250"),
            max_leverage=D("1"),
            reservation_ttl_seconds=120,
        ),
    )


def prepare_recovery_executor(g):
    proposal = CapitalProposal(
        proposal_id="flow-recovery",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("25"),
        leverage=D("1"),
    )

    decision = g.reserve(
        proposal,
        CapitalSnapshot(
            free_quote=D("500"),
            committed_quote=D("100"),
            pair_exposure_quote=D("20"),
            position_exposure_quote=D("20"),
            recovery_exposure_quote=D("10"),
        ),
    )
    assert decision.status == DecisionStatus.RESERVED
    assert g.commit(proposal.proposal_id)
    assert g.reconcile_execution(
        proposal_id=proposal.proposal_id,
        executor_id="exec-flow-1",
    )


class Executors:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def get_executor(self, executor_id):
        self.calls.append(executor_id)
        return self.payload


class Client:
    def __init__(self, payload):
        self.executors = Executors(payload)


def test_fetch_and_reconcile_executor_accounting(tmp_path):
    g = governor(tmp_path)
    prepare_recovery_executor(g)

    client = Client(
        {
            "id": "exec-flow-1",
            "executor_type": "grid_executor",
            "status": "RUNNING",
            "filled_amount_quote": "12.50",
            "cum_fees_quote": "0.025",
            "net_pnl_quote": "-0.10",
            "custom_info": {
                "position_size_quote": "9.75",
                "realized_pnl_quote": "0",
            },
        }
    )

    first = asyncio.run(
        fetch_and_reconcile_executor_accounting(
            client,
            executor_id="exec-flow-1",
            governor=g,
        )
    )

    assert client.executors.calls == ["exec-flow-1"]
    assert first.intent == IntentKind.RECOVERY
    assert first.filled_delta_quote == D("12.50")
    assert first.fee_delta_quote == D("0.025")
    assert first.position_size_quote == D("9.75")

    second = asyncio.run(
        fetch_and_reconcile_executor_accounting(
            client,
            executor_id="exec-flow-1",
            governor=g,
        )
    )

    assert second.filled_delta_quote == D("0")
    assert second.fee_delta_quote == D("0")


def test_fetch_and_reconcile_fails_closed_on_wrong_executor(tmp_path):
    g = governor(tmp_path)
    prepare_recovery_executor(g)

    client = Client(
        {
            "id": "wrong-executor",
            "status": "RUNNING",
            "filled_amount_quote": "12.50",
            "cum_fees_quote": "0.025",
        }
    )

    with pytest.raises(
        ValueError,
        match="does not match requested executor",
    ):
        asyncio.run(
            fetch_and_reconcile_executor_accounting(
                client,
                executor_id="exec-flow-1",
                governor=g,
            )
        )

    with g._connect() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*)
            FROM executor_accounting_observations
            """
        ).fetchone()[0]

    assert count == 0
