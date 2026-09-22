import sqlite3
import time

from decimal import Decimal as D

from spectra_core import (
    CapitalGovernor,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)


def governor(tmp_path):
    policy = CapitalPolicy(
        reserve_floor_quote=D("200"),
        max_total_committed_quote=D("400"),
        max_pair_quote=D("150"),
        max_position_quote=D("120"),
        max_recovery_quote=D("80"),
        max_leverage=D("1"),
    )

    return CapitalGovernor(
        tmp_path / "capital.sqlite3",
        policy,
    )


def base_snapshot(**overrides):
    values = {
        "free_quote": D("500"),
        "committed_quote": D("100"),
        "pair_exposure_quote": D("20"),
        "position_exposure_quote": D("20"),
        "recovery_exposure_quote": D("0"),
    }

    values.update(overrides)
    return CapitalSnapshot(**values)


def proposal(
    proposal_id,
    amount,
    *,
    intent=IntentKind.ENTRY,
    leverage="1",
    pair="BTC-USD",
):
    return CapitalProposal(
        proposal_id=proposal_id,
        pair=pair,
        intent=intent,
        amount_quote=D(amount),
        leverage=D(leverage),
    )


def test_valid_proposal_reserves_capital(tmp_path):
    g = governor(tmp_path)

    result = g.reserve(
        proposal("p1", "50"),
        base_snapshot(),
    )

    assert result.status == DecisionStatus.RESERVED
    assert result.approved
    assert result.reserved_quote == D("50")
    assert g.active_reserved_quote() == D("50")


def test_proposal_id_is_idempotent(tmp_path):
    g = governor(tmp_path)

    first = g.reserve(
        proposal("same", "50"),
        base_snapshot(),
    )

    second = g.reserve(
        proposal("same", "50"),
        base_snapshot(),
    )

    assert first.status == DecisionStatus.RESERVED
    assert second.status == DecisionStatus.RESERVED
    assert g.active_reserved_quote() == D("50")


def test_recovery_budget_is_independent_boundary(tmp_path):
    g = governor(tmp_path)

    snapshot = base_snapshot(
        pair_exposure_quote=D("0"),
        position_exposure_quote=D("0"),
    )

    result = g.reserve(
        proposal(
            "recovery",
            "81",
            intent=IntentKind.RECOVERY,
        ),
        snapshot,
    )

    assert result.status == DecisionStatus.REJECTED
    assert "recovery budget" in result.reason


def test_leverage_limit_is_hard_boundary(tmp_path):
    g = governor(tmp_path)

    result = g.reserve(
        proposal(
            "leveraged",
            "25",
            leverage="2",
        ),
        base_snapshot(),
    )

    assert result.status == DecisionStatus.REJECTED
    assert "leverage" in result.reason


def test_reserve_floor_is_hard_boundary(tmp_path):
    g = governor(tmp_path)

    result = g.reserve(
        proposal("floor", "60"),
        base_snapshot(
            free_quote=D("250"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
        ),
    )

    assert result.status == DecisionStatus.REJECTED
    assert "reserve floor" in result.reason


def test_total_cap_is_hard_boundary(tmp_path):
    g = governor(tmp_path)

    result = g.reserve(
        proposal(
            "total-cap",
            "20",
            pair="ETH-USD",
        ),
        base_snapshot(
            committed_quote=D("390"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
        ),
    )

    assert result.status == DecisionStatus.REJECTED
    assert "total capital cap" in result.reason


def test_position_cap_blocks_competing_reservations(tmp_path):
    g = governor(tmp_path)
    snapshot = base_snapshot()

    first = g.reserve(
        proposal("first", "50"),
        snapshot,
    )
    assert first.approved

    second = g.reserve(
        proposal("second", "60"),
        snapshot,
    )

    assert second.status == DecisionStatus.REJECTED
    assert "position exposure cap" in second.reason


def test_release_returns_reserved_capacity(tmp_path):
    g = governor(tmp_path)

    result = g.reserve(
        proposal("temporary", "50"),
        base_snapshot(),
    )

    assert result.approved
    assert g.active_reserved_quote() == D("50")

    assert g.release("temporary")
    assert g.active_reserved_quote() == D("0")

    decision = g.get_decision("temporary")
    assert decision is not None
    assert decision.status == DecisionStatus.RELEASED


def test_commit_records_committed_state(tmp_path):
    g = governor(tmp_path)

    result = g.reserve(
        proposal(
            "commit-me",
            "40",
            pair="ETH-USD",
        ),
        base_snapshot(
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
        ),
    )

    assert result.approved
    assert g.commit("commit-me")

    decision = g.get_decision("commit-me")
    assert decision is not None
    assert decision.status == DecisionStatus.COMMITTED

    assert g.active_reserved_quote() == D("0")


def test_stale_reserved_capital_expires(tmp_path):
    policy = CapitalPolicy(
        reserve_floor_quote=D("200"),
        max_total_committed_quote=D("400"),
        max_pair_quote=D("150"),
        max_position_quote=D("120"),
        max_recovery_quote=D("80"),
        max_leverage=D("1"),
        reservation_ttl_seconds=1,
    )

    db_path = tmp_path / "expiry.sqlite3"
    g = CapitalGovernor(db_path, policy)

    result = g.reserve(
        proposal("stale", "50"),
        base_snapshot(),
    )

    assert result.status == DecisionStatus.RESERVED
    assert g.protected_quote() == D("50")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE capital_reservations
            SET created_at = ?
            WHERE proposal_id = ?
            """,
            (
                time.time() - 10,
                "stale",
            ),
        )

    assert g.protected_quote() == D("0")

    decision = g.get_decision("stale")
    assert decision is not None
    assert decision.status == DecisionStatus.EXPIRED


def test_committed_capital_stays_protected_until_reconciled(
    tmp_path,
):
    policy = CapitalPolicy(
        reserve_floor_quote=D("200"),
        max_total_committed_quote=D("400"),
        max_pair_quote=D("150"),
        max_position_quote=D("120"),
        max_recovery_quote=D("80"),
        max_leverage=D("1"),
        reservation_ttl_seconds=1,
    )

    db_path = tmp_path / "committed.sqlite3"
    g = CapitalGovernor(db_path, policy)

    result = g.reserve(
        proposal(
            "committed-old",
            "40",
            pair="ETH-USD",
        ),
        base_snapshot(
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
        ),
    )

    assert result.approved
    assert g.commit("committed-old")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE capital_reservations
            SET created_at = ?
            WHERE proposal_id = ?
            """,
            (
                time.time() - 1000,
                "committed-old",
            ),
        )

    assert g.protected_quote() == D("40")

    decision = g.get_decision("committed-old")
    assert decision is not None
    assert decision.status == DecisionStatus.COMMITTED

    assert g.reconcile("committed-old")
    assert g.protected_quote() == D("0")

    decision = g.get_decision("committed-old")
    assert decision is not None
    assert decision.status == DecisionStatus.RECONCILED


def test_matching_claim_commits_atomically(tmp_path):
    g = governor(tmp_path)
    expected = proposal("claim-1", "50")

    assert g.reserve(expected, base_snapshot()).status == DecisionStatus.RESERVED
    assert g.commit_if_matches(expected) is True

    decision = g.get_decision("claim-1")
    assert decision is not None
    assert decision.status == DecisionStatus.COMMITTED
    assert decision.reserved_quote == D("50")
    assert g.protected_quote() == D("50")


def test_mismatched_claim_cannot_commit(tmp_path):
    g = governor(tmp_path)

    reserved = proposal("claim-2", "50", pair="BTC-USD")
    assert g.reserve(reserved, base_snapshot()).status == DecisionStatus.RESERVED

    mismatched = proposal("claim-2", "51", pair="BTC-USD")
    assert g.commit_if_matches(mismatched) is False

    decision = g.get_decision("claim-2")
    assert decision is not None
    assert decision.status == DecisionStatus.RESERVED


def test_claim_matching_covers_pair_intent_and_leverage(tmp_path):
    g = governor(tmp_path)

    reserved = proposal(
        "claim-3",
        "50",
        pair="BTC-USD",
        intent=IntentKind.ENTRY,
        leverage="1",
    )
    assert g.reserve(reserved, base_snapshot()).status == DecisionStatus.RESERVED

    assert g.commit_if_matches(
        proposal("claim-3", "50", pair="ETH-USD")
    ) is False

    assert g.commit_if_matches(
        proposal("claim-3", "50", pair="BTC-USD", intent=IntentKind.PRESS)
    ) is False

    assert g.commit_if_matches(
        proposal("claim-3", "50", pair="BTC-USD", leverage="2")
    ) is False

    assert g.get_decision("claim-3").status == DecisionStatus.RESERVED


def test_committed_claim_cannot_be_replayed(tmp_path):
    g = governor(tmp_path)
    expected = proposal("claim-4", "50")

    assert g.reserve(expected, base_snapshot()).status == DecisionStatus.RESERVED
    assert g.commit_if_matches(expected) is True
    assert g.commit_if_matches(expected) is False

    assert g.get_decision("claim-4").status == DecisionStatus.COMMITTED


def test_expired_claim_cannot_commit(tmp_path):
    policy = CapitalPolicy(
        reserve_floor_quote=D("0"),
        max_total_committed_quote=D("500"),
        max_pair_quote=D("500"),
        max_position_quote=D("500"),
        max_recovery_quote=D("500"),
        max_leverage=D("1"),
        reservation_ttl_seconds=1,
    )

    db = tmp_path / "expired-claim.sqlite3"
    g = CapitalGovernor(db, policy)

    expected = proposal("claim-expired", "50")

    assert g.reserve(
        expected,
        CapitalSnapshot(free_quote=D("500")),
    ).status == DecisionStatus.RESERVED

    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            UPDATE capital_reservations
            SET created_at = ?
            WHERE proposal_id = ?
            """,
            (time.time() - 10, "claim-expired"),
        )
        conn.commit()

    assert g.commit_if_matches(expected) is False
    assert g.get_decision("claim-expired").status == DecisionStatus.EXPIRED


def test_execution_receipt_reconciles_atomically(tmp_path):
    g = governor(tmp_path)

    p = proposal("receipt-1", "50")

    assert g.reserve(
        p,
        CapitalSnapshot(
            free_quote=D("500"),
            committed_quote=D("100"),
            pair_exposure_quote=D("20"),
            position_exposure_quote=D("20"),
            recovery_exposure_quote=D("0"),
        ),
    ).status == DecisionStatus.RESERVED
    assert g.commit("receipt-1")

    assert g.reconcile_execution(
        proposal_id="receipt-1",
        executor_id="exec-1",
    )

    assert g.get_decision("receipt-1").status == DecisionStatus.RECONCILED
    assert g.get_execution_receipt("receipt-1") == "exec-1"


def test_execution_receipt_exact_replay_is_idempotent(tmp_path):
    g = governor(tmp_path)

    p = proposal("receipt-replay", "50")

    assert g.reserve(
        p,
        CapitalSnapshot(
            free_quote=D("500"),
            committed_quote=D("100"),
            pair_exposure_quote=D("20"),
            position_exposure_quote=D("20"),
            recovery_exposure_quote=D("0"),
        ),
    ).status == DecisionStatus.RESERVED
    assert g.commit("receipt-replay")

    assert g.reconcile_execution(
        proposal_id="receipt-replay",
        executor_id="exec-replay",
    )

    assert g.reconcile_execution(
        proposal_id="receipt-replay",
        executor_id="exec-replay",
    )

    assert g.get_execution_receipt("receipt-replay") == "exec-replay"


def test_execution_receipt_conflict_fails_closed(tmp_path):
    import pytest

    g = governor(tmp_path)

    p = proposal("receipt-conflict", "50")

    assert g.reserve(
        p,
        CapitalSnapshot(
            free_quote=D("500"),
            committed_quote=D("100"),
            pair_exposure_quote=D("20"),
            position_exposure_quote=D("20"),
            recovery_exposure_quote=D("0"),
        ),
    ).status == DecisionStatus.RESERVED
    assert g.commit("receipt-conflict")

    assert g.reconcile_execution(
        proposal_id="receipt-conflict",
        executor_id="exec-original",
    )

    with pytest.raises(RuntimeError, match="conflicting execution receipt"):
        g.reconcile_execution(
            proposal_id="receipt-conflict",
            executor_id="exec-different",
        )

    assert g.get_execution_receipt("receipt-conflict") == "exec-original"
    assert (
        g.get_decision("receipt-conflict").status
        == DecisionStatus.RECONCILED
    )



def test_execution_provenance_preserves_recovery_authorization(tmp_path):
    from spectra_core.models import (
        CapitalProposal,
        ExecutionProvenance,
        IntentKind,
    )

    g = governor(tmp_path)

    p = CapitalProposal(
        proposal_id="recovery-provenance",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("25"),
        leverage=D("1"),
    )

    assert g.reserve(
        p,
        CapitalSnapshot(
            free_quote=D("500"),
            committed_quote=D("100"),
            pair_exposure_quote=D("20"),
            position_exposure_quote=D("20"),
            recovery_exposure_quote=D("10"),
        ),
    ).status == DecisionStatus.RESERVED

    assert g.commit("recovery-provenance")

    # Authorization is not execution provenance until an executor exists.
    assert g.get_execution_provenance("recovery-provenance") is None

    assert g.reconcile_execution(
        proposal_id="recovery-provenance",
        executor_id="exec-recovery",
    )

    assert g.get_execution_provenance(
        "recovery-provenance"
    ) == ExecutionProvenance(
        proposal_id="recovery-provenance",
        executor_id="exec-recovery",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        authorized_quote=D("25"),
        leverage=D("1"),
    )

    # Exact replay remains idempotent and preserves the same provenance.
    assert g.reconcile_execution(
        proposal_id="recovery-provenance",
        executor_id="exec-recovery",
    )

    assert (
        g.get_execution_provenance(
            "recovery-provenance"
        ).authorized_quote
        == D("25")
    )



def test_execution_provenance_conflict_fails_closed_on_reopen(tmp_path):
    import pytest

    g = governor(tmp_path)

    p = proposal("provenance-conflict", "50")

    assert g.reserve(
        p,
        CapitalSnapshot(
            free_quote=D("500"),
            committed_quote=D("100"),
            pair_exposure_quote=D("20"),
            position_exposure_quote=D("20"),
            recovery_exposure_quote=D("0"),
        ),
    ).status == DecisionStatus.RESERVED

    assert g.commit("provenance-conflict")

    assert g.reconcile_execution(
        proposal_id="provenance-conflict",
        executor_id="exec-provenance-conflict",
    )

    with g._connect() as conn:
        conn.execute(
            """
            UPDATE execution_provenance
            SET intent = ?
            WHERE proposal_id = ?
            """,
            (
                IntentKind.RECOVERY.value,
                "provenance-conflict",
            ),
        )

    with pytest.raises(
        RuntimeError,
        match="execution provenance conflicts",
    ):
        type(g)(g.db_path, g.policy)
