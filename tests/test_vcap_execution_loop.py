import asyncio
import time
from dataclasses import replace
from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools import executor_create
from mcp_servers.hummingbot_api.tools.vcap_execution import (
    execute_vcap_decision,
)
from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.kraken_market_fabric import KrakenTradingRule
from spectra_core.models import (
    BreakerMode,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.recovery_fingerprint import VCAPPositionState
from spectra_core.vcap_execution import (
    VCAPExecutionDecision,
    VCAPLoopAction,
    carve_next_vcap_tranche,
    replan_vcap_sequence,
)
from spectra_core.vcap_tranches import VCAPTranche

D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "vcap-loop.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("1000"),
            max_pair_quote=D("1000"),
            max_position_quote=D("1000"),
            max_recovery_quote=D("500"),
            max_leverage=D("1"),
            reservation_ttl_seconds=120,
        ),
    )


def reserve_envelope(g, amount="100"):
    proposal = CapitalProposal(
        proposal_id="vcap-envelope-1",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D(amount),
        leverage=D("1"),
    )
    decision = g.reserve(
        proposal,
        CapitalSnapshot(
            free_quote=D("1000"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
            recovery_exposure_quote=D("0"),
        ),
    )
    assert decision.status == DecisionStatus.RESERVED
    return proposal


def observation(timestamp, price):
    return AssetObservation(
        trading_pair="BTC-USD",
        timestamp=timestamp,
        price=price,
        quote_volume_24h=1_000_000,
        spread_bps=5.0,
        depth_quote=50_000.0,
        depth_imbalance=0.1,
        microprice_edge_bps=0.5,
    )


def reversal_fingerprint():
    price = 100.0
    fp = AssetFingerprint.cold_start(observation(1_000, price))
    for i in range(1, 41):
        price = 101.0 if i % 2 else 100.0
        fp = fp.update(observation(1_000 + i * 60, price))
    return fp


def rule(min_notional="10"):
    return KrakenTradingRule(
        trading_pair="BTC-USD",
        min_order_size=D("0.01"),
        min_notional_size=D(min_notional),
        min_order_value=D("0"),
        min_price_increment=D("0.01"),
        min_base_amount_increment=D("0.001"),
        min_quote_amount_increment=D("0.01"),
        supports_limit_orders=True,
        supports_market_orders=True,
    )


def test_replan_executes_only_one_currently_justified_tranche():
    fp = reversal_fingerprint()
    decision = replan_vcap_sequence(
        envelope_proposal_id="vcap-envelope-1",
        sequence=1,
        pair="BTC-USD",
        seed_quote=D("20"),
        remaining_authorized_quote=D("100"),
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1100"),
            current_price=D("100"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        current_price=D("100"),
        now=fp.last_seen_at,
    )

    assert decision.action == VCAPLoopAction.EXECUTE
    assert decision.next_proposal is not None
    assert decision.next_tranche is not None
    assert decision.next_proposal.proposal_id == "vcap-envelope-1:vcap:0001"
    assert decision.next_proposal.amount_quote == decision.next_tranche.quote_amount
    assert "next_tranche_revalidated" in decision.reasons


def test_replan_stops_when_recovery_target_is_already_satisfied():
    fp = reversal_fingerprint()
    decision = replan_vcap_sequence(
        envelope_proposal_id="vcap-envelope-1",
        sequence=2,
        pair="BTC-USD",
        seed_quote=D("20"),
        remaining_authorized_quote=D("80"),
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1040"),
            current_price=D("100"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        current_price=D("100"),
        now=fp.last_seen_at,
    )

    assert decision.action == VCAPLoopAction.STOP
    assert decision.next_proposal is None
    assert "recovery_target_satisfied" in decision.reasons


def test_replan_holds_when_behavior_evidence_is_stale():
    fp = reversal_fingerprint()
    decision = replan_vcap_sequence(
        envelope_proposal_id="vcap-envelope-1",
        sequence=2,
        pair="BTC-USD",
        seed_quote=D("20"),
        remaining_authorized_quote=D("80"),
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1100"),
            current_price=D("100"),
        ),
        fingerprint=fp,
        trading_rule=rule(),
        current_price=D("100"),
        now=fp.last_seen_at + 4 * 60 * 60,
    )

    assert decision.action == VCAPLoopAction.HOLD
    assert decision.next_proposal is None
    assert "fresh_recovery_evidence_required" in decision.reasons


def test_replan_holds_when_remaining_capital_cannot_form_sellable_tranche():
    fp = reversal_fingerprint()
    decision = replan_vcap_sequence(
        envelope_proposal_id="vcap-envelope-1",
        sequence=2,
        pair="BTC-USD",
        seed_quote=D("20"),
        remaining_authorized_quote=D("20"),
        position=VCAPPositionState(
            pair="BTC-USD",
            position_base=D("10"),
            cost_basis_quote=D("1100"),
            current_price=D("100"),
        ),
        fingerprint=fp,
        trading_rule=rule(min_notional="50"),
        current_price=D("100"),
        now=fp.last_seen_at,
    )

    assert decision.action == VCAPLoopAction.HOLD
    assert decision.next_proposal is None
    assert "no_sellable_tranche_now" in decision.reasons


def test_atomic_carve_preserves_total_protected_capital(tmp_path):
    g = governor(tmp_path)
    reserve_envelope(g, "100")

    child = CapitalProposal(
        proposal_id="vcap-envelope-1:vcap:0001",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("20"),
        leverage=D("1"),
    )
    decision = VCAPExecutionDecision(
        envelope_proposal_id="vcap-envelope-1",
        sequence=1,
        action=VCAPLoopAction.EXECUTE,
        remaining_authorized_quote=D("100"),
        current_price=D("100"),
        recovery_plan=None,
        tranche_plan=None,
        next_tranche=VCAPTranche(
            sequence=1,
            quote_amount=D("20"),
            base_amount=D("0.2"),
            weight_ratio=D("0.2"),
        ),
        next_proposal=child,
        reasons=(),
    )

    before = g.protected_quote()
    assert carve_next_vcap_tranche(g, decision)
    after = g.protected_quote()

    assert before == D("100")
    assert after == D("100")
    assert g.get_decision("vcap-envelope-1").reserved_quote == D("80")
    assert g.get_decision(child.proposal_id).reserved_quote == D("20")

    # Exact replay cannot carve another $20.
    assert carve_next_vcap_tranche(g, decision)
    assert g.protected_quote() == D("100")
    assert g.get_decision("vcap-envelope-1").reserved_quote == D("80")


def test_conflicting_child_replay_fails_closed(tmp_path):
    g = governor(tmp_path)
    reserve_envelope(g, "100")

    first = CapitalProposal(
        proposal_id="vcap-envelope-1:vcap:0001",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("20"),
        leverage=D("1"),
    )
    assert g.carve_reserved_recovery_tranche(
        envelope_proposal_id="vcap-envelope-1",
        tranche_proposal=first,
    )

    conflict = CapitalProposal(
        proposal_id=first.proposal_id,
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("25"),
        leverage=D("1"),
    )
    assert not g.carve_reserved_recovery_tranche(
        envelope_proposal_id="vcap-envelope-1",
        tranche_proposal=conflict,
    )
    assert g.get_decision("vcap-envelope-1").reserved_quote == D("80")


class Connectors:
    async def get_trading_rules(self, *, connector_name, trading_pairs):
        assert connector_name == "kraken"
        assert trading_pairs == ["BTC-USD"]
        return {
            "BTC-USD": {
                "min_order_size": "0.01",
                "min_notional_size": "10",
                "min_price_increment": "0.01",
                "min_base_amount_increment": "0.01",
                "min_quote_amount_increment": "0.01",
                "supports_limit_orders": True,
            }
        }


class Executors:
    def __init__(self):
        self.calls = []

    async def create_executor(self, executor_config, account_name, controller_id):
        self.calls.append((executor_config, account_name, controller_id))
        return {"executor_id": executor_config["id"]}


class Client:
    def __init__(self):
        self.connectors = Connectors()
        self.executors = Executors()


def executable_decision():
    proposal = CapitalProposal(
        proposal_id="vcap-envelope-1:vcap:0001",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("20"),
        leverage=D("1"),
    )
    return VCAPExecutionDecision(
        envelope_proposal_id="vcap-envelope-1",
        sequence=1,
        action=VCAPLoopAction.EXECUTE,
        remaining_authorized_quote=D("100"),
        current_price=D("100"),
        recovery_plan=None,
        tranche_plan=None,
        next_tranche=VCAPTranche(
            sequence=1,
            quote_amount=D("20"),
            base_amount=D("0.2"),
            weight_ratio=D("0.2"),
        ),
        next_proposal=proposal,
        reasons=("next_tranche_revalidated",),
    )


@pytest.mark.parametrize(
    "native_quote,reference_price",
    [("20", "100"), ("20.1234567890123456789", "100.1234567890123456789")],
)
def test_execute_vcap_tranche_uses_exact_execution_gate_and_replay_is_safe(
    monkeypatch,
    tmp_path,
    native_quote,
    reference_price,
):
    g = governor(tmp_path)
    reserve_envelope(g, "100")
    client = Client()

    executor_create.trading_rules_cache.clear()
    monkeypatch.setattr(executor_create.settings, "agent_slug", "spectra")
    monkeypatch.setattr(
        executor_create,
        "get_breaker_mode",
        lambda: BreakerMode.NORMAL,
    )
    monkeypatch.setattr(executor_create, "get_capital_governor", lambda: g)
    ledger = EconomicPositionLedger(tmp_path / "economic.sqlite3")
    ledger.open_position(position_id="position", pair="BTC-USD", created_at=1000)
    monkeypatch.setattr(executor_create, "get_economic_position_ledger", lambda: ledger)
    from mcp_servers.hummingbot_api.tools import execution_book

    async def fresh_book(rule):
        observed = time.time()
        return execution_book.FreshVenueBook(
            rule.trading_pair,
            "XBTUSD",
            D("99.99"),
            D("100"),
            ((D("99.99"), D("10")),),
            ((D("100"), D("10")),),
            observed - 0.01,
            observed,
            "test-fresh-Kraken-Depth",
            "c" * 64,
            venue_min_order_size=D("0.01"),
            venue_min_cost_quote=D("10"),
            venue_base_asset="BTC",
            venue_quote_asset="USD",
            venue_rules_observed_at=observed,
            venue_rules_sha256="e" * 64,
        )

    monkeypatch.setattr(execution_book, "fetch_protected_kraken_book", fresh_book)
    decision = executable_decision()
    decision = replace(
        decision,
        current_price=D(reference_price),
        next_proposal=replace(decision.next_proposal, amount_quote=D(native_quote)),
        next_tranche=replace(
            decision.next_tranche,
            quote_amount=D(native_quote),
            base_amount=D(native_quote) / D(reference_price),
        ),
    )
    first = asyncio.run(
        execute_vcap_decision(
            client,
            decision=decision,
            governor=g,
            position_id="position",
            buy_max_price_slippage_bps=D("0"),
            buy_fee_allowance_bps=D("0"),
        )
    )

    assert first["executed"] is True
    assert len(client.executors.calls) == 1
    config = client.executors.calls[0][0]
    assert config["type"] == "order_executor"
    assert config["execution_strategy"] == "LIMIT"
    assert D(str(config["price"])) == D("100")
    protected = g.get_protected_buy_submission(decision.next_proposal.proposal_id)
    assert D(protected["policy"]["reference_price"]) == D(reference_price)
    assert D(str(config["amount"])) == D("0.2")
    assert D(
        g.get_protected_buy_submission(decision.next_proposal.proposal_id)[
            "authorized_quote_budget"
        ]
    ) == D(native_quote)
    assert g.get_execution_receipt(decision.next_proposal.proposal_id) == config["id"]
    assert g.get_decision("vcap-envelope-1").reserved_quote == D("100") - D(
        native_quote
    )

    replay = asyncio.run(
        execute_vcap_decision(
            client,
            decision=decision,
            governor=g,
            position_id="position",
            buy_max_price_slippage_bps=D("0"),
            buy_fee_allowance_bps=D("0"),
        )
    )

    assert replay["executed"] is True
    assert replay["idempotent_replay"] is True
    assert replay["executor_id"] == config["id"]
    assert len(client.executors.calls) == 1
