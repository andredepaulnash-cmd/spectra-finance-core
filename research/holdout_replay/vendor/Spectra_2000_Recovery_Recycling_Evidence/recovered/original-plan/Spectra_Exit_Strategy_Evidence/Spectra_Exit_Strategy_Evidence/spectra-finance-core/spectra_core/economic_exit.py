from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Iterable

from .capital_governor import CapitalGovernor
from .economic_position import (
    EconomicLot,
    EconomicPositionLedger,
    EconomicPositionStatus,
)
from .kraken_market_fabric import KrakenTradingRule
from .models import IntentKind


ZERO = Decimal("0")


def _text(value: object, field: str) -> str:
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


def _time(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


class EconomicExitManifestStatus(StrEnum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"


class EconomicExitProgressState(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    DUST_BLOCKED = "DUST_BLOCKED"


@dataclass(frozen=True)
class EconomicExitProgress:
    manifest_id: str
    state: EconomicExitProgressState
    remaining_active_base: Decimal
    realized_pnl_quote: Decimal
    strategic_retained_base: Decimal
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class EconomicExitOwner:
    proposal_id: str
    executor_id: str
    intent: IntentKind
    account_name: str
    connector_name: str
    lot_ids: tuple[str, ...]
    target_base: Decimal
    target_cost_basis_quote: Decimal
    target_entry_fees_quote: Decimal


@dataclass(frozen=True)
class EconomicPositionExitManifest:
    manifest_id: str
    position_id: str
    pair: str
    status: EconomicExitManifestStatus
    target_active_base: Decimal
    target_cost_basis_quote: Decimal
    target_entry_fees_quote: Decimal
    reference_price: Decimal
    min_order_size: Decimal
    min_notional_quote: Decimal
    realized_pnl_before_quote: Decimal
    strategic_retained_base_at_creation: Decimal
    unattributed_base_at_creation: Decimal
    owners: tuple[EconomicExitOwner, ...]
    created_at: float
    completed_at: float | None

    @property
    def proposal_ids(self) -> tuple[str, ...]:
        return tuple(owner.proposal_id for owner in self.owners)

    @property
    def executor_ids(self) -> tuple[str, ...]:
        return tuple(owner.executor_id for owner in self.owners)

    @property
    def lot_ids(self) -> tuple[str, ...]:
        return tuple(
            lot_id
            for owner in self.owners
            for lot_id in owner.lot_ids
        )


class EconomicExitManifestStore:
    """Persist immutable full-position exit scope beside the economic ledger."""

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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS economic_exit_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_active_base TEXT NOT NULL,
                    target_cost_basis_quote TEXT NOT NULL,
                    target_entry_fees_quote TEXT NOT NULL,
                    reference_price TEXT NOT NULL,
                    min_order_size TEXT NOT NULL,
                    min_notional_quote TEXT NOT NULL,
                    realized_pnl_before_quote TEXT NOT NULL,
                    strategic_retained_base_at_creation TEXT NOT NULL,
                    unattributed_base_at_creation TEXT NOT NULL,
                    owners_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    completed_at REAL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    economic_exit_manifest_open_position_idx
                ON economic_exit_manifests(position_id)
                WHERE status = 'OPEN';
                """
            )

    @staticmethod
    def _owners_json(owners: Iterable[EconomicExitOwner]) -> str:
        return json.dumps(
            [
                {
                    "proposal_id": owner.proposal_id,
                    "executor_id": owner.executor_id,
                    "intent": owner.intent.value,
                    "account_name": owner.account_name,
                    "connector_name": owner.connector_name,
                    "lot_ids": list(owner.lot_ids),
                    "target_base": str(owner.target_base),
                    "target_cost_basis_quote": str(
                        owner.target_cost_basis_quote
                    ),
                    "target_entry_fees_quote": str(
                        owner.target_entry_fees_quote
                    ),
                }
                for owner in owners
            ],
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _owners_from_json(raw: str) -> tuple[EconomicExitOwner, ...]:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise RuntimeError("corrupt economic exit owner manifest")

        result: list[EconomicExitOwner] = []
        for row in parsed:
            if not isinstance(row, dict):
                raise RuntimeError("corrupt economic exit owner manifest")
            lots = row.get("lot_ids")
            if not isinstance(lots, list) or not lots:
                raise RuntimeError("corrupt economic exit owner lot scope")
            result.append(
                EconomicExitOwner(
                    proposal_id=_text(row.get("proposal_id"), "proposal_id"),
                    executor_id=_text(row.get("executor_id"), "executor_id"),
                    intent=IntentKind(str(row.get("intent"))),
                    account_name=_text(
                        row.get("account_name"),
                        "account_name",
                    ),
                    connector_name=_text(
                        row.get("connector_name"),
                        "connector_name",
                    ).lower(),
                    lot_ids=tuple(_text(value, "lot_id") for value in lots),
                    target_base=Decimal(str(row.get("target_base"))),
                    target_cost_basis_quote=Decimal(
                        str(row.get("target_cost_basis_quote"))
                    ),
                    target_entry_fees_quote=Decimal(
                        str(row.get("target_entry_fees_quote"))
                    ),
                )
            )
        return tuple(result)

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> EconomicPositionExitManifest:
        return EconomicPositionExitManifest(
            manifest_id=str(row["manifest_id"]),
            position_id=str(row["position_id"]),
            pair=str(row["pair"]),
            status=EconomicExitManifestStatus(str(row["status"])),
            target_active_base=Decimal(str(row["target_active_base"])),
            target_cost_basis_quote=Decimal(
                str(row["target_cost_basis_quote"])
            ),
            target_entry_fees_quote=Decimal(
                str(row["target_entry_fees_quote"])
            ),
            reference_price=Decimal(str(row["reference_price"])),
            min_order_size=Decimal(str(row["min_order_size"])),
            min_notional_quote=Decimal(str(row["min_notional_quote"])),
            realized_pnl_before_quote=Decimal(
                str(row["realized_pnl_before_quote"])
            ),
            strategic_retained_base_at_creation=Decimal(
                str(row["strategic_retained_base_at_creation"])
            ),
            unattributed_base_at_creation=Decimal(
                str(row["unattributed_base_at_creation"])
            ),
            owners=cls._owners_from_json(str(row["owners_json"])),
            created_at=float(row["created_at"]),
            completed_at=(
                None
                if row["completed_at"] is None
                else float(row["completed_at"])
            ),
        )

    def load(self, manifest_id: str) -> EconomicPositionExitManifest | None:
        mid = _text(manifest_id, "manifest_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM economic_exit_manifests
                WHERE manifest_id = ?
                """,
                (mid,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def open_for_position(
        self,
        position_id: str,
    ) -> EconomicPositionExitManifest | None:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM economic_exit_manifests
                WHERE position_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (pid, EconomicExitManifestStatus.OPEN.value),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def latest_for_position(
        self,
        position_id: str,
    ) -> EconomicPositionExitManifest | None:
        pid = _text(position_id, "position_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM economic_exit_manifests
                WHERE position_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (pid,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def create(
        self,
        manifest: EconomicPositionExitManifest,
    ) -> EconomicPositionExitManifest:
        if manifest.status != EconomicExitManifestStatus.OPEN:
            raise ValueError("new economic exit manifest must be OPEN")
        owners_json = self._owners_json(manifest.owners)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT *
                    FROM economic_exit_manifests
                    WHERE manifest_id = ?
                    """,
                    (manifest.manifest_id,),
                ).fetchone()
                if existing is not None:
                    current = self._from_row(existing)
                    if current != manifest:
                        raise RuntimeError(
                            "economic exit manifest replay conflicts"
                        )
                    conn.rollback()
                    return current

                open_row = conn.execute(
                    """
                    SELECT *
                    FROM economic_exit_manifests
                    WHERE position_id = ? AND status = ?
                    """,
                    (
                        manifest.position_id,
                        EconomicExitManifestStatus.OPEN.value,
                    ),
                ).fetchone()
                if open_row is not None:
                    conn.rollback()
                    return self._from_row(open_row)

                conn.execute(
                    """
                    INSERT INTO economic_exit_manifests (
                        manifest_id,
                        position_id,
                        pair,
                        status,
                        target_active_base,
                        target_cost_basis_quote,
                        target_entry_fees_quote,
                        reference_price,
                        min_order_size,
                        min_notional_quote,
                        realized_pnl_before_quote,
                        strategic_retained_base_at_creation,
                        unattributed_base_at_creation,
                        owners_json,
                        created_at,
                        completed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        manifest.manifest_id,
                        manifest.position_id,
                        manifest.pair,
                        manifest.status.value,
                        str(manifest.target_active_base),
                        str(manifest.target_cost_basis_quote),
                        str(manifest.target_entry_fees_quote),
                        str(manifest.reference_price),
                        str(manifest.min_order_size),
                        str(manifest.min_notional_quote),
                        str(manifest.realized_pnl_before_quote),
                        str(manifest.strategic_retained_base_at_creation),
                        str(manifest.unattributed_base_at_creation),
                        owners_json,
                        manifest.created_at,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        created = self.load(manifest.manifest_id)
        if created is None:
            raise RuntimeError("economic exit manifest creation failed")
        return created

    def mark_completed(
        self,
        manifest_id: str,
        *,
        completed_at: float,
    ) -> EconomicPositionExitManifest:
        mid = _text(manifest_id, "manifest_id")
        timestamp = _time(completed_at, "completed_at")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE economic_exit_manifests
                SET status = ?, completed_at = COALESCE(completed_at, ?)
                WHERE manifest_id = ?
                """,
                (
                    EconomicExitManifestStatus.COMPLETED.value,
                    timestamp,
                    mid,
                ),
            )
        result = self.load(mid)
        if result is None:
            raise ValueError(f"unknown economic exit manifest: {mid}")
        return result


def _manifest_id(
    *,
    position_id: str,
    pair: str,
    owners: tuple[EconomicExitOwner, ...],
) -> str:
    payload = EconomicExitManifestStore._owners_json(owners)
    digest = hashlib.sha256(
        f"{position_id}|{pair}|{payload}".encode("utf-8")
    ).hexdigest()[:24]
    return f"{position_id}:exit:{digest}"


def _group_owners(
    *,
    lots: tuple[EconomicLot, ...],
    governor: CapitalGovernor,
    pair: str,
) -> tuple[EconomicExitOwner, ...]:
    grouped: dict[tuple[str, str], list[EconomicLot]] = {}
    executor_owner: dict[str, str] = {}

    for lot in lots:
        if lot.proposal_id is None or lot.executor_id is None:
            raise ValueError(
                f"active lot {lot.lot_id} lacks exact proposal/executor ownership"
            )

        provenance = governor.get_execution_provenance(lot.proposal_id)
        receipt = governor.get_execution_receipt(lot.proposal_id)
        if provenance is None or receipt != lot.executor_id:
            raise ValueError(
                f"active lot {lot.lot_id} lost exact execution provenance"
            )
        if provenance.executor_id != lot.executor_id:
            raise ValueError(
                f"active lot {lot.lot_id} executor ownership changed"
            )
        if provenance.pair.strip().upper() != pair:
            raise ValueError(
                f"active lot {lot.lot_id} crosses exit pair"
            )
        if provenance.intent not in {IntentKind.ENTRY, IntentKind.RECOVERY}:
            raise ValueError(
                f"active lot {lot.lot_id} is not owned trade capital"
            )
        if provenance.account_name is None or provenance.connector_name is None:
            raise ValueError(
                f"active lot {lot.lot_id} has unscoped execution provenance"
            )

        prior = executor_owner.get(lot.executor_id)
        if prior is not None and prior != lot.proposal_id:
            raise ValueError(
                "one executor cannot own multiple live capital proposals"
            )
        executor_owner[lot.executor_id] = lot.proposal_id
        grouped.setdefault(
            (lot.proposal_id, lot.executor_id),
            [],
        ).append(lot)

    owners: list[EconomicExitOwner] = []
    for proposal_id, executor_id in sorted(grouped):
        owner_lots = grouped[(proposal_id, executor_id)]
        provenance = governor.get_execution_provenance(proposal_id)
        if provenance is None:
            raise RuntimeError("exit owner provenance disappeared")

        owners.append(
            EconomicExitOwner(
                proposal_id=proposal_id,
                executor_id=executor_id,
                intent=provenance.intent,
                account_name=str(provenance.account_name),
                connector_name=str(provenance.connector_name).lower(),
                lot_ids=tuple(lot.lot_id for lot in owner_lots),
                target_base=sum(
                    (lot.base_remaining for lot in owner_lots),
                    ZERO,
                ),
                target_cost_basis_quote=sum(
                    (
                        lot.cost_basis_remaining_quote
                        for lot in owner_lots
                    ),
                    ZERO,
                ),
                target_entry_fees_quote=sum(
                    (
                        lot.entry_fees_remaining_quote
                        for lot in owner_lots
                    ),
                    ZERO,
                ),
            )
        )

    if not owners:
        raise ValueError("economic position has no exact active trade owners")
    return tuple(owners)


def _validate_sellability(
    owners: tuple[EconomicExitOwner, ...],
    *,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
) -> None:
    price = _decimal(current_price, "current_price", positive=True)
    min_base = trading_rule.min_order_size
    min_quote = trading_rule.effective_min_quote

    for owner in owners:
        if owner.target_base < min_base:
            raise ValueError(
                f"exit owner {owner.proposal_id} is below Kraken min order size"
            )
        if owner.target_base * price < min_quote:
            raise ValueError(
                f"exit owner {owner.proposal_id} is below Kraken min notional"
            )


def validate_manifest_against_ledger(
    manifest: EconomicPositionExitManifest,
    *,
    ledger: EconomicPositionLedger,
) -> None:
    snapshot = ledger.snapshot(manifest.position_id)
    if snapshot.pair != manifest.pair:
        raise RuntimeError("economic exit manifest pair changed")
    if (
        snapshot.strategic_retained_base_same_pair
        < manifest.strategic_retained_base_at_creation
    ):
        raise RuntimeError(
            "strategic retained inventory decreased during economic exit"
        )

    allowed_lot_ids = set(manifest.lot_ids)
    current_lots = ledger.list_active_lots(manifest.position_id)
    unexpected = [
        lot.lot_id
        for lot in current_lots
        if lot.lot_id not in allowed_lot_ids
    ]
    if unexpected:
        raise RuntimeError(
            "new ACTIVE_TRADE inventory appeared after exit manifest creation"
        )

    owner_targets = {
        (owner.proposal_id, owner.executor_id): owner.target_base
        for owner in manifest.owners
    }
    current_by_owner: dict[tuple[str, str], Decimal] = {}
    for lot in current_lots:
        if lot.proposal_id is None or lot.executor_id is None:
            raise RuntimeError(
                "active inventory lost exact ownership during exit"
            )
        key = (lot.proposal_id, lot.executor_id)
        if key not in owner_targets:
            raise RuntimeError(
                "active inventory owner is outside immutable exit manifest"
            )
        current_by_owner[key] = (
            current_by_owner.get(key, ZERO) + lot.base_remaining
        )

    for key, current in current_by_owner.items():
        if current > owner_targets[key]:
            raise RuntimeError(
                "active inventory increased after exit manifest creation"
            )


def create_or_load_position_exit_manifest(
    *,
    ledger: EconomicPositionLedger,
    governor: CapitalGovernor,
    position_id: str,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
    now: float,
    store: EconomicExitManifestStore | None = None,
) -> EconomicPositionExitManifest:
    pid = _text(position_id, "position_id")
    timestamp = _time(now, "now")
    manifest_store = store or EconomicExitManifestStore(ledger.path)

    existing = manifest_store.open_for_position(pid)
    if existing is not None:
        validate_manifest_against_ledger(existing, ledger=ledger)
        return existing

    snapshot = ledger.snapshot(pid)
    if snapshot.status != EconomicPositionStatus.ACTIVE:
        raise ValueError("cannot manifest exit for closed economic position")
    if snapshot.active_base <= ZERO:
        raise ValueError("economic position has no active trade inventory")
    if trading_rule.trading_pair.strip().upper() != snapshot.pair:
        raise ValueError("exit trading rule pair does not match position")

    lots = ledger.list_active_lots(pid)
    owners = _group_owners(
        lots=lots,
        governor=governor,
        pair=snapshot.pair,
    )
    _validate_sellability(
        owners,
        trading_rule=trading_rule,
        current_price=current_price,
    )

    target_base = sum((owner.target_base for owner in owners), ZERO)
    target_basis = sum(
        (owner.target_cost_basis_quote for owner in owners),
        ZERO,
    )
    target_fees = sum(
        (owner.target_entry_fees_quote for owner in owners),
        ZERO,
    )
    if (
        target_base != snapshot.active_base
        or target_basis != snapshot.active_cost_basis_quote
        or target_fees != snapshot.active_entry_fees_quote
    ):
        raise RuntimeError(
            "exit owner manifest does not exactly cover ACTIVE_TRADE geometry"
        )

    manifest = EconomicPositionExitManifest(
        manifest_id=_manifest_id(
            position_id=pid,
            pair=snapshot.pair,
            owners=owners,
        ),
        position_id=pid,
        pair=snapshot.pair,
        status=EconomicExitManifestStatus.OPEN,
        target_active_base=target_base,
        target_cost_basis_quote=target_basis,
        target_entry_fees_quote=target_fees,
        reference_price=_decimal(
            current_price,
            "current_price",
            positive=True,
        ),
        min_order_size=trading_rule.min_order_size,
        min_notional_quote=trading_rule.effective_min_quote,
        realized_pnl_before_quote=snapshot.realized_pnl_quote,
        strategic_retained_base_at_creation=(
            snapshot.strategic_retained_base_same_pair
        ),
        unattributed_base_at_creation=(
            snapshot.unattributed_base_same_pair
        ),
        owners=owners,
        created_at=timestamp,
        completed_at=None,
    )
    created = manifest_store.create(manifest)
    validate_manifest_against_ledger(created, ledger=ledger)
    return created


def remaining_manifest_base(
    manifest: EconomicPositionExitManifest,
    *,
    ledger: EconomicPositionLedger,
) -> Decimal:
    validate_manifest_against_ledger(manifest, ledger=ledger)
    return ledger.snapshot(manifest.position_id).active_base


def realized_pnl_for_manifest(
    manifest: EconomicPositionExitManifest,
    *,
    ledger: EconomicPositionLedger,
) -> Decimal:
    return (
        ledger.snapshot(manifest.position_id).realized_pnl_quote
        - manifest.realized_pnl_before_quote
    )


def assess_position_exit_manifest(
    manifest: EconomicPositionExitManifest,
    *,
    ledger: EconomicPositionLedger,
    store: EconomicExitManifestStore | None = None,
    now: float | None = None,
) -> EconomicExitProgress:
    validate_manifest_against_ledger(manifest, ledger=ledger)
    snapshot = ledger.snapshot(manifest.position_id)

    realized = (
        snapshot.realized_pnl_quote
        - manifest.realized_pnl_before_quote
    )
    retained = snapshot.strategic_retained_base_same_pair

    if snapshot.active_base == ZERO:
        if snapshot.status != EconomicPositionStatus.CLOSED:
            raise RuntimeError(
                "zero active base did not close economic position"
            )
        if store is not None:
            if now is None:
                raise ValueError("now is required to complete exit manifest")
            store.mark_completed(
                manifest.manifest_id,
                completed_at=_time(now, "now"),
            )
        return EconomicExitProgress(
            manifest_id=manifest.manifest_id,
            state=EconomicExitProgressState.COMPLETED,
            remaining_active_base=ZERO,
            realized_pnl_quote=realized,
            strategic_retained_base=retained,
            reasons=(
                "economic_position_active_trade_flat",
                "strategic_retained_inventory_untouched",
            ),
        )

    remaining = snapshot.active_base
    if (
        remaining < manifest.min_order_size
        or remaining * manifest.reference_price
        < manifest.min_notional_quote
    ):
        return EconomicExitProgress(
            manifest_id=manifest.manifest_id,
            state=EconomicExitProgressState.DUST_BLOCKED,
            remaining_active_base=remaining,
            realized_pnl_quote=realized,
            strategic_retained_base=retained,
            reasons=(
                "remaining_active_trade_below_exchange_minimum",
                "retained_inventory_cannot_satisfy_exit_deficit",
            ),
        )

    return EconomicExitProgress(
        manifest_id=manifest.manifest_id,
        state=EconomicExitProgressState.PENDING,
        remaining_active_base=remaining,
        realized_pnl_quote=realized,
        strategic_retained_base=retained,
        reasons=("economic_position_exit_still_reconciling",),
    )

