from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Mapping

from .capital_governor import CapitalGovernor
from .executor_accounting import assess_recovery_exposure
from .models import ExecutorAccountingObservation, IntentKind
from .vcap_profit_exit import VCAPProfitState


ZERO = Decimal("0")
STATE_SCHEMA_VERSION = 1


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _decimal(
    value: Decimal,
    field: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and value <= ZERO:
        raise ValueError(f"{field} must be positive")
    if non_negative and value < ZERO:
        raise ValueError(f"{field} must be non-negative")
    return value


def _time(value: float, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


class VCAPLifecycleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    HOLD = "HOLD"
    RETIRED = "RETIRED"


class VCAPRestartAction(StrEnum):
    RESUME = "RESUME"
    HOLD = "HOLD"
    RETIRE = "RETIRE"


@dataclass(frozen=True)
class VCAPLifecycleState:
    """Durable VCAP memory.

    This record is memory, never proof of live ownership. On restart, exact
    execution provenance and live executor accounting must reconcile before any
    recovery sequence is allowed to continue.
    """

    envelope_proposal_id: str
    pair: str
    next_sequence: int
    remaining_authorized_quote: Decimal
    recovery_proposal_ids: tuple[str, ...]
    position_base: Decimal
    cost_basis_quote: Decimal
    last_known_price: Decimal
    profit_state: VCAPProfitState | None
    status: VCAPLifecycleStatus
    updated_at: float
    schema_version: int = STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "envelope_proposal_id",
            _text(self.envelope_proposal_id, "envelope_proposal_id"),
        )
        pair = _text(self.pair, "pair").upper()
        if "-" not in pair:
            raise ValueError("pair must be BASE-QUOTE")
        object.__setattr__(self, "pair", pair)

        if (
            isinstance(self.next_sequence, bool)
            or not isinstance(self.next_sequence, int)
            or self.next_sequence < 1
        ):
            raise ValueError("next_sequence must be a positive integer")

        _decimal(
            self.remaining_authorized_quote,
            "remaining_authorized_quote",
            non_negative=True,
        )
        _decimal(self.position_base, "position_base", positive=True)
        _decimal(self.cost_basis_quote, "cost_basis_quote", positive=True)
        _decimal(self.last_known_price, "last_known_price", positive=True)
        _time(self.updated_at, "updated_at")

        if self.schema_version != STATE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported VCAP state schema version {self.schema_version}"
            )

        normalized_ids: list[str] = []
        seen: set[str] = set()
        for raw in self.recovery_proposal_ids:
            proposal_id = _text(raw, "recovery_proposal_id")
            if proposal_id in seen:
                raise ValueError("duplicate recovery_proposal_id")
            seen.add(proposal_id)
            normalized_ids.append(proposal_id)
        object.__setattr__(
            self,
            "recovery_proposal_ids",
            tuple(normalized_ids),
        )


@dataclass(frozen=True)
class VCAPRestartDecision:
    action: VCAPRestartAction
    state: VCAPLifecycleState
    confirmed_recovery_exposure_quote: Decimal
    active_proposal_ids: tuple[str, ...]
    requires_position_refresh: bool
    reasons: tuple[str, ...]


class VCAPStateStore:
    """Small durable ledger for VCAP lifecycle memory."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vcap_lifecycle_state (
                    envelope_proposal_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    pair TEXT NOT NULL,
                    next_sequence INTEGER NOT NULL,
                    remaining_authorized_quote TEXT NOT NULL,
                    recovery_proposal_ids_json TEXT NOT NULL,
                    position_base TEXT NOT NULL,
                    cost_basis_quote TEXT NOT NULL,
                    last_known_price TEXT NOT NULL,
                    profit_floor_reached_at REAL,
                    peak_net_profit_bps TEXT,
                    peak_at REAL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def save(self, state: VCAPLifecycleState) -> None:
        profit = state.profit_state
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vcap_lifecycle_state (
                    envelope_proposal_id,
                    schema_version,
                    pair,
                    next_sequence,
                    remaining_authorized_quote,
                    recovery_proposal_ids_json,
                    position_base,
                    cost_basis_quote,
                    last_known_price,
                    profit_floor_reached_at,
                    peak_net_profit_bps,
                    peak_at,
                    status,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(envelope_proposal_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    pair = excluded.pair,
                    next_sequence = excluded.next_sequence,
                    remaining_authorized_quote = excluded.remaining_authorized_quote,
                    recovery_proposal_ids_json = excluded.recovery_proposal_ids_json,
                    position_base = excluded.position_base,
                    cost_basis_quote = excluded.cost_basis_quote,
                    last_known_price = excluded.last_known_price,
                    profit_floor_reached_at = excluded.profit_floor_reached_at,
                    peak_net_profit_bps = excluded.peak_net_profit_bps,
                    peak_at = excluded.peak_at,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    state.envelope_proposal_id,
                    state.schema_version,
                    state.pair,
                    state.next_sequence,
                    str(state.remaining_authorized_quote),
                    json.dumps(list(state.recovery_proposal_ids)),
                    str(state.position_base),
                    str(state.cost_basis_quote),
                    str(state.last_known_price),
                    None if profit is None else profit.profit_floor_reached_at,
                    None if profit is None else str(profit.peak_net_profit_bps),
                    None if profit is None else profit.peak_at,
                    state.status.value,
                    state.updated_at,
                ),
            )

    def load(self, envelope_proposal_id: str) -> VCAPLifecycleState | None:
        envelope = _text(envelope_proposal_id, "envelope_proposal_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM vcap_lifecycle_state
                WHERE envelope_proposal_id = ?
                """,
                (envelope,),
            ).fetchone()

        if row is None:
            return None

        try:
            proposal_ids_raw = json.loads(row["recovery_proposal_ids_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("corrupt VCAP recovery proposal ID state") from exc
        if not isinstance(proposal_ids_raw, list):
            raise ValueError("corrupt VCAP recovery proposal ID state")

        profit_state: VCAPProfitState | None = None
        if (
            row["profit_floor_reached_at"] is not None
            or row["peak_net_profit_bps"] is not None
            or row["peak_at"] is not None
        ):
            if row["peak_net_profit_bps"] is None:
                raise ValueError("corrupt VCAP profit state")
            profit_state = VCAPProfitState(
                updated_at=float(row["updated_at"]),
                profit_floor_reached_at=row["profit_floor_reached_at"],
                peak_net_profit_bps=Decimal(row["peak_net_profit_bps"]),
                peak_at=row["peak_at"],
            )

        return VCAPLifecycleState(
            envelope_proposal_id=str(row["envelope_proposal_id"]),
            schema_version=int(row["schema_version"]),
            pair=str(row["pair"]),
            next_sequence=int(row["next_sequence"]),
            remaining_authorized_quote=Decimal(
                row["remaining_authorized_quote"]
            ),
            recovery_proposal_ids=tuple(proposal_ids_raw),
            position_base=Decimal(row["position_base"]),
            cost_basis_quote=Decimal(row["cost_basis_quote"]),
            last_known_price=Decimal(row["last_known_price"]),
            profit_state=profit_state,
            status=VCAPLifecycleStatus(str(row["status"])),
            updated_at=float(row["updated_at"]),
        )

    def retire(self, envelope_proposal_id: str, *, updated_at: float) -> bool:
        envelope = _text(envelope_proposal_id, "envelope_proposal_id")
        timestamp = _time(updated_at, "updated_at")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE vcap_lifecycle_state
                SET status = ?, updated_at = ?
                WHERE envelope_proposal_id = ?
                  AND status != ?
                """,
                (
                    VCAPLifecycleStatus.RETIRED.value,
                    timestamp,
                    envelope,
                    VCAPLifecycleStatus.RETIRED.value,
                ),
            )
        return cursor.rowcount == 1


def reconcile_vcap_restart(
    state: VCAPLifecycleState,
    *,
    governor: CapitalGovernor,
    observations_by_proposal: Mapping[str, ExecutorAccountingObservation],
) -> VCAPRestartDecision:
    """Reconcile remembered VCAP state against exact live executor ownership.

    Aggregate wallet balances are intentionally absent from this API. A balance
    of 43 XRP cannot prove that an old 20 XRP VCAP lot still exists. Only the
    exact Spectra proposal -> executor provenance and that executor's live
    accounting may establish recovery ownership.

    Any missing, conflicting, or conservative/ambiguous evidence fails closed
    to HOLD. Exact terminal-flat evidence retires the stale lifecycle state.
    Exact active exposure may RESUME, but callers must rebuild fresh position
    geometry before planning another tranche or exit.
    """

    if state.status == VCAPLifecycleStatus.RETIRED:
        return VCAPRestartDecision(
            action=VCAPRestartAction.RETIRE,
            state=state,
            confirmed_recovery_exposure_quote=ZERO,
            active_proposal_ids=(),
            requires_position_refresh=False,
            reasons=("state_already_retired",),
        )

    if not state.recovery_proposal_ids:
        return VCAPRestartDecision(
            action=VCAPRestartAction.HOLD,
            state=state,
            confirmed_recovery_exposure_quote=ZERO,
            active_proposal_ids=(),
            requires_position_refresh=True,
            reasons=("no_exact_recovery_ownership_provenance",),
        )

    total = ZERO
    active: list[str] = []
    ambiguous: list[str] = []

    expected = set(state.recovery_proposal_ids)
    unexpected = set(observations_by_proposal) - expected
    if unexpected:
        return VCAPRestartDecision(
            action=VCAPRestartAction.HOLD,
            state=state,
            confirmed_recovery_exposure_quote=ZERO,
            active_proposal_ids=(),
            requires_position_refresh=True,
            reasons=("unexpected_restart_observation",),
        )

    for proposal_id in state.recovery_proposal_ids:
        provenance = governor.get_execution_provenance(proposal_id)
        if provenance is None:
            ambiguous.append(f"{proposal_id}:missing_provenance")
            continue
        if provenance.intent != IntentKind.RECOVERY:
            ambiguous.append(f"{proposal_id}:wrong_intent")
            continue
        if provenance.pair.strip().upper() != state.pair:
            ambiguous.append(f"{proposal_id}:pair_mismatch")
            continue

        observation = observations_by_proposal.get(proposal_id)
        if observation is None:
            ambiguous.append(f"{proposal_id}:missing_live_observation")
            continue
        if observation.executor_id != provenance.executor_id:
            ambiguous.append(f"{proposal_id}:executor_mismatch")
            continue

        assessment = assess_recovery_exposure(
            observation,
            provenance,
        )

        if assessment.conservative:
            ambiguous.append(
                f"{proposal_id}:ambiguous_exposure:{assessment.source}"
            )
            continue

        if assessment.exposure_quote > ZERO:
            total += assessment.exposure_quote
            active.append(proposal_id)

    if ambiguous:
        return VCAPRestartDecision(
            action=VCAPRestartAction.HOLD,
            state=state,
            confirmed_recovery_exposure_quote=total,
            active_proposal_ids=tuple(active),
            requires_position_refresh=True,
            reasons=tuple(["restart_evidence_ambiguous", *ambiguous]),
        )

    if total == ZERO:
        return VCAPRestartDecision(
            action=VCAPRestartAction.RETIRE,
            state=state,
            confirmed_recovery_exposure_quote=ZERO,
            active_proposal_ids=(),
            requires_position_refresh=False,
            reasons=("exact_recovery_exposure_is_flat",),
        )

    return VCAPRestartDecision(
        action=VCAPRestartAction.RESUME,
        state=state,
        confirmed_recovery_exposure_quote=total,
        active_proposal_ids=tuple(active),
        requires_position_refresh=True,
        reasons=(
            "exact_recovery_ownership_confirmed",
            "fresh_position_geometry_required",
        ),
    )
