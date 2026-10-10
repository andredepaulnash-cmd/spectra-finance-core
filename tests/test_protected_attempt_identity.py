"""Exercise authorization identity through real Spectra creation and durable journals."""

import asyncio
import hashlib
import json
import time
from decimal import Decimal as D

import pytest

from mcp_servers.hummingbot_api.tools import execution_book, executor_create
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.models import (
    BreakerMode,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)

IDENTITY = {
    "proposal_id": "entry-generation-1",
    "account_name": "master_account",
    "controller_id": "spectra",
    "position_id": "position-1",
    "intent": "ENTRY",
    "connector_name": "kraken",
    "trading_pair": "XRP-USD",
}


def governor(path):
    return CapitalGovernor(
        path,
        CapitalPolicy(
            reserve_floor_quote=D(0),
            max_total_committed_quote=D(5000),
            max_pair_quote=D(5000),
            max_position_quote=D(5000),
            max_recovery_quote=D(2000),
            max_leverage=D(1),
        ),
    )


class FakeClient:
    def __init__(self, capital, outcome="success", proposal_id=IDENTITY["proposal_id"]):
        self.capital = capital
        self.outcome = outcome
        self.proposal_id = proposal_id
        self.calls = []
        self.rule_calls = []
        self.connectors = self
        self.executors = self

    async def get_trading_rules(self, connector_name, trading_pairs):
        self.rule_calls.append((connector_name, trading_pairs))
        return {
            pair: {
                "min_order_size": "1",
                "min_notional_size": "1",
                "min_base_amount_increment": "0.1",
                "min_price_increment": "0.01",
                "min_quote_amount_increment": "0.01",
                "supports_limit_orders": True,
            }
            for pair in trading_pairs
        }

    async def create_executor(self, executor_config, account_name, controller_id):
        # Validate at the mutation boundary, not after the response is returned.
        stored = self.capital.get_protected_buy_submission(self.proposal_id)
        assert stored["capital_status"] == "COMMITTED"
        assert stored["config"] == executor_config
        assert stored["config"]["id"] == executor_config["id"]
        self.calls.append((dict(executor_config), account_name, controller_id))
        if self.outcome == "timeout":
            raise TimeoutError("synthetic ambiguous create")
        if self.outcome == "missing_ack":
            return {"status": "accepted"}
        # The pinned API returns the supplied typed config ID.
        return {"executor_id": executor_config["id"]}


@pytest.fixture
def admitted(monkeypatch, tmp_path):
    capital = governor(tmp_path / "capital.sqlite")
    ledger = EconomicPositionLedger(tmp_path / "economic.sqlite")
    ledger.open_position(position_id="position-1", pair="XRP-USD", created_at=1000)
    proposal = CapitalProposal(
        proposal_id=IDENTITY["proposal_id"],
        pair="XRP-USD",
        intent=IntentKind.ENTRY,
        amount_quote=D(50),
        leverage=D(1),
    )
    assert (
        capital.reserve(proposal, CapitalSnapshot(free_quote=D(5000))).status
        == DecisionStatus.RESERVED
    )
    monkeypatch.setattr(executor_create.settings, "agent_slug", "spectra")
    monkeypatch.setattr(executor_create, "get_capital_governor", lambda: capital)
    monkeypatch.setattr(executor_create, "get_economic_position_ledger", lambda: ledger)
    monkeypatch.setattr(executor_create, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    monkeypatch.setattr(
        executor_create.executor_preferences,
        "merge_with_defaults",
        lambda executor_type, config: dict(config),
    )

    async def book(rule):
        observed = time.time()
        return execution_book.FreshVenueBook(
            "XRP-USD",
            "XRPUSD",
            D("0.99"),
            D("1"),
            ((D("0.99"), D("1000")),),
            ((D("1"), D("1000")),),
            observed,
            observed,
            "synthetic-Kraken-Depth",
            "a" * 64,
            venue_min_order_size=D("1"),
            venue_min_cost_quote=D("1"),
            venue_base_asset="XRP",
            venue_quote_asset="USD",
            venue_rules_observed_at=observed,
            venue_rules_sha256="b" * 64,
        )

    monkeypatch.setattr(execution_book, "fetch_protected_kraken_book", book)
    return capital, proposal


def create(client, *, config_updates=None, argument_updates=None):
    config = {
        "connector_name": "kraken",
        "trading_pair": "XRP-USD",
        "side": 1,
        "prices": [1],
        "amounts_quote": [50],
        "leverage": 1,
    }
    config.update(config_updates or {})
    arguments = {
        "account_name": "master_account",
        "controller_id": "spectra",
        "spectra_proposal_id": IDENTITY["proposal_id"],
        "spectra_position_id": "position-1",
        "spectra_intent": "ENTRY",
        "spectra_max_price_slippage_bps": 0,
        "spectra_fee_allowance_bps": 0,
    }
    arguments.update(argument_updates or {})
    return asyncio.run(
        executor_create.create_executor(client, "dca_executor", config, **arguments)
    )


def test_backend_identity_is_committed_before_submission_and_bound_to_receipt(admitted):
    capital, proposal = admitted
    client = FakeClient(capital)
    result = create(client)
    assert "error" not in result
    expected = executor_create._protected_buy_attempt_id(**IDENTITY)
    assert result["executor_id"] == expected
    assert result["config_used"]["id"] == expected
    stored = capital.get_protected_buy_submission(proposal.proposal_id)
    assert stored["config"]["id"] == expected
    assert stored["executor_id"] == expected
    assert stored["authorized_quote_budget"] == "50"
    assert stored["config"]["amount"] == "50"
    assert stored["config"]["price"] == "1"
    assert stored["policy"]["max_total_quote"] == "50"
    provenance = capital.get_execution_provenance(proposal.proposal_id)
    assert provenance.executor_id == expected
    assert provenance.authorized_quote == D(50)


@pytest.mark.parametrize("outcome", ["success", "timeout", "missing_ack"])
def test_replay_and_restart_do_not_submit_another_order(admitted, monkeypatch, outcome):
    capital, proposal = admitted
    client = FakeClient(capital, outcome)
    first = create(client)
    assert ("error" in first) == (outcome != "success")
    expected = client.calls[0][0]["id"]
    retry = create(client, config_updates={"id": "attacker-new-native-id"})
    assert "error" in retry
    assert len(client.calls) == 1
    restarted = governor(capital.db_path)
    monkeypatch.setattr(executor_create, "get_capital_governor", lambda: restarted)
    after_restart = create(
        client,
        config_updates={"id": "another-id", "timestamp": time.time()},
        argument_updates={"spectra_max_price_slippage_bps": 20},
    )
    assert "error" in after_restart
    assert len(client.calls) == 1
    assert (
        restarted.get_protected_buy_submission(proposal.proposal_id)["config"]["id"]
        == expected
    )
    assert restarted.get_decision(proposal.proposal_id).status == (
        DecisionStatus.RECONCILED if outcome == "success" else DecisionStatus.COMMITTED
    )


@pytest.mark.parametrize("wrong_id", ["unrelated-executor", 123, {"id": "wrong"}])
def test_wrong_ack_preserves_committed_claim_without_reconciliation_or_retry(
    admitted, monkeypatch, wrong_id
):
    capital, proposal = admitted
    client = FakeClient(capital)
    original_create = client.create_executor

    async def wrong_ack(**payload):
        await original_create(**payload)
        return {"executor_id": wrong_id, "id": payload["executor_config"]["id"]}

    client.create_executor = wrong_ack
    reconciliations = []

    def must_not_reconcile(**receipt):
        reconciliations.append(receipt)
        raise AssertionError("wrong ACK must not reach receipt reconciliation")

    monkeypatch.setattr(capital, "reconcile_execution", must_not_reconcile)
    result = create(client)
    assert (
        "different from the committed protected BUY attempt"
        if isinstance(wrong_id, str)
        else "without a non-empty string executor_id"
    ) in result["error"]
    assert result["expected_executor_id"] == client.calls[0][0]["id"]
    assert result["result"]["executor_id"] == wrong_id
    assert not reconciliations
    assert capital.get_execution_receipt(proposal.proposal_id) is None
    assert capital.get_decision(proposal.proposal_id).status == DecisionStatus.COMMITTED
    assert (
        capital.get_protected_buy_submission(proposal.proposal_id)["submission_state"]
        == "COMMITTED"
    )
    assert "error" in create(client)
    restarted = governor(capital.db_path)
    monkeypatch.setattr(executor_create, "get_capital_governor", lambda: restarted)
    assert "error" in create(client, config_updates={"id": "another-id"})
    assert len(client.calls) == 1
    assert restarted.get_execution_receipt(proposal.proposal_id) is None
    assert (
        restarted.get_decision(proposal.proposal_id).status == DecisionStatus.COMMITTED
    )


@pytest.mark.parametrize(
    "schema",
    ["id_only", "null", "empty", "false", "zero", "none", "list", "text", "integer"],
)
def test_missing_primary_or_nonmapping_ack_is_structured_ambiguous_without_fallback(
    admitted, schema
):
    capital, proposal = admitted
    client = FakeClient(capital)
    original_create = client.create_executor

    async def malformed_ack(**payload):
        await original_create(**payload)
        expected = payload["executor_config"]["id"]
        if schema == "id_only":
            return {"id": expected}
        if schema in {"null", "empty", "false", "zero"}:
            primary = {"null": None, "empty": "", "false": False, "zero": 0}[schema]
            return {"executor_id": primary, "id": expected}
        return {"none": None, "list": [], "text": "accepted", "integer": 123}[schema]

    client.create_executor = malformed_ack
    result = create(client)
    assert "without a non-empty string executor_id" in result["error"]
    assert result["expected_executor_id"] == client.calls[0][0]["id"]
    assert capital.get_decision(proposal.proposal_id).status == DecisionStatus.COMMITTED
    assert capital.get_execution_receipt(proposal.proposal_id) is None
    assert len(client.calls) == 1
    assert "error" in create(client)
    assert len(client.calls) == 1


@pytest.mark.parametrize("intent", [IntentKind.ENTRY, IntentKind.RECOVERY])
def test_governor_binds_only_saved_attempt_id_after_timeout_and_restart(
    admitted, intent
):
    capital, proposal = admitted
    if intent == IntentKind.RECOVERY:
        proposal = CapitalProposal(
            proposal_id="recovery-generation-1",
            pair="XRP-USD",
            intent=IntentKind.RECOVERY,
            amount_quote=D(50),
            leverage=D(1),
        )
        assert (
            capital.reserve(proposal, CapitalSnapshot(free_quote=D(5000))).status
            == DecisionStatus.RESERVED
        )
    client = FakeClient(capital, "timeout", proposal_id=proposal.proposal_id)
    result = create(
        client,
        argument_updates={
            "spectra_proposal_id": proposal.proposal_id,
            "spectra_intent": intent.value,
        },
    )
    assert "synthetic ambiguous create" in result["error"]
    expected = client.calls[0][0]["id"]
    receipt = dict(
        proposal_id=proposal.proposal_id,
        executor_id=expected,
        account_name=IDENTITY["account_name"],
        connector_name=IDENTITY["connector_name"],
        controller_id=IDENTITY["controller_id"],
    )
    for wrong in ("unrelated-executor", " " + expected + " "):
        with pytest.raises(RuntimeError, match="immutable protected BUY attempt"):
            capital.reconcile_execution(**{**receipt, "executor_id": wrong})
        assert (
            capital.get_decision(proposal.proposal_id).status
            == DecisionStatus.COMMITTED
        )
        assert capital.get_execution_receipt(proposal.proposal_id) is None
        assert capital.get_execution_provenance(proposal.proposal_id) is None
    restarted = governor(capital.db_path)
    with pytest.raises(RuntimeError, match="immutable protected BUY attempt"):
        restarted.reconcile_execution(
            **{**receipt, "executor_id": "other-after-restart"}
        )
    assert restarted.reconcile_execution(**receipt)
    assert restarted.reconcile_execution(**receipt)
    assert restarted.get_execution_receipt(proposal.proposal_id) == expected
    assert (
        restarted.get_execution_provenance(proposal.proposal_id).executor_id == expected
    )
    with pytest.raises(RuntimeError, match="immutable protected BUY attempt"):
        restarted.reconcile_execution(
            **{**receipt, "executor_id": "conflicting-replay"}
        )
    assert restarted.get_execution_receipt(proposal.proposal_id) == expected
    assert (
        restarted.get_decision(proposal.proposal_id).status == DecisionStatus.RECONCILED
    )
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "malformation",
    [
        "missing_id",
        "empty_id",
        "nonstring_id",
        "padded_id",
        "control_id",
        "overlong_id",
        "surrogate_id",
        "nonmapping",
        "invalid_json",
        "hash_corrupt",
    ],
)
def test_malformed_saved_protected_config_never_grants_a_receipt(
    admitted, malformation
):
    capital, proposal = admitted
    client = FakeClient(capital, "timeout")
    assert "error" in create(client)
    config = dict(client.calls[0][0])
    executor_id = config["id"]
    if malformation == "missing_id":
        config.pop("id")
    elif malformation == "empty_id":
        config["id"] = ""
    elif malformation == "nonstring_id":
        config["id"] = 123
    elif malformation == "padded_id":
        config["id"] = " " + executor_id + " "
    elif malformation == "control_id":
        config["id"] = "bad\x00identity"
    elif malformation == "overlong_id":
        config["id"] = "a" * 129
    elif malformation == "surrogate_id":
        config["id"] = "bad\ud800identity"
    elif malformation == "nonmapping":
        config = []
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    if malformation == "invalid_json":
        payload = "{"
    digest = hashlib.sha256(payload.encode()).hexdigest()
    if malformation == "hash_corrupt":
        digest = "0" * 64
    with capital._connect() as conn:
        conn.execute(
            "UPDATE protected_buy_submissions SET config_json=?,config_sha256=? "
            "WHERE proposal_id=?",
            (payload, digest, proposal.proposal_id),
        )
    restarted = governor(capital.db_path)
    with pytest.raises(ValueError, match="protected BUY committed"):
        restarted.reconcile_execution(
            proposal_id=proposal.proposal_id,
            executor_id=executor_id,
            account_name=IDENTITY["account_name"],
            connector_name=IDENTITY["connector_name"],
            controller_id=IDENTITY["controller_id"],
        )
    assert (
        restarted.get_decision(proposal.proposal_id).status == DecisionStatus.COMMITTED
    )
    assert restarted.get_execution_receipt(proposal.proposal_id) is None
    assert restarted.get_execution_provenance(proposal.proposal_id) is None
    assert len(client.calls) == 1


def test_legacy_nonprotected_governor_receipt_remains_supported(admitted):
    capital, proposal = admitted
    assert capital.commit(proposal.proposal_id)
    assert capital.get_protected_buy_submission(proposal.proposal_id) is None
    assert capital.reconcile_execution(
        proposal_id=proposal.proposal_id, executor_id="legacy-generated-executor"
    )
    assert (
        capital.get_execution_receipt(proposal.proposal_id)
        == "legacy-generated-executor"
    )


@pytest.mark.parametrize("native_id", ["caller-id", "", False, {"id": "bypass"}])
def test_caller_native_ids_cannot_replace_authorized_identity(admitted, native_id):
    capital, _ = admitted
    result = create(FakeClient(capital), config_updates={"id": native_id})
    assert "error" not in result
    assert result["config_used"]["id"] == executor_create._protected_buy_attempt_id(
        **IDENTITY
    )


def test_saved_native_identity_and_controller_cannot_replace_actual_scope(
    admitted, monkeypatch
):
    capital, _ = admitted
    monkeypatch.setattr(
        executor_create.executor_preferences,
        "merge_with_defaults",
        lambda executor_type, config: {
            "id": "saved-native-id",
            "controller_id": "saved-other-controller",
            **config,
        },
    )
    result = create(
        FakeClient(capital),
        argument_updates={"controller_id": "caller-other-controller"},
    )
    assert "error" not in result
    assert result["controller_id"] == "spectra"
    assert result["config_used"]["id"] == executor_create._protected_buy_attempt_id(
        **IDENTITY
    )
    assert "controller_id" not in result["config_used"]


@pytest.mark.parametrize(
    "argument,value",
    [
        ("spectra_proposal_id", None),
        ("spectra_proposal_id", 123),
        ("spectra_proposal_id", "bad\x00proposal"),
        ("spectra_proposal_id", "a" * 1025),
        ("spectra_proposal_id", "bad\ud800proposal"),
        ("spectra_position_id", ""),
        ("spectra_position_id", False),
        ("spectra_intent", "UNKNOWN"),
        ("spectra_intent", []),
        ("account_name", False),
        ("account_name", ""),
        ("account_name", "bad\naccount"),
    ],
)
def test_malformed_identity_refused_before_capital_and_backend(
    admitted, argument, value
):
    capital, proposal = admitted
    client = FakeClient(capital)
    result = create(client, argument_updates={argument: value})
    assert "error" in result
    assert not client.calls
    assert not client.rule_calls
    assert capital.get_decision(proposal.proposal_id).status == DecisionStatus.RESERVED
    assert capital.get_protected_buy_submission(proposal.proposal_id) is None


def test_identity_normalization_is_stable_and_collision_resistant():
    expected = executor_create._protected_buy_attempt_id(**IDENTITY)
    assert expected.startswith("spectra-buy-v1-")
    assert len(expected) == 79
    changed = {field: f" {value} " for field, value in IDENTITY.items()}
    changed.update(
        intent=" entry ", connector_name=" KRAKEN ", trading_pair=" xrp-usd "
    )
    assert executor_create._protected_buy_attempt_id(**changed) == expected
    split_a = {**IDENTITY, "proposal_id": "a|b", "account_name": "c"}
    split_b = {**IDENTITY, "proposal_id": "a", "account_name": "b|c"}
    assert executor_create._protected_buy_attempt_id(
        **split_a
    ) != executor_create._protected_buy_attempt_id(**split_b)


@pytest.mark.parametrize(
    "field,value",
    [
        ("proposal_id", "entry-generation-2"),
        ("account_name", "other-account"),
        ("controller_id", "other-controller"),
        ("position_id", "position-2"),
        ("intent", "RECOVERY"),
        ("connector_name", "other-connector"),
        ("trading_pair", "ADA-USD"),
    ],
)
def test_distinct_authorized_scope_or_generation_has_distinct_identity(field, value):
    original = executor_create._protected_buy_attempt_id(**IDENTITY)
    altered = executor_create._protected_buy_attempt_id(**{**IDENTITY, field: value})
    assert original != altered


def test_normalized_account_sent_to_backend_matches_committed_scope(admitted):
    capital, _ = admitted
    client = FakeClient(capital)
    result = create(client, argument_updates={"account_name": " master_account "})
    assert "error" not in result
    assert client.calls[0][1] == "master_account"
    assert result["config_used"]["id"] == executor_create._protected_buy_attempt_id(
        **IDENTITY
    )


def test_non_spectra_native_identity_is_preserved(monkeypatch):
    monkeypatch.setattr(executor_create.settings, "agent_slug", "condor")
    monkeypatch.setattr(
        executor_create.executor_preferences,
        "merge_with_defaults",
        lambda executor_type, config: {"id": "saved-condor-id", **config},
    )

    async def no_violation(client, executor_type, config):
        return None

    monkeypatch.setattr(executor_create, "_trading_rule_violation", no_violation)

    class CondorClient:
        def __init__(self):
            self.executors = self
            self.calls = []

        async def create_executor(self, **payload):
            self.calls.append(payload)
            return {"executor_id": payload["executor_config"]["id"]}

    client = CondorClient()
    result = asyncio.run(
        executor_create.create_executor(
            client,
            "order_executor",
            {"connector_name": "kraken", "trading_pair": "XRP-USD", "amount": "1"},
            controller_id="native-controller",
        )
    )
    assert "error" not in result
    assert result["executor_id"] == "saved-condor-id"
    assert result["controller_id"] == "native-controller"
    assert client.calls[0]["executor_config"]["id"] == "saved-condor-id"
