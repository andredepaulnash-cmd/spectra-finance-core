"""Opt-in, evidence-bound residual classification; deliberately no live route.

The service only changes scheduling classification. It never trades, releases
capital, moves ownership, erases basis or manufactures proceeds. Callers must
supply complete terminal executor order snapshots after reconciliation. Local
state is revalidated under write locks on both the ledger and governor before
classification or successor opening. External snapshots are still evidence,
not an exchange-side lock; integration must keep terminated executors stopped.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, Decimal, localcontext
from math import isfinite
from typing import Iterator

from .capital_governor import CapitalGovernor
from .economic_position import (
    EconomicPositionLedger,
    EconomicPositionStatus,
    _decimal,
    _pair,
    _text,
    normalized_base_asset,
)
from .executor_provenance_bridge import ExecutorOrderSnapshot
from .fill_reconciliation import LiveFillLedgerReconciler, TradeFillEvidence
from .kraken_market_fabric import KrakenTradingRule, normalize_kraken_trading_rules
from .models import CapitalProposal, IntentKind

ZERO = Decimal("0")
TERMINAL_ORDER_STATES = frozenset(
    {"FILLED", "CANCELED", "CANCELLED", "FAILED", "REJECTED"}
)
RESIDUAL_STATES = ("DUST", "DUST_REVIEW")


def _time(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


def _json(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _sum(values) -> Decimal:
    """Exact finite inventory addition independent of ambient precision."""
    values = tuple(values)
    if not values:
        return ZERO
    for value in values:
        _decimal(value, "inventory amount")
    with localcontext() as ctx:
        ctx.prec = max(
            64,
            max(v.adjusted() for v in values)
            - min(v.as_tuple().exponent for v in values)
            + len(str(len(values)))
            + 4,
        )
        return sum(values, ZERO)


def _product(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = max(
            64, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 4
        )
        return left * right


def _floor(value: Decimal, step: Decimal) -> Decimal:
    # Avoid ambient Decimal precision rounding a quantity *up* across a step.
    with localcontext() as ctx:
        ctx.prec = max(
            64,
            len(value.as_tuple().digits)
            + abs(value.adjusted())
            + len(step.as_tuple().digits)
            + abs(step.adjusted())
            + 8,
        )
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class ResidualGeometry:
    owned_base: Decimal
    rounded_base: Decimal
    rounded_price: Decimal
    rounded_notional_quote: Decimal
    marked_value_quote: Decimal
    executable: bool
    reasons: tuple[str, ...]


def residual_geometry(
    *, base: Decimal, price: Decimal, rule: KrakenTradingRule
) -> ResidualGeometry:
    """Evaluate the exact submitted quantity, price and notional, conservatively.

    Venue unavailability is never evidence of dust. A supported spot LIMIT
    route and positive increments are required. This does not test liquidity,
    profitability, an optional profit floor, or aggregate wallet holdings.
    """
    base = _decimal(base, "base", non_negative=True)
    price = _decimal(price, "price", positive=True)
    _pair(rule.trading_pair)
    if not rule.supports_limit_orders:
        raise ValueError("dust classification requires a supported LIMIT route")
    for name in (
        "min_order_size",
        "min_notional_size",
        "min_order_value",
        "min_quote_amount_increment",
    ):
        _decimal(getattr(rule, name), name, non_negative=True)
    for name in ("min_base_amount_increment", "min_price_increment"):
        _decimal(getattr(rule, name), name, positive=True)
    quantity = _floor(base, rule.min_base_amount_increment)
    rounded_price = _floor(price, rule.min_price_increment)
    if rounded_price <= ZERO:
        raise ValueError("price rounds to zero; market evidence is unusable")
    with localcontext() as ctx:
        ctx.prec = max(
            64,
            len(quantity.as_tuple().digits) + len(rounded_price.as_tuple().digits) + 8,
        )
        notional = quantity * rounded_price
        mark = _product(base, price)
    # A base-sized LIMIT sell submits quantity and price, not a quote budget.
    # Rounding this product to a quote-budget increment can falsely classify
    # an executable order as dust. Compare the exact product to the minimum.
    reasons = []
    if quantity <= ZERO:
        reasons.append("quantity_rounds_to_zero")
    if quantity < rule.min_order_size:
        reasons.append("below_minimum_quantity")
    if notional < rule.effective_min_quote:
        reasons.append("below_minimum_notional")
    return ResidualGeometry(
        base, quantity, rounded_price, notional, mark, not reasons, tuple(reasons)
    )


@dataclass(frozen=True)
class RoundedSalePreflight:
    sale: ResidualGeometry
    remainder: ResidualGeometry
    allowed: bool
    reasons: tuple[str, ...]


def preflight_rounded_sale(
    *,
    owned_base: Decimal,
    requested_base: Decimal,
    price: Decimal,
    rule: KrakenTradingRule,
) -> RoundedSalePreflight:
    """Check a proposed sell and its *rounded* remainder without placing it.

    A dust-making partial sell is deferred here. A confirmed partial fill may
    nevertheless leave dust; the checkpoint lifecycle handles that afterwards.
    """
    _decimal(owned_base, "owned_base", positive=True)
    _decimal(requested_base, "requested_base", positive=True)
    if requested_base > owned_base:
        raise ValueError("requested sale exceeds exact owned inventory")
    sale = residual_geometry(base=requested_base, price=price, rule=rule)
    with localcontext() as ctx:
        ctx.prec = max(
            64,
            len(owned_base.as_tuple().digits)
            + abs(owned_base.adjusted())
            + len(sale.rounded_base.as_tuple().digits)
            + abs(sale.rounded_base.adjusted())
            + 8,
        )
        left = owned_base - sale.rounded_base
    remainder = residual_geometry(base=left, price=price, rule=rule)
    reasons = list(sale.reasons)
    if remainder.owned_base > ZERO and not remainder.executable:
        reasons.append("rounded_sale_would_leave_untradeable_residual")
    return RoundedSalePreflight(sale, remainder, not reasons, tuple(reasons))


@dataclass(frozen=True)
class DustCheckpoint:
    checkpoint_id: str
    position_id: str
    market_id: str
    source_digest: str
    observed_at: float
    expires_at: float


@dataclass(frozen=True)
class DustValuation:
    position_id: str
    pair: str
    status: EconomicPositionStatus
    base: Decimal
    cost_basis_quote: Decimal
    entry_fees_quote: Decimal
    marked_value_quote: Decimal
    unrealized_pnl_quote: Decimal


class DustLifecycle:
    """Disabled by default, with durable and restart-safe safety checkpoints.

    The governor database may be separate. ATTACH plus BEGIN IMMEDIATE locks
    both databases before reads; only the ledger is written, avoiding a claim
    of atomic cross-database writes under SQLite WAL. No network I/O occurs in
    a transaction. A fresh checkpoint is required after any relevant mutation.
    """

    def __init__(
        self,
        *,
        ledger: EconomicPositionLedger,
        governor: CapitalGovernor,
        enabled: bool = False,
        max_evidence_age_seconds: float = 30,
    ):
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")
        self.ledger = ledger
        self.governor = governor
        self.enabled = enabled
        self.max_age = _time(max_evidence_age_seconds, "max_evidence_age_seconds")
        self.governor_schema = (
            "main"
            if ledger.path.resolve() == governor.db_path.resolve()
            else "dust_governor"
        )
        self._init_schema()

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ValueError("dust lifecycle is disabled")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.ledger._connect()
        try:
            if self.governor_schema != "main":
                conn.execute(
                    "ATTACH DATABASE ? AS dust_governor", (str(self.governor.db_path),)
                )
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.ledger._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS economic_dust_markets (
                    market_id TEXT PRIMARY KEY, account_name TEXT NOT NULL,
                    connector_name TEXT NOT NULL, pair TEXT NOT NULL,
                    price TEXT NOT NULL, rule_json TEXT NOT NULL,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS economic_dust_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, position_id TEXT NOT NULL,
                    market_id TEXT NOT NULL, source_digest TEXT NOT NULL,
                    orders_json TEXT NOT NULL, observed_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS economic_dust_transitions (
                    checkpoint_id TEXT PRIMARY KEY, position_id TEXT NOT NULL,
                    from_status TEXT NOT NULL, to_status TEXT NOT NULL,
                    occurred_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS economic_dust_entry_cancellations (
                    proposal_id TEXT PRIMARY KEY, reason TEXT NOT NULL, occurred_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS economic_dust_entry_admissions (
                    proposal_id TEXT PRIMARY KEY, successor_id TEXT NOT NULL UNIQUE,
                    account_name TEXT NOT NULL, pair TEXT NOT NULL,
                    amount_quote TEXT NOT NULL, leverage TEXT NOT NULL,
                    checkpoint_ids_json TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, claimed_at REAL,
                    executor_id TEXT
                );
                CREATE TABLE IF NOT EXISTS economic_dust_successors (
                    successor_id TEXT NOT NULL, predecessor_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(successor_id, predecessor_id)
                );
            """)

    def _fresh(self, observed_at: float, now: float) -> None:
        observed_at = _time(observed_at, "observed_at")
        if observed_at > now or now - observed_at > self.max_age:
            raise ValueError("dust evidence is stale or future-dated")

    @staticmethod
    def _rule(row: sqlite3.Row) -> KrakenTradingRule:
        raw = json.loads(row["rule_json"])
        for key in (
            "min_order_size",
            "min_notional_size",
            "min_order_value",
            "min_price_increment",
            "min_base_amount_increment",
            "min_quote_amount_increment",
        ):
            raw[key] = Decimal(raw[key])
        return KrakenTradingRule(**raw)

    def observe_market(
        self,
        *,
        market_id: str,
        account_name: str,
        connector_name: str,
        price: Decimal,
        rule: KrakenTradingRule,
        observed_at: float,
        now: float,
    ) -> None:
        """Persist fresh venue evidence and mark newly executable dust for review.

        Changed thresholds/price never silently restore old lots into a newer
        position. DUST_REVIEW blocks entry and requires a new safe checkpoint.
        """
        self._require_enabled()
        mid = _text(market_id, "market_id")
        account = _text(account_name, "account_name")
        connector = _text(connector_name, "connector_name").lower()
        pair = _pair(rule.trading_pair)
        observed, now = _time(observed_at, "observed_at"), _time(now, "now")
        self._fresh(observed, now)
        residual_geometry(base=ZERO, price=price, rule=rule)
        values = (
            mid,
            account,
            connector,
            pair,
            str(price),
            _json(asdict(rule)),
            observed,
        )
        with self._transaction() as conn:
            old = conn.execute(
                "SELECT * FROM economic_dust_markets WHERE market_id = ?", (mid,)
            ).fetchone()
            if old:
                if tuple(old) != values:
                    raise ValueError("market evidence replay conflicts")
                return
            latest = conn.execute(
                """SELECT observed_at FROM economic_dust_markets
                WHERE account_name = ? AND connector_name = ? AND pair = ?
                ORDER BY observed_at DESC, rowid DESC LIMIT 1""",
                (account, connector, pair),
            ).fetchone()
            if latest and observed <= latest["observed_at"]:
                raise ValueError("market evidence must advance monotonically")
            conn.execute(
                "INSERT INTO economic_dust_markets VALUES (?, ?, ?, ?, ?, ?, ?)", values
            )
            rows = conn.execute(
                """SELECT position_id FROM economic_positions
                WHERE pair = ? AND status IN ('DUST', 'DUST_REVIEW')""",
                (pair,),
            ).fetchall()
            for row in rows:
                pid = row["position_id"]
                scope = self._owner_scope(conn, pid)
                if scope != (account, connector):
                    continue
                snap = self.ledger._snapshot_with_conn(conn, pid)
                if residual_geometry(
                    base=snap.active_base, price=price, rule=rule
                ).executable:
                    conn.execute(
                        """UPDATE economic_positions SET status = 'DUST_REVIEW', updated_at = ?
                        WHERE position_id = ?""",
                        (now, pid),
                    )

    def _rows(
        self, conn: sqlite3.Connection, table: str, field: str, value: str
    ) -> list[dict]:
        # table/field names are internal constants, never caller strings.
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM {table} WHERE {field} = ? ORDER BY rowid", (value,)
            )
        ]

    def _owner_scope(self, conn: sqlite3.Connection, pid: str) -> tuple[str, str]:
        lots = self._rows(conn, "economic_lots", "position_id", pid)
        scopes = set()
        for lot in lots:
            owner = conn.execute(
                f"SELECT * FROM {self.governor_schema}.execution_provenance WHERE proposal_id = ?",
                (lot["proposal_id"],),
            ).fetchone()
            if (
                owner is None
                or owner["executor_id"] != lot["executor_id"]
                or owner["pair"] != lot["pair"]
            ):
                raise ValueError(
                    "dust requires exact scoped owner provenance for every lot"
                )
            if not owner["account_name"] or not owner["connector_name"]:
                raise ValueError("dust owner provenance is unscoped")
            scopes.add((owner["account_name"], owner["connector_name"].lower()))
        if len(scopes) != 1:
            raise ValueError("dust requires one exact account and venue scope")
        return next(iter(scopes))

    def _source(self, conn: sqlite3.Connection, pid: str) -> dict:
        position = self.ledger._position_row(conn, pid)
        result = {"pair": position["pair"], "position_id": pid}
        for table in (
            "economic_lots",
            "economic_position_events",
            "economic_fill_bindings",
            "economic_trade_fill_reconciliation",
        ):
            try:
                result[table] = self._rows(conn, table, "position_id", pid)
            except sqlite3.OperationalError as exc:
                raise ValueError("dust requires durable fill reconciliation") from exc
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        result["position_mutation_locks"] = (
            self._rows(conn, "selected_recovery_position_locks", "position_id", pid)
            if "selected_recovery_position_locks" in tables
            else []
        )
        result["partial_dispatch_claims"] = []
        result["orphan_partial_dispatch_claims"] = []
        if "partial_recycle_dispatch_claims" in tables:
            if "partial_recycle_manifests" not in tables:
                raise ValueError("partial dispatch claims have no manifest evidence")
            result["partial_dispatch_claims"] = [
                dict(r)
                for r in conn.execute(
                    """
                SELECT c.*, m.position_id, m.status AS manifest_status
                FROM partial_recycle_dispatch_claims c
                JOIN partial_recycle_manifests m USING(manifest_id)
                WHERE m.position_id = ? ORDER BY c.manifest_id""",
                    (pid,),
                )
            ]
            result["orphan_partial_dispatch_claims"] = [
                dict(r) for r in conn.execute("""
                SELECT c.* FROM partial_recycle_dispatch_claims c
                LEFT JOIN partial_recycle_manifests m USING(manifest_id)
                WHERE m.manifest_id IS NULL ORDER BY c.manifest_id""")
            ]
        # Include all same-base reservations, including aliases/other quotes.
        reservations = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM {self.governor_schema}.capital_reservations ORDER BY proposal_id"
            )
            if normalized_base_asset(r["pair"]) == position["base_asset"]
            and r["status"] not in {"REJECTED", "EXPIRED", "RELEASED"}
        ]
        result["reservations"] = reservations
        result["same_asset_owned_proposals"] = sorted(
            {
                r["proposal_id"]
                for r in conn.execute(
                    """SELECT l.proposal_id
                FROM economic_lots l JOIN economic_positions p USING(position_id)
                WHERE p.base_asset = ? AND l.proposal_id IS NOT NULL""",
                    (position["base_asset"],),
                )
            }
        )
        owners = {lot["proposal_id"] for lot in result["economic_lots"]}
        result["owner_position_claims"] = [
            dict(r)
            for r in conn.execute(
                "SELECT lot_id, position_id, proposal_id, executor_id FROM economic_lots ORDER BY lot_id"
            )
            if r["proposal_id"] in owners
        ]
        for table in (
            "execution_provenance",
            "execution_receipts",
            "executor_accounting_observations",
            "executor_accounting_refresh_receipts",
            "recovery_recycle_releases",
        ):
            result[table] = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM {self.governor_schema}.{table} ORDER BY rowid"
                )
                if r["proposal_id"] in owners
            ]
        return result

    def _validate_source(
        self, source: dict, snapshots: list[dict], *, now: float
    ) -> None:
        lots, events = source["economic_lots"], source["economic_position_events"]
        fills, bindings = (
            source["economic_trade_fill_reconciliation"],
            source["economic_fill_bindings"],
        )
        if not lots or not events:
            raise ValueError("dust requires reconciled entry and actual exit fills")
        if source["position_mutation_locks"]:
            raise ValueError("position mutation lock prevents dust admission")
        if source["orphan_partial_dispatch_claims"] or any(
            r["manifest_status"] != "COMPLETED"
            for r in source["partial_dispatch_claims"]
        ):
            raise ValueError("pending partial sell dispatch prevents dust admission")
        if any(
            r["position_id"] != source["position_id"]
            for r in source["owner_position_claims"]
        ):
            raise ValueError("exact owner crosses economic position identities")
        if any(
            r["status"] in {"RESERVED", "COMMITTED"} for r in source["reservations"]
        ):
            raise ValueError("same-asset capital reservation remains pending")
        if any(
            r["status"] == "RECONCILED"
            and r["proposal_id"] not in source["same_asset_owned_proposals"]
            for r in source["reservations"]
        ):
            raise ValueError("same-asset execution has no reconciled owned inventory")
        provenance = {r["proposal_id"]: r for r in source["execution_provenance"]}
        receipts = {
            r["proposal_id"]: r["executor_id"] for r in source["execution_receipts"]
        }
        binding_by_id = {r["order_id"]: r for r in bindings}
        orders = {}
        executors = set()
        for snapshot in snapshots:
            executor = snapshot["executor_id"]
            if executor in executors:
                raise ValueError("duplicate executor evidence")
            executors.add(executor)
            for order in snapshot["orders"]:
                oid = order["order_id"]
                if oid in orders or order["executor_id"] != executor:
                    raise ValueError("duplicate or foreign order evidence")
                orders[oid] = order
        if executors != {r["executor_id"] for r in provenance.values()}:
            raise ValueError("order snapshots must cover every exact owner")
        if set(orders) != set(binding_by_id):
            raise ValueError("order evidence does not cover exact durable bindings")
        for lot in lots:
            owner = provenance.get(lot["proposal_id"])
            if (
                owner is None
                or owner["executor_id"] != lot["executor_id"]
                or receipts.get(lot["proposal_id"]) != lot["executor_id"]
                or owner["pair"] != source["pair"]
                or lot["ownership"] != "ACTIVE_TRADE"
            ):
                raise ValueError("lot has missing or foreign exact owner evidence")
        buy_results = {r["result_id"] for r in fills if r["side"] == "BUY"}
        sell_results = {r["result_id"] for r in fills if r["side"] == "SELL"}
        if buy_results != {r["lot_id"] for r in lots} or sell_results != {
            r["event_id"] for r in events
        }:
            raise ValueError("economic mutations are not fully reconciled to fills")
        for fill in fills:
            binding = binding_by_id.get(fill["order_id"])
            if binding is None:
                raise ValueError("fill has no exact order binding")
            owner = provenance.get(binding["proposal_id"])
            if owner is None or any(
                fill[key] != binding[key]
                for key in (
                    "account_name",
                    "connector_name",
                    "pair",
                    "position_id",
                    "role",
                )
            ):
                raise ValueError("fill reconciliation scope disagrees with ownership")
            if any(
                binding[key] != owner[key]
                for key in ("executor_id", "pair", "account_name", "connector_name")
            ):
                raise ValueError("binding no longer matches exact owner")
        self._validate_fill_mutations(source, binding_by_id)
        for oid, order in orders.items():
            binding = binding_by_id[oid]
            if order["executor_id"] != binding["executor_id"]:
                raise ValueError("order executor does not match durable owner")
            expected_side = "SELL" if binding["role"] == "POSITION_EXIT" else "BUY"
            if order["side"] != expected_side:
                raise ValueError("order side does not match durable binding")
            if order["status"] not in TERMINAL_ORDER_STATES:
                raise ValueError(
                    "pending or unknown order prevents dust classification"
                )
            amount, filled = Decimal(order["amount"]), Decimal(order["filled_amount"])
            _decimal(amount, "order amount", positive=True)
            _decimal(filled, "order filled amount", non_negative=True)
            if filled > amount or (order["status"] == "FILLED" and filled != amount):
                raise ValueError("terminal order quantity is inconsistent")
            matching = [r for r in fills if r["order_id"] == oid]
            if any(r["side"] != order["side"] for r in matching):
                raise ValueError("order side conflicts with reconciled fills")
            if sum((Decimal(r["amount_base"]) for r in matching), ZERO) != filled:
                raise ValueError("terminal order has unreconciled partial fills")
            changed = order["updated_at"] or order["created_at"]
            if float(changed) > now:
                raise ValueError("order evidence is future-dated")
        for owner in provenance.values():
            observations = [
                r
                for r in source["executor_accounting_observations"]
                if r["executor_id"] == owner["executor_id"]
            ]
            if not observations or observations[-1]["status"] != "TERMINATED":
                raise ValueError("owner executor is not durably terminated")
            refreshed = [
                r
                for r in source["executor_accounting_refresh_receipts"]
                if r["executor_id"] == owner["executor_id"]
                and r["proposal_id"] == owner["proposal_id"]
                and r["observation_id"] == observations[-1]["observation_id"]
            ]
            self._fresh(
                float(
                    refreshed[-1]["observed_at"]
                    if refreshed
                    else observations[-1]["observed_at"]
                ),
                now,
            )
            owner_fills = [
                r
                for r in fills
                if binding_by_id[r["order_id"]]["executor_id"] == owner["executor_id"]
            ]
            turnover = sum(
                (Decimal(r["amount_base"]) * Decimal(r["price"]) for r in owner_fills),
                ZERO,
            )
            fees = sum(
                (
                    Decimal(r["fee_paid"])
                    * (
                        Decimal(r["price"])
                        if r["fee_currency"] == r["pair"].split("-")[0]
                        else Decimal("1")
                    )
                    for r in owner_fills
                ),
                ZERO,
            )
            if turnover != Decimal(
                observations[-1]["filled_amount_quote"]
            ) or fees != Decimal(observations[-1]["cum_fees_quote"]):
                raise ValueError(
                    "terminal executor totals contain unreconciled activity"
                )
            if owner["intent"] == "RECOVERY":
                released = sum(
                    (
                        Decimal(r["amount_quote"])
                        for r in source["recovery_recycle_releases"]
                        if r["proposal_id"] == owner["proposal_id"]
                    ),
                    ZERO,
                )
                disposed_basis = sum(
                    (
                        Decimal(r["cost_basis_original_quote"])
                        - Decimal(r["cost_basis_remaining_quote"])
                        for r in lots
                        if r["proposal_id"] == owner["proposal_id"]
                    ),
                    ZERO,
                )
                if released > disposed_basis:
                    raise ValueError("recovery release exceeds proven disposed basis")
                if released < Decimal(owner["authorized_quote"]):
                    raise ValueError(
                        "unreleased recovery obligation prevents slot release"
                    )

    @staticmethod
    def _validate_fill_mutations(source: dict, bindings: dict) -> None:
        """Check actual fill geometry and each original owner's conservation.

        A matching result ID alone is not proof that a ledger mutation agrees
        with its receipt. Fees in base remain part of this exact comparison.
        """
        lots = {r["lot_id"]: r for r in source["economic_lots"]}
        events = {r["event_id"]: r for r in source["economic_position_events"]}
        expected = {}
        for raw in source["economic_trade_fill_reconciliation"]:
            fill = TradeFillEvidence(
                raw["trade_id"],
                raw["order_id"],
                raw["account_name"],
                raw["connector_name"],
                raw["pair"],
                raw["side"],
                Decimal(raw["amount_base"]),
                Decimal(raw["price"]),
                Decimal(raw["fee_paid"]),
                raw["fee_currency"],
                float(raw["occurred_at"]),
            )
            _decimal(fill.amount_base, "fill amount", positive=True)
            _decimal(fill.price, "fill price", positive=True)
            _decimal(fill.fee_paid, "fill fee", non_negative=True)
            base, quote, fee = LiveFillLedgerReconciler._fee_geometry(fill)
            owner = bindings[raw["order_id"]]["proposal_id"]
            totals = expected.setdefault(owner, [ZERO, ZERO, ZERO])
            if fill.side == "BUY":
                lot = lots[raw["result_id"]]
                actual = tuple(
                    Decimal(lot[k])
                    for k in (
                        "base_original",
                        "cost_basis_original_quote",
                        "entry_fees_original_quote",
                    )
                )
                if actual != (base, quote, fee) or lot["proposal_id"] != owner:
                    raise ValueError("entry lot differs from exact reconciled fill")
                pieces = (base, quote, fee)
            else:
                event = events[raw["result_id"]]
                actual = tuple(
                    Decimal(event[k])
                    for k in ("base_amount", "quote_amount", "fee_quote")
                )
                if actual != (base, quote, fee):
                    raise ValueError("exit event differs from exact reconciled fill")
                pieces = (
                    -base,
                    -Decimal(event["allocated_cost_basis_quote"]),
                    -Decimal(event["allocated_entry_fees_quote"]),
                )
            for index, piece in enumerate(pieces):
                totals[index] = _sum((totals[index], piece))
        for owner, totals in expected.items():
            current = [ZERO, ZERO, ZERO]
            for lot in lots.values():
                if lot["proposal_id"] != owner:
                    continue
                for index, key in enumerate(
                    (
                        "base_remaining",
                        "cost_basis_remaining_quote",
                        "entry_fees_remaining_quote",
                    )
                ):
                    value = _decimal(Decimal(lot[key]), key, non_negative=True)
                    current[index] = _sum((current[index], value))
            if current != totals:
                raise ValueError("exact owner inventory or basis does not reconcile")

    def _market(
        self, conn: sqlite3.Connection, *, pid: str, market_id: str, now: float
    ) -> sqlite3.Row:
        account, connector = self._owner_scope(conn, pid)
        pair = self.ledger._position_row(conn, pid)["pair"]
        latest = conn.execute(
            """SELECT * FROM economic_dust_markets
            WHERE account_name = ? AND connector_name = ? AND pair = ?
            ORDER BY observed_at DESC, rowid DESC LIMIT 1""",
            (account, connector, pair),
        ).fetchone()
        if latest is None or latest["market_id"] != market_id:
            raise ValueError("checkpoint does not use current exact venue evidence")
        self._fresh(float(latest["observed_at"]), now)
        return latest

    def capture_checkpoint(
        self,
        *,
        checkpoint_id: str,
        position_id: str,
        market_id: str,
        order_snapshots: tuple[ExecutorOrderSnapshot, ...],
        observed_at: float,
        now: float,
    ) -> DustCheckpoint:
        self._require_enabled()
        cid, pid, mid = (
            _text(checkpoint_id, "checkpoint_id"),
            _text(position_id, "position_id"),
            _text(market_id, "market_id"),
        )
        now, observed = _time(now, "now"), _time(observed_at, "observed_at")
        self._fresh(observed, now)
        self.reconcile_unsubmitted_entries(now=now)
        raw = json.loads(_json([asdict(s) for s in order_snapshots]))
        with self._transaction() as conn:
            self._market(conn, pid=pid, market_id=mid, now=now)
            source = self._source(conn, pid)
            self._validate_source(source, raw, now=now)
            if any(
                float(r["reconciled_at"]) > observed
                for r in source["economic_trade_fill_reconciliation"]
            ):
                raise ValueError("order evidence predates latest fill reconciliation")
            if any(
                float(r["observed_at"]) > observed
                for r in (
                    source["executor_accounting_observations"]
                    + source["executor_accounting_refresh_receipts"]
                )
            ):
                raise ValueError(
                    "order evidence predates terminal executor observation"
                )
            if any(
                float(o["updated_at"] or o["created_at"]) > observed
                for s in raw
                for o in s["orders"]
            ):
                raise ValueError("order evidence predates an included order update")
            digest = hashlib.sha256(_json(source).encode()).hexdigest()
            checkpoint = DustCheckpoint(
                cid, pid, mid, digest, observed, observed + self.max_age
            )
            values = (
                cid,
                pid,
                mid,
                digest,
                _json(raw),
                observed,
                checkpoint.expires_at,
            )
            old = conn.execute(
                "SELECT * FROM economic_dust_checkpoints WHERE checkpoint_id = ?",
                (cid,),
            ).fetchone()
            if old:
                if tuple(old) != values:
                    raise ValueError("dust checkpoint replay conflicts")
            else:
                conn.execute(
                    "INSERT INTO economic_dust_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
            return checkpoint

    def _check(
        self,
        conn: sqlite3.Connection,
        checkpoint_id: str,
        now: float,
        admitted_proposal: CapitalProposal | None = None,
    ) -> tuple[str, ResidualGeometry]:
        row = conn.execute(
            "SELECT * FROM economic_dust_checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown dust checkpoint")
        self._fresh(float(row["observed_at"]), now)
        if now > row["expires_at"]:
            raise ValueError("dust checkpoint expired")
        pid = row["position_id"]
        source = self._source(conn, pid)
        if admitted_proposal is not None:
            candidate = [
                r
                for r in source["reservations"]
                if r["proposal_id"] == admitted_proposal.proposal_id
            ]
            if len(candidate) != 1 or not self._matches_reservation(
                candidate[0], admitted_proposal
            ):
                raise ValueError("dust admission lacks its exact pending capital claim")
            source["reservations"] = [
                r
                for r in source["reservations"]
                if r["proposal_id"] != admitted_proposal.proposal_id
            ]
        if hashlib.sha256(_json(source).encode()).hexdigest() != row["source_digest"]:
            raise ValueError("dust checkpoint invalidated by changed durable state")
        self._validate_source(source, json.loads(row["orders_json"]), now=now)
        market = self._market(conn, pid=pid, market_id=row["market_id"], now=now)
        snap = self.ledger._snapshot_with_conn(conn, pid)
        if snap.active_base <= ZERO or snap.status == EconomicPositionStatus.CLOSED:
            raise ValueError("dust checkpoint requires positive owned residual")
        geometry = residual_geometry(
            base=snap.active_base,
            price=Decimal(market["price"]),
            rule=self._rule(market),
        )
        if geometry.executable:
            raise ValueError("old residual remains executable")
        return pid, geometry

    def classify(self, *, checkpoint_id: str, now: float) -> ResidualGeometry:
        self._require_enabled()
        cid, now = _text(checkpoint_id, "checkpoint_id"), _time(now, "now")
        with self._transaction() as conn:
            pid, geometry = self._check(conn, cid, now)
            old_status = self.ledger._position_row(conn, pid)["status"]
            if old_status != "DUST":
                if conn.execute(
                    "SELECT 1 FROM economic_dust_transitions WHERE checkpoint_id = ?",
                    (cid,),
                ).fetchone():
                    raise ValueError(
                        "already-used classification checkpoint requires refresh"
                    )
                conn.execute(
                    "UPDATE economic_positions SET status = 'DUST', updated_at = ? WHERE position_id = ?",
                    (now, pid),
                )
                conn.execute(
                    "INSERT INTO economic_dust_transitions VALUES (?, ?, ?, 'DUST', ?)",
                    (cid, pid, old_status, now),
                )
            return geometry

    def _validate_predecessors(
        self,
        conn,
        *,
        base: str,
        checkpoint_ids: tuple[str, ...],
        now: float,
        admitted_proposal: CapitalProposal | None = None,
    ):
        predecessors = conn.execute(
            """SELECT position_id, status FROM economic_positions
            WHERE base_asset = ? AND status IN ('DUST', 'DUST_REVIEW')""",
            (base,),
        ).fetchall()
        if any(r["status"] != "DUST" for r in predecessors):
            raise ValueError("dust predecessor requires review before re-entry")
        checked = [
            self._check(conn, cid, now, admitted_proposal)[0] for cid in checkpoint_ids
        ]
        if len(set(checked)) != len(checked) or set(checked) != {
            r["position_id"] for r in predecessors
        }:
            raise ValueError("checkpoints must cover every same-base dust predecessor")
        # Repeated dust cycles must not hide an executable accumulation.
        # This is only a gate: no lot is pooled, transferred, or sold.
        scoped_base = {}
        markets = []
        for predecessor, cid in zip(checked, checkpoint_ids):
            cp = conn.execute(
                "SELECT market_id FROM economic_dust_checkpoints WHERE checkpoint_id = ?",
                (cid,),
            ).fetchone()
            market = self._market(
                conn, pid=predecessor, market_id=cp["market_id"], now=now
            )
            scope = (market["account_name"], market["connector_name"])
            scoped_base[scope] = _sum(
                (
                    scoped_base.get(scope, ZERO),
                    self.ledger._snapshot_with_conn(conn, predecessor).active_base,
                )
            )
            markets.append((scope, market))
        for scope, market in markets:
            if residual_geometry(
                base=scoped_base[scope],
                price=Decimal(market["price"]),
                rule=self._rule(market),
            ).executable:
                raise ValueError(
                    "combined old residuals are executable; owner-preserving resolution required"
                )
        return checked

    def open_successor(
        self,
        *,
        position_id: str,
        pair: str,
        checkpoint_ids: tuple[str, ...],
        now: float,
    ) -> None:
        """Atomically open one slot after checking *all* same-base predecessors.

        This authorizes no order or capital reservation. Production admission
        must call the same guard at its own dispatch boundary before enabling
        this currently unregistered feature.
        """
        self._require_enabled()
        pid, pair, now = (
            _text(position_id, "position_id"),
            _pair(pair),
            _time(now, "now"),
        )
        base = normalized_base_asset(pair)
        if not checkpoint_ids or len(set(checkpoint_ids)) != len(checkpoint_ids):
            raise ValueError("provide distinct checkpoints for every dust predecessor")
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM economic_positions WHERE position_id = ?", (pid,)
            ).fetchone()
            if existing:
                links = conn.execute(
                    "SELECT checkpoint_id FROM economic_dust_successors WHERE successor_id = ?",
                    (pid,),
                ).fetchall()
                if existing["pair"] != pair or {
                    r["checkpoint_id"] for r in links
                } != set(checkpoint_ids):
                    raise ValueError("successor replay conflicts")
                # An idempotent replay never opens/reopens or changes anything.
                return
            if conn.execute(
                "SELECT 1 FROM economic_positions WHERE base_asset = ? AND status = 'ACTIVE'",
                (base,),
            ).fetchone():
                raise ValueError("base asset already has an ACTIVE position")
            checked = self._validate_predecessors(
                conn, base=base, checkpoint_ids=checkpoint_ids, now=now
            )
            conn.execute(
                """INSERT INTO economic_positions
                (position_id, pair, base_asset, status, created_at, updated_at)
                VALUES (?, ?, ?, 'ACTIVE', ?, ?)""",
                (pid, pair, base, now, now),
            )
            conn.executemany(
                "INSERT INTO economic_dust_successors VALUES (?, ?, ?, ?)",
                [
                    (pid, predecessor, cid, now)
                    for predecessor, cid in zip(checked, checkpoint_ids)
                ],
            )

    @staticmethod
    def _matches_reservation(row, proposal: CapitalProposal) -> bool:
        return (
            row["status"] in {"RESERVED", "COMMITTED"}
            and row["pair"] == proposal.pair
            and row["intent"] == proposal.intent.value
            and Decimal(row["amount_quote"]) == proposal.amount_quote
            and Decimal(row["leverage"]) == proposal.leverage
        )

    def cancel_unsubmitted_entry(self, *, proposal_id: str, now: float) -> bool:
        """Close an empty slot only after exact proof no external send occurred."""
        now = _time(now, "now")
        with self._transaction() as conn:
            admission = conn.execute(
                "SELECT * FROM economic_dust_entry_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if admission is None or admission["status"] != "ADMITTED":
                return False
            reservation = conn.execute(
                f"SELECT * FROM {self.governor_schema}.capital_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            no_claim_after_lease = (
                reservation is None
                and now - float(admission["created_at"])
                >= self.governor.policy.reservation_ttl_seconds
            )
            if not no_claim_after_lease and (
                reservation is None
                or reservation["status"]
                not in {
                    "REJECTED",
                    "EXPIRED",
                    "RELEASED",
                }
            ):
                return False
            if conn.execute(
                f"SELECT 1 FROM {self.governor_schema}.execution_receipts WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone():
                raise ValueError("cannot cancel an entry with an execution receipt")
            successor = admission["successor_id"]
            for table in (
                "economic_lots",
                "economic_position_events",
                "economic_fill_bindings",
                "selected_recovery_position_locks",
            ):
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE position_id = ?", (successor,)
                ).fetchone():
                    raise ValueError(
                        "cannot cancel an entry with owned or pending activity"
                    )
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "partial_recycle_dispatch_claims" in tables:
                if "partial_recycle_manifests" not in tables:
                    raise ValueError(
                        "cannot cancel without partial dispatch ownership evidence"
                    )
                if conn.execute(
                    """SELECT 1 FROM partial_recycle_dispatch_claims c
                    LEFT JOIN partial_recycle_manifests m USING(manifest_id)
                    WHERE m.manifest_id IS NULL OR (m.position_id = ? AND m.status != 'COMPLETED')""",
                    (successor,),
                ).fetchone():
                    raise ValueError(
                        "cannot cancel an entry with a pending partial dispatch"
                    )
            position = self.ledger._position_row(conn, successor)
            if position["status"] != "ACTIVE":
                raise ValueError("unsubmitted successor state changed")
            conn.execute(
                "UPDATE economic_positions SET status = 'CLOSED', updated_at = ? WHERE position_id = ?",
                (now, successor),
            )
            conn.execute(
                "UPDATE economic_dust_entry_admissions SET status = 'CANCELLED' WHERE proposal_id = ?",
                (proposal_id,),
            )
            conn.execute(
                "INSERT INTO economic_dust_entry_cancellations VALUES (?, ?, ?)",
                (
                    proposal_id,
                    (
                        "NO_CAPITAL_CLAIM_AFTER_LEASE"
                        if no_claim_after_lease
                        else reservation["status"]
                    ),
                    now,
                ),
            )
            return True

    def reconcile_unsubmitted_entries(self, *, now: float) -> None:
        """Recover only expired/declined admissions with proven no-send state."""
        now = _time(now, "now")
        # Do not hold ledger locks while using the Governor's own RESERVED-only
        # CAS. A concurrent COMMITTED transition wins and prevents cancellation.
        with self._transaction() as conn:
            rows = conn.execute(
                f"""SELECT a.proposal_id, r.status, COALESCE(r.created_at, a.created_at) AS created_at
                FROM economic_dust_entry_admissions a
                LEFT JOIN {self.governor_schema}.capital_reservations r USING(proposal_id)
                WHERE a.status = 'ADMITTED'"""
            ).fetchall()
        for row in rows:
            if (
                row["status"] == "RESERVED"
                and now - float(row["created_at"])
                >= self.governor.policy.reservation_ttl_seconds
            ):
                self.governor.release(
                    row["proposal_id"],
                    reason="unsubmitted dust entry reservation expired",
                )
            self.cancel_unsubmitted_entry(proposal_id=row["proposal_id"], now=now)

    def prepare_entry(
        self, *, proposal: CapitalProposal, account_name: str, now: float
    ) -> str | None:
        """Bind existing entry authorization to current dust checkpoints.

        Called by the ordinary capital-authorization path after its portfolio
        await, before reserving money. It never grants capital authority.
        """
        account = _text(account_name, "account_name")
        now = _time(now, "now")
        self.reconcile_unsubmitted_entries(now=now)
        base = normalized_base_asset(proposal.pair)
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM economic_dust_entry_admissions WHERE proposal_id = ?",
                (proposal.proposal_id,),
            ).fetchone()
            if existing is not None:
                if (
                    proposal.intent != IntentKind.ENTRY
                    or existing["account_name"] != account
                    or existing["pair"] != proposal.pair
                    or Decimal(existing["amount_quote"]) != proposal.amount_quote
                    or Decimal(existing["leverage"]) != proposal.leverage
                ):
                    raise ValueError("dust entry admission replay conflicts")
                # This is an immutable admission replay, not a new dispatch.
                # The actual create path always checks current evidence again.
                return existing["successor_id"]
            residuals = conn.execute(
                """SELECT position_id FROM economic_positions
                WHERE base_asset = ? AND status IN ('DUST', 'DUST_REVIEW')
                ORDER BY created_at, position_id""",
                (base,),
            ).fetchall()
            active = conn.execute(
                "SELECT * FROM economic_positions WHERE base_asset = ? AND status = 'ACTIVE'",
                (base,),
            ).fetchall()
            if not residuals:
                if self.enabled and proposal.intent == IntentKind.ENTRY and active:
                    raise ValueError(
                        "base asset already has an ACTIVE economic position"
                    )
                return None
            self._require_enabled()
            if proposal.intent != IntentKind.ENTRY:
                raise ValueError(
                    "dust predecessors require exact successor ENTRY admission"
                )
            checkpoints = []
            for residual in residuals:
                cp = conn.execute(
                    """SELECT checkpoint_id FROM economic_dust_checkpoints
                    WHERE position_id = ? ORDER BY observed_at DESC, rowid DESC LIMIT 1""",
                    (residual["position_id"],),
                ).fetchone()
                if cp is None:
                    raise ValueError("dust predecessor lacks a current checkpoint")
                if self._owner_scope(conn, residual["position_id"]) != (
                    account,
                    "kraken",
                ):
                    raise ValueError(
                        "dust entry account differs from original owner scope"
                    )
                checkpoints.append(cp["checkpoint_id"])
            self._validate_predecessors(
                conn, base=base, checkpoint_ids=tuple(checkpoints), now=now
            )
            if active:
                successor = active[0]["position_id"]
                links = conn.execute(
                    "SELECT checkpoint_id FROM economic_dust_successors WHERE successor_id = ?",
                    (successor,),
                ).fetchall()
                if (
                    len(active) != 1
                    or {r["checkpoint_id"] for r in links} != set(checkpoints)
                    or conn.execute(
                        "SELECT 1 FROM economic_lots WHERE position_id = ?",
                        (successor,),
                    ).fetchone()
                    or conn.execute(
                        "SELECT 1 FROM economic_dust_entry_admissions WHERE successor_id = ?",
                        (successor,),
                    ).fetchone()
                ):
                    raise ValueError(
                        "active old position cannot be admitted as a successor"
                    )
            else:
                successor = f"dust-entry:{proposal.proposal_id}"
        # Use the existing ledger opening surface, including its gated branch.
        if not active:
            self.ledger.open_position(
                position_id=successor,
                pair=proposal.pair,
                created_at=now,
                dust_lifecycle=self,
                dust_checkpoint_ids=tuple(checkpoints),
            )
        with self._transaction() as conn:
            self._validate_predecessors(
                conn, base=base, checkpoint_ids=tuple(checkpoints), now=now
            )
            if self.ledger._snapshot_with_conn(conn, successor).active_base != ZERO:
                raise ValueError("successor acquired inventory before admission")
            conn.execute(
                """INSERT INTO economic_dust_entry_admissions
                (proposal_id, successor_id, account_name, pair, amount_quote, leverage,
                 checkpoint_ids_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ADMITTED', ?)""",
                (
                    proposal.proposal_id,
                    successor,
                    account,
                    proposal.pair,
                    str(proposal.amount_quote),
                    str(proposal.leverage),
                    _json(checkpoints),
                    now,
                ),
            )
        return successor

    def has_guarded_entry_state(self, pair: str) -> bool:
        """Identify when normal executor creation needs uncached venue rules."""
        base = normalized_base_asset(pair)
        with self.ledger._connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM economic_positions WHERE base_asset = ? AND status IN ('DUST', 'DUST_REVIEW')",
                    (base,),
                ).fetchone()
                is not None
            )

    def _validate_dispatch_rules(self, conn, admission, venue_rules) -> None:
        if (
            not isinstance(venue_rules, dict)
            or venue_rules.get("supports_limit_orders") is not True
        ):
            raise ValueError("dust dispatch requires complete fresh venue rules")
        pair = admission["pair"]
        normalized, _ = normalize_kraken_trading_rules(
            {pair: venue_rules}, quote_assets=(pair.rsplit("-", 1)[1],)
        )
        if pair not in normalized:
            raise ValueError("dust dispatch venue rules are incomplete")
        current = normalized[pair]
        for cid in json.loads(admission["checkpoint_ids_json"]):
            market = conn.execute(
                """SELECT m.* FROM economic_dust_markets m
                JOIN economic_dust_checkpoints c USING(market_id) WHERE c.checkpoint_id = ?""",
                (cid,),
            ).fetchone()
            if market is None or market["pair"] != pair:
                raise ValueError(
                    "dust cross-pair entry needs explicit fresh venue evidence"
                )
            previous = self._rule(market)
            if any(
                getattr(previous, key) != getattr(current, key)
                for key in (
                    "min_order_size",
                    "min_notional_size",
                    "min_order_value",
                    "min_price_increment",
                    "min_base_amount_increment",
                    "supports_limit_orders",
                )
            ):
                raise ValueError("fresh venue rules differ from dust checkpoint")

    def assert_dispatch(
        self,
        *,
        proposal: CapitalProposal,
        account_name: str,
        now: float,
        claim: bool = False,
        venue_rules: dict | None = None,
    ) -> str | None:
        """Recheck the actual governed create boundary, including legacy calls.

        Only the exact candidate RESERVED/COMMITTED claim is excluded from its
        predecessor checkpoint. Other pending claims still invalidate it. A
        claimed admission is single-use and remains held on ambiguous failure.
        """
        now = _time(now, "now")
        account = _text(account_name, "account_name")
        base = normalized_base_asset(proposal.pair)
        with self._transaction() as conn:
            residual = conn.execute(
                "SELECT 1 FROM economic_positions WHERE base_asset = ? AND status IN ('DUST', 'DUST_REVIEW')",
                (base,),
            ).fetchone()
            admission = conn.execute(
                "SELECT * FROM economic_dust_entry_admissions WHERE proposal_id = ?",
                (proposal.proposal_id,),
            ).fetchone()
            if not residual and admission is None:
                if (
                    self.enabled
                    and proposal.intent == IntentKind.ENTRY
                    and conn.execute(
                        "SELECT 1 FROM economic_positions WHERE base_asset = ? AND status = 'ACTIVE'",
                        (base,),
                    ).fetchone()
                ):
                    raise ValueError(
                        "executable or active old economic position prevents ENTRY"
                    )
                return None
            self._require_enabled()
            if admission is None:
                raise ValueError("dust successor lacks proposal-bound entry admission")
            if (
                proposal.intent != IntentKind.ENTRY
                or admission["status"] != "ADMITTED"
                or admission["account_name"] != account
                or admission["pair"] != proposal.pair
                or Decimal(admission["amount_quote"]) != proposal.amount_quote
                or Decimal(admission["leverage"]) != proposal.leverage
            ):
                raise ValueError(
                    "dust entry admission scope changed or already claimed"
                )
            self._validate_dispatch_rules(conn, admission, venue_rules)
            successor = admission["successor_id"]
            active = conn.execute(
                "SELECT position_id FROM economic_positions WHERE base_asset = ? AND status = 'ACTIVE'",
                (base,),
            ).fetchall()
            if [r["position_id"] for r in active] != [successor]:
                raise ValueError("dust successor no longer owns the sole active slot")
            if conn.execute(
                "SELECT 1 FROM economic_lots WHERE position_id = ?", (successor,)
            ).fetchone():
                raise ValueError("dust successor already has owned inventory")
            self._validate_predecessors(
                conn,
                base=base,
                checkpoint_ids=tuple(json.loads(admission["checkpoint_ids_json"])),
                now=now,
                admitted_proposal=proposal,
            )
            if claim:
                reservation = conn.execute(
                    f"SELECT status FROM {self.governor_schema}.capital_reservations WHERE proposal_id = ?",
                    (proposal.proposal_id,),
                ).fetchone()
                if reservation is None or reservation["status"] != "COMMITTED":
                    raise ValueError("dust dispatch requires exact committed capital")
                conn.execute(
                    "UPDATE economic_dust_entry_admissions SET status = 'CLAIMED', claimed_at = ? WHERE proposal_id = ?",
                    (now, proposal.proposal_id),
                )
            return successor

    def record_entry_receipt(self, *, proposal_id: str, executor_id: str) -> str | None:
        """Record an observed successful receipt, never release a failed claim."""
        with self._transaction() as conn:
            admission = conn.execute(
                "SELECT * FROM economic_dust_entry_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if admission is None:
                return
            receipt = conn.execute(
                f"SELECT executor_id FROM {self.governor_schema}.execution_receipts WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if receipt is None or receipt["executor_id"] != executor_id:
                raise ValueError("dust entry lacks exact reconciled execution receipt")
            if (
                admission["status"] == "EXECUTED"
                and admission["executor_id"] == executor_id
            ):
                return admission["successor_id"]
            if admission["status"] != "CLAIMED":
                raise ValueError("dust entry admission was not claimed for dispatch")
            conn.execute(
                "UPDATE economic_dust_entry_admissions SET status = 'EXECUTED', executor_id = ? WHERE proposal_id = ?",
                (executor_id, proposal_id),
            )
            return admission["successor_id"]

    def valuations(
        self, *, market_ids: dict[str, str], now: float
    ) -> tuple[DustValuation, ...]:
        """Value residuals by exact pair/owner scope, without converting to cash."""
        now = _time(now, "now")
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT position_id FROM economic_positions WHERE status IN ('DUST', 'DUST_REVIEW') ORDER BY created_at, position_id"
            ).fetchall()
            result = []
            for row in rows:
                pid = row["position_id"]
                market = self._market(conn, pid=pid, market_id=market_ids[pid], now=now)
                snap = self.ledger._snapshot_with_conn(conn, pid)
                mark = _product(snap.active_base, Decimal(market["price"]))
                result.append(
                    DustValuation(
                        pid,
                        snap.pair,
                        snap.status,
                        snap.active_base,
                        snap.active_cost_basis_quote,
                        snap.active_entry_fees_quote,
                        mark,
                        _sum(
                            (
                                mark,
                                -snap.active_cost_basis_quote,
                                -snap.active_entry_fees_quote,
                            )
                        ),
                    )
                )
            return tuple(result)
