from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite

from .fill_reconciliation import (
    EconomicFillRole,
    FillOwnershipBinding,
    FillReconciliationStatus,
    LiveFillLedgerReconciler,
    TradeFillEvidence,
    trade_fill_evidence_from_raw,
)
from .models import IntentKind


SPECTRA_HUMMINGBOT_API_REVISION = (
    "84426ffbf9ba09522b4444664f9770236205ae29"
)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric, not bool")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{field} must be positive")
    if non_negative and result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _timestamp(value: object, field: str) -> float:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    result = parsed.timestamp()
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{field} must resolve to a positive finite time")
    return result


@dataclass(frozen=True)
class ExecutorOrderEvidence:
    executor_id: str
    order_id: str
    order_type: str
    side: str
    amount: Decimal
    price: Decimal | None
    status: str
    filled_amount: Decimal
    average_fill_price: Decimal | None
    created_at: float
    updated_at: float | None


@dataclass(frozen=True)
class ExecutorOrderSnapshot:
    executor_id: str
    orders: tuple[ExecutorOrderEvidence, ...]


@dataclass(frozen=True)
class ExecutorTradeSnapshot:
    executor_id: str
    trades: tuple[TradeFillEvidence, ...]


@dataclass(frozen=True)
class ExecutorLedgerSyncReport:
    proposal_id: str
    executor_id: str
    position_id: str
    known_orders: int
    bound_orders: int
    confirmed_fills: int
    ledgered_fills: int
    applied_fills: int
    replayed_fills: int
    unresolved_order_ids: tuple[str, ...]
    unresolved_trade_ids: tuple[str, ...]
    fully_reconciled: bool
    reasons: tuple[str, ...]


def executor_order_snapshot_from_raw(
    raw: object,
    *,
    expected_executor_id: str,
) -> ExecutorOrderSnapshot:
    expected = _text(expected_executor_id, "expected_executor_id")
    if not isinstance(raw, dict):
        raise ValueError("executor order response must be a dict")

    executor_id = _text(raw.get("executor_id"), "executor_id")
    if executor_id != expected:
        raise ValueError("executor order response belongs to another executor")

    rows = raw.get("orders")
    if not isinstance(rows, list):
        raise ValueError("executor order response orders must be a list")

    count = raw.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("executor order response count must be an integer")
    if count != len(rows):
        raise ValueError("executor order response count does not match rows")

    parsed: list[ExecutorOrderEvidence] = []
    seen: set[str] = set()

    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("executor order record must be a dict")

        row_executor = _text(row.get("executor_id"), "order.executor_id")
        if row_executor != expected:
            raise ValueError("executor order record belongs to another executor")

        order_id = _text(row.get("client_order_id"), "client_order_id")
        if order_id in seen:
            raise ValueError("executor order response contains duplicate order id")
        seen.add(order_id)

        side = _text(row.get("trade_type"), "trade_type").upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("executor order trade_type must be BUY or SELL")

        raw_price = row.get("price")
        price = (
            None
            if raw_price is None
            else _decimal(raw_price, "price", positive=True)
        )
        raw_average = row.get("average_fill_price")
        average = (
            None
            if raw_average is None
            else _decimal(
                raw_average,
                "average_fill_price",
                positive=True,
            )
        )
        raw_updated = row.get("updated_at")
        updated = (
            None
            if raw_updated is None
            else _timestamp(raw_updated, "updated_at")
        )

        parsed.append(
            ExecutorOrderEvidence(
                executor_id=row_executor,
                order_id=order_id,
                order_type=_text(row.get("order_type"), "order_type"),
                side=side,
                amount=_decimal(row.get("amount"), "amount", positive=True),
                price=price,
                status=_text(row.get("status"), "status").upper(),
                filled_amount=_decimal(
                    row.get("filled_amount"),
                    "filled_amount",
                    non_negative=True,
                ),
                average_fill_price=average,
                created_at=_timestamp(row.get("created_at"), "created_at"),
                updated_at=updated,
            )
        )

    parsed.sort(key=lambda item: (item.created_at, item.order_id))
    return ExecutorOrderSnapshot(
        executor_id=executor_id,
        orders=tuple(parsed),
    )


def executor_trade_snapshot_from_raw(
    raw: object,
    *,
    expected_executor_id: str,
) -> ExecutorTradeSnapshot:
    expected = _text(expected_executor_id, "expected_executor_id")
    if not isinstance(raw, dict):
        raise ValueError("executor trade response must be a dict")

    executor_id = _text(raw.get("executor_id"), "executor_id")
    if executor_id != expected:
        raise ValueError("executor trade response belongs to another executor")

    rows = raw.get("trades")
    if not isinstance(rows, list):
        raise ValueError("executor trade response trades must be a list")

    count = raw.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("executor trade response count must be an integer")
    if count != len(rows):
        raise ValueError("executor trade response count does not match rows")

    parsed: list[TradeFillEvidence] = []
    seen: dict[str, TradeFillEvidence] = {}

    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("executor trade record must be a dict")
        row_executor = _text(row.get("executor_id"), "trade.executor_id")
        if row_executor != expected:
            raise ValueError("executor trade record belongs to another executor")

        evidence = trade_fill_evidence_from_raw(row)
        previous = seen.get(evidence.trade_id)
        if previous is not None:
            if previous != evidence:
                raise ValueError(
                    "executor trade response contains conflicting trade id"
                )
            raise ValueError("executor trade response contains duplicate trade id")
        seen[evidence.trade_id] = evidence
        parsed.append(evidence)

    parsed.sort(key=lambda item: (item.occurred_at, item.trade_id))
    return ExecutorTradeSnapshot(
        executor_id=executor_id,
        trades=tuple(parsed),
    )


def _expected_buy_role(intent: IntentKind) -> EconomicFillRole:
    if intent == IntentKind.ENTRY:
        return EconomicFillRole.BASE_ENTRY
    if intent == IntentKind.RECOVERY:
        return EconomicFillRole.VCAP_RECOVERY
    raise ValueError(
        "executor provenance intent cannot own economic entry inventory"
    )


def _binding_matches(
    binding: FillOwnershipBinding,
    *,
    order: ExecutorOrderEvidence,
    position_id: str,
    proposal_id: str,
    executor_id: str,
    pair: str,
    account_name: str,
    connector_name: str,
    role: EconomicFillRole,
) -> bool:
    return (
        binding.order_id == order.order_id
        and binding.position_id == position_id
        and binding.proposal_id == proposal_id
        and binding.executor_id == executor_id
        and binding.pair == pair
        and binding.account_name == account_name
        and binding.connector_name == connector_name.lower()
        and binding.role == role
    )


def synchronize_executor_evidence(
    *,
    reconciler: LiveFillLedgerReconciler,
    position_id: str,
    proposal_id: str,
    order_snapshot: ExecutorOrderSnapshot,
    trade_snapshot: ExecutorTradeSnapshot,
    now: float,
) -> ExecutorLedgerSyncReport:
    """Bind exact executor orders and reconcile exact confirmed fills.

    Both API snapshots must already have been fetched and validated before this
    function is called. This keeps network/API failure outside the mutation
    boundary: an unavailable companion endpoint cannot produce a half-sync.

    BUY ownership is established first, then BUY fills are ledgered. SELL
    ownership is established only after the source proposal owns live economic
    lots; confirmed SELL fills can then consume only those exact lots.
    """

    position = _text(position_id, "position_id")
    proposal = _text(proposal_id, "proposal_id")
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValueError("now must be numeric")
    timestamp = float(now)
    if not isfinite(timestamp) or timestamp <= 0:
        raise ValueError("now must be finite and positive")

    provenance = reconciler.governor.get_execution_provenance(proposal)
    if provenance is None:
        raise ValueError("proposal has no execution provenance")
    receipt = reconciler.governor.get_execution_receipt(proposal)
    if receipt is None or receipt != provenance.executor_id:
        raise ValueError("proposal has no exact execution receipt")
    if provenance.account_name is None or provenance.connector_name is None:
        raise ValueError("proposal execution provenance is not scoped")

    executor_id = provenance.executor_id
    if (
        order_snapshot.executor_id != executor_id
        or trade_snapshot.executor_id != executor_id
    ):
        raise ValueError("executor API evidence does not match proposal receipt")

    pair = provenance.pair.strip().upper()
    account = provenance.account_name
    connector = provenance.connector_name.lower()

    snapshot = reconciler.ledger.snapshot(position)
    if snapshot.pair != pair:
        raise ValueError("economic position pair does not match proposal")

    known_by_id = {order.order_id: order for order in order_snapshot.orders}

    for trade in trade_snapshot.trades:
        order = known_by_id.get(trade.order_id)
        if order is None:
            raise ValueError(
                "confirmed executor fill references an unknown executor order"
            )
        if trade.side != order.side:
            raise ValueError("executor order side and confirmed fill disagree")
        if (
            trade.pair != pair
            or trade.account_name != account
            or trade.connector_name != connector
        ):
            raise ValueError(
                "confirmed executor fill does not match proposal execution scope"
            )

    buy_role = _expected_buy_role(provenance.intent)

    # Preflight every existing binding before any new ledger mutation.
    for order in order_snapshot.orders:
        expected_role = (
            buy_role
            if order.side == "BUY"
            else EconomicFillRole.POSITION_EXIT
        )
        existing = reconciler.get_binding(order.order_id)
        if existing is not None and not _binding_matches(
            existing,
            order=order,
            position_id=position,
            proposal_id=proposal,
            executor_id=executor_id,
            pair=pair,
            account_name=account,
            connector_name=connector,
            role=expected_role,
        ):
            raise RuntimeError(
                "persisted order ownership conflicts with executor evidence"
            )

    unresolved_orders: list[str] = []
    unresolved_trades: list[str] = []
    applied = 0
    replayed = 0

    # Phase 1: bind all BUY orders before applying any entry/recovery fills.
    for order in order_snapshot.orders:
        if order.side != "BUY":
            continue
        reconciler.bind_executor_order(
            order_id=order.order_id,
            position_id=position,
            role=buy_role,
            proposal_id=proposal,
            account_name=account,
            connector_name=connector,
            pair=pair,
            created_at=order.created_at,
        )

    buy_trades = [
        trade for trade in trade_snapshot.trades if trade.side == "BUY"
    ]
    for trade in buy_trades:
        result = reconciler.reconcile_trade_fill(
            {
                "trade_id": trade.trade_id,
                "order_id": trade.order_id,
                "account_name": trade.account_name,
                "connector_name": trade.connector_name,
                "trading_pair": trade.pair,
                "trade_type": trade.side,
                "amount": str(trade.amount_base),
                "price": str(trade.price),
                "fee_paid": str(trade.fee_paid),
                "fee_currency": trade.fee_currency,
                "timestamp": datetime.fromtimestamp(
                    trade.occurred_at,
                    tz=timezone.utc,
                ).isoformat(),
            },
            reconciled_at=timestamp,
        )
        if result.status == FillReconciliationStatus.APPLIED:
            applied += 1
        elif result.status == FillReconciliationStatus.REPLAY:
            replayed += 1
        else:
            unresolved_trades.append(trade.trade_id)

    # Phase 2: source lots now exist, so SELL orders can be bound exactly.
    sell_trade_ids_by_order: dict[str, list[str]] = {}
    for trade in trade_snapshot.trades:
        if trade.side == "SELL":
            sell_trade_ids_by_order.setdefault(trade.order_id, []).append(
                trade.trade_id
            )

    for order in order_snapshot.orders:
        if order.side != "SELL":
            continue
        if reconciler.get_binding(order.order_id) is not None:
            continue
        try:
            reconciler.bind_executor_order(
                order_id=order.order_id,
                position_id=position,
                role=EconomicFillRole.POSITION_EXIT,
                proposal_id=proposal,
                account_name=account,
                connector_name=connector,
                pair=pair,
                created_at=order.created_at,
            )
        except ValueError:
            unresolved_orders.append(order.order_id)
            unresolved_trades.extend(
                sell_trade_ids_by_order.get(order.order_id, ())
            )

    for trade in trade_snapshot.trades:
        if trade.side != "SELL" or trade.trade_id in unresolved_trades:
            continue
        if reconciler.get_binding(trade.order_id) is None:
            unresolved_trades.append(trade.trade_id)
            continue
        result = reconciler.reconcile_trade_fill(
            {
                "trade_id": trade.trade_id,
                "order_id": trade.order_id,
                "account_name": trade.account_name,
                "connector_name": trade.connector_name,
                "trading_pair": trade.pair,
                "trade_type": trade.side,
                "amount": str(trade.amount_base),
                "price": str(trade.price),
                "fee_paid": str(trade.fee_paid),
                "fee_currency": trade.fee_currency,
                "timestamp": datetime.fromtimestamp(
                    trade.occurred_at,
                    tz=timezone.utc,
                ).isoformat(),
            },
            reconciled_at=timestamp,
        )
        if result.status == FillReconciliationStatus.APPLIED:
            applied += 1
        elif result.status == FillReconciliationStatus.REPLAY:
            replayed += 1
        else:
            unresolved_trades.append(trade.trade_id)

    bound = sum(
        1
        for order in order_snapshot.orders
        if reconciler.get_binding(order.order_id) is not None
    )
    unresolved_order_set = tuple(dict.fromkeys(unresolved_orders))
    unresolved_trade_set = tuple(dict.fromkeys(unresolved_trades))
    ledgered = applied + replayed
    fully = (
        bound == len(order_snapshot.orders)
        and ledgered == len(trade_snapshot.trades)
        and not unresolved_order_set
        and not unresolved_trade_set
    )

    reasons: list[str] = []
    if fully:
        reasons.append("executor_accounting_fully_reconciled")
    else:
        reasons.append("executor_accounting_requires_reconciliation")

    return ExecutorLedgerSyncReport(
        proposal_id=proposal,
        executor_id=executor_id,
        position_id=position,
        known_orders=len(order_snapshot.orders),
        bound_orders=bound,
        confirmed_fills=len(trade_snapshot.trades),
        ledgered_fills=ledgered,
        applied_fills=applied,
        replayed_fills=replayed,
        unresolved_order_ids=unresolved_order_set,
        unresolved_trade_ids=unresolved_trade_set,
        fully_reconciled=fully,
        reasons=tuple(reasons),
    )
