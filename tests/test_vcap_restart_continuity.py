import asyncio
import sqlite3
from decimal import Decimal

from mcp_servers.hummingbot_api.tools.vcap_restart import (
    reconcile_persisted_vcap_state,
)
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.executor_accounting import (
    executor_accounting_observation_from_raw,
)
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.vcap_profit_exit import VCAPProfitState
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPRestartAction,
    VCAPStateStore,
    reconcile_vcap_restart,
)


D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "vcap-restart-governor.sqlite3",
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


def prepare_recovery_execution(
    g,
    *,
    proposal_id="vcap-envelope-1:vcap:0001",
    executor_id="exec-restart-1",
    pair="XRP-USD",
    amount="20",
):
    proposal = CapitalProposal(
        proposal_id=proposal_id,
        pair=pair,
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
    assert g.commit(proposal.proposal_id)
    assert g.reconcile_execution(
        proposal_id=proposal.proposal_id,
        executor_id=executor_id,
    )
    return proposal


def lifecycle_state(
    *,
    proposal_ids=("vcap-envelope-1:vcap:0001",),
    status=VCAPLifecycleStatus.ACTIVE,
):
    return VCAPLifecycleState(
        envelope_proposal_id="vcap-envelope-1",
        pair="XRP-USD",
        next_sequence=2,
        remaining_authorized_quote=D("80"),
        recovery_proposal_ids=tuple(proposal_ids),
        position_base=D("20"),
        cost_basis_quote=D("100"),
        last_known_price=D("5"),
        profit_state=VCAPProfitState(
            updated_at=10_000,
            profit_floor_reached_at=9_000,
            peak_net_profit_bps=D("230"),
            peak_at=9_500,
        ),
        status=status,
        updated_at=10_000,
    )


def observation(
    *,
    executor_id="exec-restart-1",
    status="RUNNING",
    close_type=None,
    filled="20",
):
    raw = {
        "id": executor_id,
        "executor_type": "dca_executor",
        "status": status,
        "filled_amount_quote": filled,
        "cum_fees_quote": "0.05",
    }
    if close_type is not None:
        raw["close_type"] = close_type
    return executor_accounting_observation_from_raw(raw)


def test_vcap_state_store_round_trips_profit_and_sequence(tmp_path):
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    original = lifecycle_state()

    store.save(original)
    loaded = store.load("vcap-envelope-1")

    assert loaded == original
    assert loaded is not None
    assert loaded.next_sequence == 2
    assert loaded.remaining_authorized_quote == D("80")
    assert loaded.profit_state is not None
    assert loaded.profit_state.peak_net_profit_bps == D("230")


def test_corrupt_persisted_proposal_ownership_fails_load_closed(tmp_path):
    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    store.save(lifecycle_state())

    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """
            UPDATE vcap_lifecycle_state
            SET recovery_proposal_ids_json = ?
            WHERE envelope_proposal_id = ?
            """,
            ("not-json", "vcap-envelope-1"),
        )

    try:
        store.load("vcap-envelope-1")
        assert False, "corrupt VCAP state should fail closed"
    except ValueError as exc:
        assert "corrupt VCAP recovery proposal ID state" in str(exc)


def test_exact_active_executor_can_resume_but_requires_fresh_geometry(tmp_path):
    g = governor(tmp_path)
    prepare_recovery_execution(g)

    decision = reconcile_vcap_restart(
        lifecycle_state(),
        governor=g,
        observations_by_proposal={
            "vcap-envelope-1:vcap:0001": observation(),
        },
    )

    assert decision.action == VCAPRestartAction.RESUME
    assert decision.confirmed_recovery_exposure_quote == D("20")
    assert decision.active_proposal_ids == (
        "vcap-envelope-1:vcap:0001",
    )
    assert decision.requires_position_refresh is True
    assert "exact_recovery_ownership_confirmed" in decision.reasons


def test_terminal_flat_executor_retires_stale_state(tmp_path):
    g = governor(tmp_path)
    prepare_recovery_execution(g)

    decision = reconcile_vcap_restart(
        lifecycle_state(),
        governor=g,
        observations_by_proposal={
            "vcap-envelope-1:vcap:0001": observation(
                status="TERMINATED",
                close_type="TAKE_PROFIT",
                filled="40",
            ),
        },
    )

    assert decision.action == VCAPRestartAction.RETIRE
    assert decision.confirmed_recovery_exposure_quote == D("0")
    assert decision.active_proposal_ids == ()
    assert "exact_recovery_exposure_is_flat" in decision.reasons


def test_missing_live_executor_evidence_holds_instead_of_using_wallet_balance(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_recovery_execution(g)

    # Deliberately no wallet/base-asset balance is supplied. A hypothetical
    # retained 43 XRP cannot stand in for proof that this old 20 XRP recovery
    # execution still owns anything.
    decision = reconcile_vcap_restart(
        lifecycle_state(),
        governor=g,
        observations_by_proposal={},
    )

    assert decision.action == VCAPRestartAction.HOLD
    assert decision.requires_position_refresh is True
    assert any(
        "missing_live_observation" in reason
        for reason in decision.reasons
    )


def test_executor_identity_mismatch_holds_fail_closed(tmp_path):
    g = governor(tmp_path)
    prepare_recovery_execution(g)

    decision = reconcile_vcap_restart(
        lifecycle_state(),
        governor=g,
        observations_by_proposal={
            "vcap-envelope-1:vcap:0001": observation(
                executor_id="some-other-executor"
            ),
        },
    )

    assert decision.action == VCAPRestartAction.HOLD
    assert any("executor_mismatch" in reason for reason in decision.reasons)


def test_ambiguous_terminal_executor_holds_instead_of_guessing_flat(tmp_path):
    g = governor(tmp_path)
    prepare_recovery_execution(g)

    decision = reconcile_vcap_restart(
        lifecycle_state(),
        governor=g,
        observations_by_proposal={
            "vcap-envelope-1:vcap:0001": observation(
                status="TERMINATED",
                close_type=None,
            ),
        },
    )

    assert decision.action == VCAPRestartAction.HOLD
    assert any(
        "ambiguous_exposure" in reason
        for reason in decision.reasons
    )


class Executors:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    async def get_executor(self, executor_id):
        self.calls.append(executor_id)
        return self.payloads[executor_id]


class Client:
    def __init__(self, payloads):
        self.executors = Executors(payloads)


def test_restart_helper_retires_closed_state_and_never_queries_wallet(tmp_path):
    g = governor(tmp_path)
    prepare_recovery_execution(g)

    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    store.save(lifecycle_state())

    client = Client(
        {
            "exec-restart-1": {
                "id": "exec-restart-1",
                "executor_type": "dca_executor",
                "status": "TERMINATED",
                "close_type": "TAKE_PROFIT",
                "filled_amount_quote": "40",
                "cum_fees_quote": "0.10",
                "net_pnl_quote": "2.00",
                "custom_info": {
                    "realized_pnl_quote": "2.00",
                },
            }
        }
    )

    result = asyncio.run(
        reconcile_persisted_vcap_state(
            client,
            envelope_proposal_id="vcap-envelope-1",
            governor=g,
            store=store,
            now=11_000,
        )
    )

    assert result.action == VCAPRestartAction.RETIRE
    assert result.state.status == VCAPLifecycleStatus.RETIRED
    assert client.executors.calls == ["exec-restart-1"]

    persisted = store.load("vcap-envelope-1")
    assert persisted is not None
    assert persisted.status == VCAPLifecycleStatus.RETIRED


def test_restart_helper_persists_hold_when_live_lookup_fails(tmp_path):
    g = governor(tmp_path)
    prepare_recovery_execution(g)

    store = VCAPStateStore(tmp_path / "vcap-state.sqlite3")
    store.save(lifecycle_state())

    client = Client({})

    result = asyncio.run(
        reconcile_persisted_vcap_state(
            client,
            envelope_proposal_id="vcap-envelope-1",
            governor=g,
            store=store,
            now=11_500,
        )
    )

    assert result.action == VCAPRestartAction.HOLD
    assert result.state.status == VCAPLifecycleStatus.HOLD
    assert "restart_live_reconciliation_incomplete" in result.reasons


def test_retired_state_never_resumes_even_if_old_executor_payload_reappears(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_recovery_execution(g)

    state = lifecycle_state(status=VCAPLifecycleStatus.RETIRED)
    decision = reconcile_vcap_restart(
        state,
        governor=g,
        observations_by_proposal={
            "vcap-envelope-1:vcap:0001": observation(),
        },
    )

    assert decision.action == VCAPRestartAction.RETIRE
    assert decision.requires_position_refresh is False
    assert decision.active_proposal_ids == ()
