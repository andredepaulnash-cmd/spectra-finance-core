from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from fractions import Fraction
from math import isfinite

from .capital_governor import CapitalGovernor
from .decimal_math import exact_decimal_sum
from .economic_position import EconomicPositionLedger
from .models import IntentKind

ZERO = Decimal("0")


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
    if positive and result <= ZERO:
        raise ValueError(f"{field} must be positive")
    if non_negative and result < ZERO:
        raise ValueError(f"{field} must be non-negative")
    return result


def _pair(value: object) -> str:
    pair = _text(value, "trading_pair").upper()
    if pair.count("-") != 1:
        raise ValueError("trading_pair must be BASE-QUOTE")
    base, quote = pair.split("-", 1)
    if not base or not quote:
        raise ValueError("trading_pair must be BASE-QUOTE")
    return pair


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be an ISO timestamp string")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be an ISO timestamp string") from exc
    if parsed.tzinfo is None:
        # The pinned Hummingbot database historically emitted both aware and
        # naive ISO strings. Treat a naive database timestamp as UTC so replay
        # is deterministic across host timezones.
        parsed = parsed.replace(tzinfo=timezone.utc)
    result = parsed.timestamp()
    if not isfinite(result) or result <= 0:
        raise ValueError("timestamp must resolve to a positive finite time")
    return result


class EconomicFillRole(StrEnum):
    BASE_ENTRY = "BASE_ENTRY"
    VCAP_RECOVERY = "VCAP_RECOVERY"
    POSITION_EXIT = "POSITION_EXIT"


class FillReconciliationStatus(StrEnum):
    APPLIED = "APPLIED"
    REPLAY = "REPLAY"
    UNBOUND = "UNBOUND"


@dataclass(frozen=True)
class TradeFillEvidence:
    trade_id: str
    order_id: str
    account_name: str
    connector_name: str
    pair: str
    side: str
    amount_base: Decimal
    price: Decimal
    fee_paid: Decimal
    fee_currency: str | None
    occurred_at: float

    @property
    def quote_amount(self) -> Decimal:
        with localcontext() as context:
            context.prec = max(
                28,
                len(self.amount_base.as_tuple().digits)
                + len(self.price.as_tuple().digits)
                + 2,
            )
            return self.amount_base * self.price


@dataclass(frozen=True)
class FillOwnershipBinding:
    order_id: str
    position_id: str
    role: EconomicFillRole
    pair: str
    account_name: str
    connector_name: str
    proposal_id: str
    executor_id: str
    created_at: float


@dataclass(frozen=True)
class FillReconciliationResult:
    trade_id: str
    order_id: str
    status: FillReconciliationStatus
    role: EconomicFillRole | None
    result_id: str | None
    position_id: str | None
    reasons: tuple[str, ...]


def trade_fill_evidence_from_raw(raw: object) -> TradeFillEvidence:
    """Parse the exact trade-history shape of the pinned Hummingbot API.

    The pinned TradeRepository emits fee_paid/fee_currency, not an estimated fee
    inferred from an order or executor summary. Only a confirmed trade row may
    reach the economic ledger.
    """

    if not isinstance(raw, dict):
        raise ValueError("trade fill must be a dict")

    trade_id = _text(raw.get("trade_id"), "trade_id")
    order_id = _text(raw.get("order_id"), "order_id")
    account_name = _text(raw.get("account_name"), "account_name")
    connector_name = _text(raw.get("connector_name"), "connector_name").lower()
    pair = _pair(raw.get("trading_pair"))
    side = _text(raw.get("trade_type"), "trade_type").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("trade_type must be BUY or SELL")

    amount = _decimal(raw.get("amount"), "amount", positive=True)
    price = _decimal(raw.get("price"), "price", positive=True)
    if "fee_paid" not in raw or raw.get("fee_paid") is None:
        raise ValueError("trade fill is missing confirmed fee_paid")
    fee = _decimal(
        raw.get("fee_paid"),
        "fee_paid",
        non_negative=True,
    )

    raw_fee_currency = raw.get("fee_currency")
    fee_currency: str | None
    if fee == ZERO and (
        raw_fee_currency is None
        or (isinstance(raw_fee_currency, str) and not raw_fee_currency.strip())
    ):
        fee_currency = None
    else:
        fee_currency = _text(raw_fee_currency, "fee_currency").upper()

    return TradeFillEvidence(
        trade_id=trade_id,
        order_id=order_id,
        account_name=account_name,
        connector_name=connector_name,
        pair=pair,
        side=side,
        amount_base=amount,
        price=price,
        fee_paid=fee,
        fee_currency=fee_currency,
        occurred_at=_timestamp(raw.get("timestamp")),
    )


class LiveFillLedgerReconciler:
    """Bind exact Hummingbot orders to economic ownership and apply trade fills.

    Order bindings are explicit. Pair/time/amount similarity is never used to
    guess ownership. Unbound trade fills are observable but cannot mutate the
    ledger, which isolates manual activity and future retained inventory.

    Trade ids also become the economic lot/event ids. Therefore a crash after
    the economic mutation but before this reconciler records its receipt is
    replay-safe: the ledger mutation itself is already idempotent.
    """

    def __init__(
        self,
        *,
        ledger: EconomicPositionLedger,
        governor: CapitalGovernor,
    ):
        self.ledger = ledger
        self.governor = governor
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.ledger.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS economic_fill_bindings (
                    order_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    account_name TEXT NOT NULL,
                    connector_name TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    executor_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS economic_trade_fill_reconciliation (
                    trade_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    account_name TEXT NOT NULL,
                    connector_name TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    side TEXT NOT NULL,
                    amount_base TEXT NOT NULL,
                    price TEXT NOT NULL,
                    fee_paid TEXT NOT NULL,
                    fee_currency TEXT,
                    occurred_at REAL NOT NULL,
                    role TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    reconciled_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS
                    economic_fill_bindings_executor_idx
                ON economic_fill_bindings(executor_id);

                CREATE INDEX IF NOT EXISTS
                    economic_trade_fill_order_idx
                ON economic_trade_fill_reconciliation(order_id);
                """)

    def bind_executor_order(
        self,
        *,
        order_id: str,
        position_id: str,
        role: EconomicFillRole,
        proposal_id: str,
        account_name: str,
        connector_name: str,
        pair: str,
        created_at: float,
    ) -> FillOwnershipBinding:
        order = _text(order_id, "order_id")
        position = _text(position_id, "position_id")
        proposal = _text(proposal_id, "proposal_id")
        account = _text(account_name, "account_name")
        connector = _text(connector_name, "connector_name").lower()
        normalized_pair = _pair(pair)

        if not isinstance(role, EconomicFillRole):
            raise TypeError("role must be EconomicFillRole")
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            raise ValueError("created_at must be numeric")
        timestamp = float(created_at)
        if not isfinite(timestamp) or timestamp <= 0:
            raise ValueError("created_at must be finite and positive")

        provenance = self.governor.get_execution_provenance(proposal)
        if provenance is None:
            raise ValueError("fill binding requires exact execution provenance")
        receipt = self.governor.get_execution_receipt(proposal)
        if receipt is None or receipt != provenance.executor_id:
            raise ValueError("fill binding requires exact execution receipt")

        if provenance.pair.strip().upper() != normalized_pair:
            raise ValueError("fill binding pair does not match provenance")
        if provenance.account_name is None or provenance.connector_name is None:
            raise ValueError("fill binding requires scoped execution provenance")
        if provenance.account_name != account:
            raise ValueError("fill binding account does not match provenance")
        if provenance.connector_name.lower() != connector:
            raise ValueError("fill binding connector does not match provenance")

        expected_intent = {
            EconomicFillRole.BASE_ENTRY: IntentKind.ENTRY,
            EconomicFillRole.VCAP_RECOVERY: IntentKind.RECOVERY,
        }.get(role)
        if expected_intent is not None and provenance.intent != expected_intent:
            raise ValueError(f"{role.value} binding has wrong capital intent")
        if role == EconomicFillRole.POSITION_EXIT and provenance.intent not in {
            IntentKind.ENTRY,
            IntentKind.RECOVERY,
        }:
            raise ValueError("exit binding source is not owned trade capital")

        snapshot = self.ledger.snapshot(position)
        if snapshot.pair != normalized_pair:
            raise ValueError("fill binding pair does not match economic position")

        binding = FillOwnershipBinding(
            order_id=order,
            position_id=position,
            role=role,
            pair=normalized_pair,
            account_name=account,
            connector_name=connector,
            proposal_id=proposal,
            executor_id=provenance.executor_id,
            created_at=timestamp,
        )

        # Exact replay must remain valid after a SELL has already consumed the
        # source lots. Otherwise a restart after a complete exit could reject
        # its own previously-proven binding because ACTIVE_TRADE is now flat.
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT *
                FROM economic_fill_bindings
                WHERE order_id = ?
                """,
                (order,),
            ).fetchone()

            if existing is not None:
                current = self._binding_from_row(existing)
                if current != binding:
                    raise RuntimeError(
                        "order ownership binding conflicts with existing binding"
                    )
                return current

        if role == EconomicFillRole.POSITION_EXIT:
            owned = self.ledger.active_lot_ids_for_provenance(
                position_id=position,
                proposal_id=proposal,
                executor_id=provenance.executor_id,
            )
            if not owned:
                raise ValueError("exit binding source owns no active economic lots")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO economic_fill_bindings (
                    order_id,
                    position_id,
                    role,
                    pair,
                    account_name,
                    connector_name,
                    proposal_id,
                    executor_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.order_id,
                    binding.position_id,
                    binding.role.value,
                    binding.pair,
                    binding.account_name,
                    binding.connector_name,
                    binding.proposal_id,
                    binding.executor_id,
                    binding.created_at,
                ),
            )

        return binding

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> FillOwnershipBinding:
        return FillOwnershipBinding(
            order_id=str(row["order_id"]),
            position_id=str(row["position_id"]),
            role=EconomicFillRole(str(row["role"])),
            pair=str(row["pair"]),
            account_name=str(row["account_name"]),
            connector_name=str(row["connector_name"]),
            proposal_id=str(row["proposal_id"]),
            executor_id=str(row["executor_id"]),
            created_at=float(row["created_at"]),
        )

    def get_binding(self, order_id: str) -> FillOwnershipBinding | None:
        order = _text(order_id, "order_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM economic_fill_bindings
                WHERE order_id = ?
                """,
                (order,),
            ).fetchone()
        return None if row is None else self._binding_from_row(row)

    @staticmethod
    def _evidence_signature(fill: TradeFillEvidence) -> tuple[object, ...]:
        return (
            fill.order_id,
            fill.account_name,
            fill.connector_name,
            fill.pair,
            fill.side,
            str(fill.amount_base),
            str(fill.price),
            str(fill.fee_paid),
            fill.fee_currency,
            fill.occurred_at,
        )

    def _existing_result(
        self,
        fill: TradeFillEvidence,
    ) -> FillReconciliationResult | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM economic_trade_fill_reconciliation
                WHERE trade_id = ?
                """,
                (fill.trade_id,),
            ).fetchone()
        if row is None:
            return None

        actual = (
            str(row["order_id"]),
            str(row["account_name"]),
            str(row["connector_name"]),
            str(row["pair"]),
            str(row["side"]),
            str(row["amount_base"]),
            str(row["price"]),
            str(row["fee_paid"]),
            None if row["fee_currency"] is None else str(row["fee_currency"]),
            float(row["occurred_at"]),
        )
        if actual != self._evidence_signature(fill):
            raise RuntimeError("trade_id replay conflicts with existing fill evidence")

        return FillReconciliationResult(
            trade_id=fill.trade_id,
            order_id=fill.order_id,
            status=FillReconciliationStatus.REPLAY,
            role=EconomicFillRole(str(row["role"])),
            result_id=str(row["result_id"]),
            position_id=str(row["position_id"]),
            reasons=("trade_fill_already_reconciled",),
        )

    @staticmethod
    def _fee_geometry(
        fill: TradeFillEvidence,
    ) -> tuple[Decimal, Decimal, Decimal]:
        base_asset, quote_asset = fill.pair.split("-", 1)
        fee_currency = fill.fee_currency

        if fill.fee_paid > ZERO and fee_currency not in {
            base_asset,
            quote_asset,
        }:
            raise ValueError("third-currency trade fee cannot be valued exactly")

        if fill.side == "BUY":
            acquired_base = fill.amount_base
            entry_fee_quote = ZERO

            if fill.fee_paid > ZERO and fee_currency == base_asset:
                acquired_base = exact_decimal_sum(
                    (acquired_base, fill.fee_paid.copy_negate())
                )
                if acquired_base <= ZERO:
                    raise ValueError("base-denominated fee consumes entire fill")
            elif fill.fee_paid > ZERO and fee_currency == quote_asset:
                entry_fee_quote = fill.fee_paid

            return acquired_base, fill.quote_amount, entry_fee_quote

        base_sold = fill.amount_base
        exit_fee_quote = ZERO

        if fill.fee_paid > ZERO and fee_currency == base_asset:
            base_sold = exact_decimal_sum((base_sold, fill.fee_paid))
        elif fill.fee_paid > ZERO and fee_currency == quote_asset:
            exit_fee_quote = fill.fee_paid

        return base_sold, fill.quote_amount, exit_fee_quote

    def _validate_fill_binding(
        self,
        fill: TradeFillEvidence,
        binding: FillOwnershipBinding,
    ) -> None:
        if fill.pair != binding.pair:
            raise ValueError("trade fill pair does not match order binding")
        if fill.account_name != binding.account_name:
            raise ValueError("trade fill account does not match order binding")
        if fill.connector_name != binding.connector_name:
            raise ValueError("trade fill connector does not match order binding")

        expected_side = (
            "SELL" if binding.role == EconomicFillRole.POSITION_EXIT else "BUY"
        )
        if fill.side != expected_side:
            raise ValueError(
                f"{binding.role.value} requires {expected_side} trade fills"
            )

        provenance = self.governor.get_execution_provenance(binding.proposal_id)
        receipt = self.governor.get_execution_receipt(binding.proposal_id)
        if provenance is None or receipt != binding.executor_id:
            raise ValueError("bound fill lost exact execution provenance")
        if provenance.executor_id != binding.executor_id:
            raise ValueError("bound fill executor provenance changed")
        if provenance.pair.strip().upper() != binding.pair:
            raise ValueError("bound fill provenance pair changed")

    def reconcile_trade_fill(
        self,
        raw: object,
        *,
        reconciled_at: float | None = None,
    ) -> FillReconciliationResult:
        fill = trade_fill_evidence_from_raw(raw)

        # Cash authority burns authorization before dispatch, and exact BUY
        # evidence audits actual spending even when the inventory fill replays.
        # Source SELLs have no approved sale-purpose manifest: record financial
        # truth below, but keep their unclassified proceeds out of spending.
        authority = self.governor.cash_authority_snapshot()
        binding = self.get_binding(fill.order_id)
        if authority is not None and binding is not None:
            self._validate_fill_binding(fill, binding)
            if fill.occurred_at > authority.activated_at:
                if fill.side == "BUY":
                    try:
                        acquired, spent, _ = self._fee_geometry(fill)
                    except ValueError:
                        self.governor.record_cash_conflict(
                            f"source-fill:{fill.trade_id}",
                            "source fee cannot be valued exactly",
                        )
                        raise
                    self.governor.record_cash_buy_fill(
                        trade_id=fill.trade_id,
                        order_id=fill.order_id,
                        proposal_id=binding.proposal_id,
                        executor_id=binding.executor_id,
                        pair=fill.pair,
                        account_name=fill.account_name,
                        connector_name=fill.connector_name,
                        gross_spent_quote=spent,
                        base_acquired=acquired,
                        price=fill.price,
                        fee_paid=fill.fee_paid,
                        fee_currency=fill.fee_currency,
                        occurred_at=fill.occurred_at,
                    )
                else:
                    self.governor.record_cash_conflict(
                        f"source-fill:{fill.trade_id}",
                        "source SELL has no durable cash sale purpose",
                    )

        replay = self._existing_result(fill)
        if replay is not None:
            if binding is not None:
                self._audit_protected_buy_fill(fill, binding)
            return replay

        binding = self.get_binding(fill.order_id)
        if binding is None:
            return FillReconciliationResult(
                trade_id=fill.trade_id,
                order_id=fill.order_id,
                status=FillReconciliationStatus.UNBOUND,
                role=None,
                result_id=None,
                position_id=None,
                reasons=("trade_fill_has_no_exact_order_binding",),
            )

        self._validate_fill_binding(fill, binding)

        if reconciled_at is None:
            reconciled = fill.occurred_at
        else:
            if isinstance(reconciled_at, bool) or not isinstance(
                reconciled_at, (int, float)
            ):
                raise ValueError("reconciled_at must be numeric")
            reconciled = float(reconciled_at)
            if not isfinite(reconciled) or reconciled <= 0:
                raise ValueError("reconciled_at must be finite and positive")

        amount_base, quote_amount, fee_quote = self._fee_geometry(fill)
        result_id = f"trade-fill:{fill.trade_id}"

        if binding.role == EconomicFillRole.BASE_ENTRY:
            self.ledger.record_entry_fill(
                governor=self.governor,
                position_id=binding.position_id,
                lot_id=result_id,
                proposal_id=binding.proposal_id,
                base_amount=amount_base,
                cost_basis_quote=quote_amount,
                entry_fees_quote=fee_quote,
                occurred_at=fill.occurred_at,
            )
        elif binding.role == EconomicFillRole.VCAP_RECOVERY:
            self.ledger.record_vcap_fill(
                governor=self.governor,
                position_id=binding.position_id,
                lot_id=result_id,
                proposal_id=binding.proposal_id,
                base_amount=amount_base,
                cost_basis_quote=quote_amount,
                entry_fees_quote=fee_quote,
                occurred_at=fill.occurred_at,
            )
        else:
            eligible = self.ledger.active_lot_ids_for_provenance(
                position_id=binding.position_id,
                proposal_id=binding.proposal_id,
                executor_id=binding.executor_id,
            )
            self.ledger.record_partial_exit(
                event_id=result_id,
                position_id=binding.position_id,
                base_sold=amount_base,
                gross_proceeds_quote=quote_amount,
                exit_fee_quote=fee_quote,
                occurred_at=fill.occurred_at,
                eligible_lot_ids=eligible,
            )

        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT trade_id
                FROM economic_trade_fill_reconciliation
                WHERE trade_id = ?
                """,
                (fill.trade_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO economic_trade_fill_reconciliation (
                        trade_id,
                        order_id,
                        account_name,
                        connector_name,
                        pair,
                        side,
                        amount_base,
                        price,
                        fee_paid,
                        fee_currency,
                        occurred_at,
                        role,
                        position_id,
                        result_id,
                        reconciled_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill.trade_id,
                        fill.order_id,
                        fill.account_name,
                        fill.connector_name,
                        fill.pair,
                        fill.side,
                        str(fill.amount_base),
                        str(fill.price),
                        str(fill.fee_paid),
                        fill.fee_currency,
                        fill.occurred_at,
                        binding.role.value,
                        binding.position_id,
                        result_id,
                        reconciled,
                    ),
                )

        # Policy failures must not erase confirmed money. The inventory and
        # exact trade journal commit first; this durable gate also replays after
        # a crash between those commits and the policy audit.
        self._audit_protected_buy_fill(fill, binding)
        return FillReconciliationResult(
            trade_id=fill.trade_id,
            order_id=fill.order_id,
            status=FillReconciliationStatus.APPLIED,
            role=binding.role,
            result_id=result_id,
            position_id=binding.position_id,
            reasons=("confirmed_trade_fill_applied",),
        )

    def _audit_protected_buy_fill(
        self,
        fill: TradeFillEvidence,
        binding: FillOwnershipBinding,
    ) -> None:
        if fill.side != "BUY":
            return
        submission = self.governor.get_protected_buy_submission(binding.proposal_id)
        if submission is None:
            return
        config = submission["config"]
        cap = Fraction(Decimal(str(config["price"])))
        amount = Fraction(Decimal(str(config["amount"])))
        reasons: list[str] = []
        if Fraction(fill.price) > cap:
            reasons.append(f"protected_buy_fill_price_above_cap:{fill.trade_id}")

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.amount_base, r.price, r.fee_paid, r.fee_currency "
                "FROM economic_trade_fill_reconciliation r "
                "JOIN economic_fill_bindings b ON b.order_id=r.order_id "
                "WHERE b.proposal_id=? AND r.side='BUY'",
                (binding.proposal_id,),
            ).fetchall()
        filled = sum(
            (Fraction(Decimal(row["amount_base"])) for row in rows), Fraction(0)
        )
        if filled > amount:
            reasons.append("protected_buy_cumulative_fills_above_authorized_amount")

        policy = submission.get("policy")
        if policy is None:
            reasons.append("protected_buy_fee_policy_missing")
        else:
            fee_rate = Fraction(Decimal(str(policy["fee_allowance_bps"]))) / 10000
            base, quote = fill.pair.split("-", 1)
            # Compare in the confirmed fee's own asset; no rounded conversion
            # can hide a breach and third-asset fees are never estimated.
            if fill.fee_paid:
                if fill.fee_currency == quote:
                    exceeded = (
                        Fraction(fill.fee_paid)
                        > Fraction(fill.amount_base) * Fraction(fill.price) * fee_rate
                    )
                elif fill.fee_currency == base:
                    exceeded = (
                        Fraction(fill.fee_paid) > Fraction(fill.amount_base) * fee_rate
                    )
                else:
                    exceeded = True
                if exceeded:
                    reasons.append(
                        f"protected_buy_fill_fee_above_allowance:{fill.trade_id}"
                    )

        for reason in reasons:
            self.governor.record_protected_buy_conflict(
                proposal_id=binding.proposal_id,
                reason=reason,
            )

    def reconcile_trade_fills(
        self,
        rows: list[object],
        *,
        reconciled_at: float | None = None,
    ) -> tuple[FillReconciliationResult, ...]:
        parsed = [(trade_fill_evidence_from_raw(row), row) for row in rows]
        parsed.sort(key=lambda item: (item[0].occurred_at, item[0].trade_id))

        results: list[FillReconciliationResult] = []
        seen: dict[str, TradeFillEvidence] = {}
        for fill, raw in parsed:
            previous = seen.get(fill.trade_id)
            if previous is not None:
                if previous != fill:
                    raise RuntimeError("batch contains conflicting duplicate trade_id")
                continue
            seen[fill.trade_id] = fill
            results.append(
                self.reconcile_trade_fill(
                    raw,
                    reconciled_at=reconciled_at,
                )
            )
        return tuple(results)
