"""Default-off, governed selected-child runtime integration.

No MCP tool is registered by importing this module. Caller-owned capital claims,
accounting health and market snapshots remain necessary; this adapter creates no
new capital reservations. Ambiguous external mutations are never retried blindly.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction as F
from math import isfinite
from typing import Callable

from .accounting_health import AccountingHealthDecision
from .economic_position import EconomicPositionLedger
from .executor_accounting import executor_accounting_observation_from_raw
from .fill_reconciliation import EconomicFillRole, LiveFillLedgerReconciler
from .models import BreakerMode, CapitalProposal, DecisionStatus, IntentKind
from .selected_recovery import (
    ExitPlan,
    PatchPlan,
    RecoveryRung,
    SelectedRecoveryBook,
    _all_lots,
    _digest,
    _encode_plan,
    _exit_plan,
    _json,
    _patch_plan,
    _persisted_trade,
    _provenance,
)
from .selected_recovery_ledger import exact_decimal, initialize_selected_recovery

TERMINAL = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "FAILED"}


@dataclass(frozen=True)
class SelectedRecoveryRuntimePolicy:
    enabled: bool = False
    max_market_age_seconds: float = 30.0

    def __post_init__(self):
        if (
            type(self.enabled) is not bool
            or not isfinite(self.max_market_age_seconds)
            or self.max_market_age_seconds <= 0
        ):
            raise ValueError(
                "explicit enable gate and positive finite freshness required"
            )


class SelectedRecoveryRuntime:
    def __init__(
        self,
        *,
        book: SelectedRecoveryBook,
        ledger: EconomicPositionLedger,
        governor,
        policy: SelectedRecoveryRuntimePolicy | None = None,
        clock: Callable[[], float] = time.time,
        breaker: Callable | None = None,
    ):
        self.book, self.ledger, self.governor = book, ledger, governor
        self.policy = policy or SelectedRecoveryRuntimePolicy()
        self.clock = clock
        if breaker is None:
            from .runtime import get_breaker_mode

            breaker = get_breaker_mode
        self.breaker = breaker
        self.reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=governor)
        with ledger._connect() as conn:
            initialize_selected_recovery(conn)
        with book._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS selected_recovery_runtime_actions (
                    action_id TEXT PRIMARY KEY, phase TEXT NOT NULL,
                    payload_json TEXT NOT NULL, error TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS selected_recovery_one_action
                    ON selected_recovery_runtime_actions((1)) WHERE phase != 'COMPLETE';
            """)

    @property
    def owner_id(self):
        return (
            "selected:"
            + self.book.snapshot()["manifest_id"]
            + ":"
            + _digest(str(self.book.path.resolve()))[:16]
        )

    def active(self):
        with self.book._connect() as conn:
            row = conn.execute(
                "SELECT action_id, phase, payload_json FROM selected_recovery_runtime_actions WHERE phase != 'COMPLETE'"
            ).fetchone()
        return (
            None
            if row is None
            else {"action_id": row[0], "phase": row[1], **json.loads(row[2])}
        )

    def _phase(self, action_id, phase, error=None):
        with self.book._connect() as conn:
            conn.execute(
                "UPDATE selected_recovery_runtime_actions SET phase=?, error=? WHERE action_id=?",
                (phase, error, action_id),
            )

    def _enabled(self):
        return self.policy.enabled and self.book.enabled

    def _fresh(self, health, market_at):
        now = self.clock()
        if (
            not isfinite(now)
            or not isfinite(market_at)
            or market_at <= 0
            or not 0 <= now - market_at <= self.policy.max_market_age_seconds
        ):
            raise ValueError("fresh current market evidence required")
        scope = self.book.snapshot()["manifest"]["scope"]
        if (
            health.position_id != scope["parent"]
            or not health.fully_reconciled
            or not health.allows_live_mutation(now=now)
        ):
            raise ValueError("fresh fully reconciled accounting required")
        if self.breaker() not in {BreakerMode.NORMAL, BreakerMode.DEFENSIVE}:
            raise ValueError("breaker blocks selected recovery dispatch")
        if scope["connector"] != "kraken":
            raise ValueError("selected execution is Kraken spot only")

    def _native_matches_book(self):
        state = self.book.snapshot()
        expected = {}
        for source in _all_lots(state):
            row = expected.setdefault(
                source["id"], {**source, "base": F(0), "cost": F(0), "fees": F(0)}
            )
            for key in ("base", "cost", "fees"):
                row[key] += F(source[key])
        native = self.ledger.snapshot(state["manifest"]["scope"]["parent"])
        if expected and native.status.value != "ACTIVE":
            raise ValueError("selected native inventory is no longer ACTIVE")
        actual = {
            lot.lot_id: lot
            for lot in self.ledger.list_active_lots(
                state["manifest"]["scope"]["parent"]
            )
        }
        if set(actual) != set(expected):
            raise ValueError("native inventory membership differs from selected book")
        for lid, row in expected.items():
            lot = actual[lid]
            if (
                F(lot.base_remaining),
                F(lot.cost_basis_remaining_quote),
                F(lot.entry_fees_remaining_quote),
                lot.proposal_id,
                lot.executor_id,
            ) != (
                row["base"],
                row["cost"],
                row["fees"],
                row["proposal"],
                row["executor"],
            ):
                raise ValueError(
                    "native source ownership/basis differs from selected book"
                )

    def _no_competing_capital(self, candidate):
        from .economic_position import normalized_base_asset

        asset = normalized_base_asset(self.book.snapshot()["manifest"]["scope"]["pair"])
        with self.governor._connect() as conn:
            rows = conn.execute(
                "SELECT proposal_id, pair, status FROM capital_reservations WHERE status IN ('RESERVED', 'COMMITTED', 'RECONCILED')"
            ).fetchall()
            sealed = {
                row[0]
                for row in conn.execute(
                    "SELECT proposal_id FROM selected_recovery_sealed_basis"
                )
            }

        with self.ledger._connect() as conn:
            known = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT proposal_id FROM economic_lots WHERE proposal_id IS NOT NULL"
                )
            }
        for row in rows:
            if (
                row["proposal_id"] == candidate
                or normalized_base_asset(row["pair"]) != asset
            ):
                continue
            if row["status"] in {"RESERVED", "COMMITTED"} or (
                row["proposal_id"] not in known and row["proposal_id"] not in sealed
            ):
                raise ValueError("same-asset capital/execution outcome is unresolved")

    async def _passive_sources(self, client):
        from mcp_servers.hummingbot_api.tools.executor_provenance import (
            fetch_executor_order_snapshot,
            fetch_executor_trade_snapshot,
        )

        state = self.book.snapshot()
        scope = state["manifest"]["scope"]
        sources = {
            (lot["proposal"], lot["executor"], lot["kind"]) for lot in _all_lots(state)
        }
        for proposal, executor, kind in sorted(sources):
            intent = IntentKind.ENTRY if kind == "ORIGINAL" else IntentKind.RECOVERY
            _provenance(self.governor, proposal, executor, scope, intent)
            raw = await client.executors.get_executor(executor)
            observation = executor_accounting_observation_from_raw(
                raw, expected_executor_id=executor
            )
            if (
                observation.status != "TERMINATED"
                or observation.close_type != "POSITION_HOLD"
            ):
                raise ValueError("source executor is not passive POSITION_HOLD")
            trades = await fetch_executor_trade_snapshot(client, executor_id=executor)
            orders = await fetch_executor_order_snapshot(client, executor_id=executor)
            if not orders.orders or not trades.trades:
                raise ValueError("nonempty exact source execution evidence required")
            known = {order.order_id: order for order in orders.orders}
            if len(known) != len(orders.orders) or len(
                {fill.trade_id for fill in trades.trades}
            ) != len(trades.trades):
                raise ValueError("duplicate passive source order/trade identities")
            if any(
                order.filled_amount > order.amount
                or (order.status == "FILLED" and order.filled_amount != order.amount)
                or (order.status in {"REJECTED", "FAILED"} and order.filled_amount != 0)
                for order in orders.orders
            ):
                raise ValueError("contradictory passive source terminal geometry")
            if any(
                order.status not in TERMINAL or order.side != "BUY"
                for order in orders.orders
            ):
                raise ValueError("source executor has pending or competing orders")
            for fill in trades.trades:
                if fill.order_id not in known or fill.side != "BUY":
                    raise ValueError("source trade is not exact passive BUY evidence")
                binding = self.reconciler.get_binding(fill.order_id)
                if (
                    binding is None
                    or binding.proposal_id != proposal
                    or binding.executor_id != executor
                ):
                    raise ValueError("source order binding does not match source owner")
                _persisted_trade(
                    self.reconciler,
                    fill,
                    (
                        EconomicFillRole.BASE_ENTRY
                        if kind == "ORIGINAL"
                        else EconomicFillRole.VCAP_RECOVERY
                    ),
                    scope["parent"],
                )
            if any(
                fill.fee_paid and fill.fee_currency != scope["pair"].split("-")[1]
                for fill in trades.trades
            ):
                raise ValueError(
                    "source fees must have exact quote-denominated evidence"
                )
            if sum(
                (F(fill.amount_base) * F(fill.price) for fill in trades.trades), F(0)
            ) != F(observation.filled_amount_quote) or sum(
                (F(fill.fee_paid) for fill in trades.trades), F(0)
            ) != F(
                observation.cum_fees_quote
            ):
                raise ValueError(
                    "passive source executor counters differ from exact trades"
                )
            for order in orders.orders:
                if sum(
                    (
                        F(fill.amount_base)
                        for fill in trades.trades
                        if fill.order_id == order.order_id
                    ),
                    F(0),
                ) != F(order.filled_amount):
                    raise ValueError("source order fill totals are incomplete")

    def _claim_action(self, action_id, payload, plan):
        with self.book._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if _digest(self.book._read(conn)) != plan.state_token:
                raise ValueError("selected plan changed during dispatch preparation")
            if conn.execute(
                "SELECT 1 FROM selected_recovery_runtime_actions WHERE phase != 'COMPLETE'"
            ).fetchone():
                raise ValueError("selected mutation already pending")
            conn.execute(
                "INSERT INTO selected_recovery_runtime_actions VALUES (?, 'DISPATCHING', ?, NULL)",
                (action_id, _json(payload)),
            )

    def release_unactivated_lock(self):
        """Release only a proven pre-send/zero-fill initial admission lease."""
        state = self.book.snapshot()
        if (
            state["split"]
            or state["selected"] is not None
            or state["pending"] is not None
        ):
            return
        active = self.active()
        if active is not None:
            if active["phase"] != "BLOCKED":
                return
            self._phase(
                active["action_id"],
                "COMPLETE",
                "pre-send validation failed; no external request sent",
            )
        position = state["manifest"]["scope"]["parent"]
        if self.ledger.selected_recovery_lock_owner(position) == self.owner_id:
            self.ledger.release_selected_recovery_lock(position, self.owner_id)

    async def dispatch(
        self,
        client,
        *,
        plan,
        rule,
        health: AccountingHealthDecision,
        market_observed_at: float,
        proposal_id: str | None = None,
        buy_fee_reserve_quote: Decimal = Decimal(0),
    ):
        try:
            return await self._dispatch(
                client,
                plan=plan,
                rule=rule,
                health=health,
                market_observed_at=market_observed_at,
                proposal_id=proposal_id,
                buy_fee_reserve_quote=buy_fee_reserve_quote,
            )
        finally:
            self.release_unactivated_lock()

    async def _dispatch(
        self,
        client,
        *,
        plan,
        rule,
        health: AccountingHealthDecision,
        market_observed_at: float,
        proposal_id: str | None = None,
        buy_fee_reserve_quote: Decimal = Decimal(0),
    ):
        if self.active() is not None:
            return await self.synchronize(client)
        if not self._enabled():
            return {"executed": False, "reason": "selected_recovery_disabled"}
        self._fresh(health, market_observed_at)
        validator = _patch_plan if isinstance(plan, PatchPlan) else _exit_plan
        if validator(self.book.snapshot(), json.loads(plan.request_json)) != plan:
            raise ValueError("stale or altered selected dispatch plan")
        scope = self.book.snapshot()["manifest"]["scope"]
        if rule.trading_pair != scope["pair"] or not rule.supports_limit_orders:
            raise ValueError("exact supported Kraken limit trading rule required")
        price = exact_decimal(F(json.loads(plan.request_json)["price"]))
        if (
            price <= 0
            or rule.min_price_increment <= 0
            or price % rule.min_price_increment
        ):
            raise ValueError("price violates exact exchange increment")
        kind = "BUY" if isinstance(plan, PatchPlan) else "SELL"
        if kind == "BUY":
            if (
                not proposal_id
                or not buy_fee_reserve_quote.is_finite()
                or buy_fee_reserve_quote < 0
            ):
                raise ValueError("exact proposal and nonnegative fee reserve required")
            budget = exact_decimal(plan.amount_quote)
            gross = budget - buy_fee_reserve_quote
            amount = exact_decimal(F(gross) / F(price))
            capital = CapitalProposal(
                proposal_id, scope["pair"], IntentKind.RECOVERY, budget, Decimal(1)
            )
            decision = self.governor.get_decision(proposal_id)
            if (
                decision is None
                or decision.status != DecisionStatus.RESERVED
                or decision.reserved_quote != budget
            ):
                raise ValueError(
                    "existing exact RESERVED all-in capital claim required"
                )
        else:
            amount = exact_decimal(plan.target_base)
            gross = amount * price
            capital = None
        if (
            amount < rule.min_order_size
            or gross < rule.effective_min_quote
            or gross <= 0
            or rule.min_base_amount_increment <= 0
            or amount % rule.min_base_amount_increment
            or rule.min_quote_amount_increment <= 0
            or gross % rule.min_quote_amount_increment
        ):
            raise ValueError(
                "selected order violates exact venue amount/notional rules"
            )
        # One durable position owner excludes other governed paths. No split is
        # created by this admission lock; source inventory remains untouched.
        self._no_competing_capital(proposal_id)
        self.ledger.claim_selected_recovery_lock(scope["parent"], self.owner_id)
        await self._passive_sources(client)
        self._fresh(health, market_observed_at)  # all awaits precede final checks
        self._native_matches_book()
        self._no_competing_capital(proposal_id)
        if validator(self.book.snapshot(), json.loads(plan.request_json)) != plan:
            raise ValueError("selected plan changed while checking source evidence")
        action_id = (
            "selected:"
            + _digest(
                {
                    "plan": _encode_plan(plan),
                    "proposal": proposal_id,
                    "fee_reserve": str(buy_fee_reserve_quote),
                }
            )[:32]
        )
        executor_id = action_id + ":executor"
        config = {
            "id": executor_id,
            "connector_name": "kraken",
            "trading_pair": scope["pair"],
            "side": 1 if kind == "BUY" else 2,
            "leverage": 1,
            "level_id": action_id,
        }
        if kind == "BUY":
            config.update(
                type="dca_executor",
                amounts_quote=[str(gross)],
                prices=[str(price)],
                mode="MAKER",
            )
            self.ledger.allow_selected_recovery_proposal(
                scope["parent"], self.owner_id, proposal_id
            )
        else:
            config.update(
                type="order_executor",
                amount=str(amount),
                price=str(price),
                execution_strategy="LIMIT",
            )
            self.ledger.create_selected_recovery_exit_scope(
                governor=self.governor,
                book=self.book,
                manifest_id=plan.manifest_id,
                owner_id=self.owner_id,
                scope=scope,
                slices=json.loads(plan.slices_json),
            )
        payload = {
            "kind": kind,
            "plan": _encode_plan(plan),
            "proposal_id": proposal_id,
            "executor_id": executor_id,
            "config": config,
            "amount": str(amount),
            "price": str(price),
            "scope": scope,
            "owner_id": self.owner_id,
        }
        self._claim_action(action_id, payload, plan)
        if capital is not None and not self.governor.commit_if_matches(capital):
            self._phase(
                action_id,
                "COMPLETE",
                "capital claim no longer matches; no external call",
            )
            return {"executed": False, "reason": "capital_claim_changed"}
        # Recheck after the exact claim commit as well: a delayed reconciler can
        # add inventory without awaiting in this adapter. Never spend on drift.
        try:
            self._fresh(health, market_observed_at)
            self._native_matches_book()
            self._no_competing_capital(proposal_id)
            if (
                self.ledger.selected_recovery_lock_owner(scope["parent"])
                != self.owner_id
            ):
                raise ValueError("selected mutation owner changed before send")
            if validator(self.book.snapshot(), json.loads(plan.request_json)) != plan:
                raise ValueError("selected plan changed immediately before send")
        except Exception as exc:
            self._phase(action_id, "BLOCKED", str(exc))
            raise
        # No awaited work may be inserted between these final checks and send.
        try:
            result = await client.executors.create_executor(
                executor_config=config,
                account_name=scope["account"],
                controller_id=scope["controller"],
            )
            if (result.get("executor_id") or result.get("id")) != executor_id:
                raise ValueError("external executor identity is ambiguous")
            if capital is not None:
                self.governor.reconcile_execution(
                    proposal_id=proposal_id,
                    executor_id=executor_id,
                    account_name=scope["account"],
                    connector_name=scope["connector"],
                    controller_id=scope["controller"],
                )
            self._phase(action_id, "DISPATCHED")
        except Exception as exc:
            self._phase(action_id, "UNCERTAIN", str(exc))
            raise
        return await self.synchronize(client)

    async def synchronize(self, client):
        action = self.active()
        if action is None:
            return {
                "executed": False,
                "reason": (
                    "no_selected_mutation_pending"
                    if self._enabled()
                    else "selected_recovery_disabled"
                ),
            }
        # Disabling rollout blocks NEW orders, never accounting for a previously
        # durable authorized attempt. This local handle only imports exact facts.
        book = (
            self.book
            if self.book.enabled
            else SelectedRecoveryBook(self.book.path, enabled=True)
        )
        from mcp_servers.hummingbot_api.tools.executor_provenance import (
            fetch_executor_order_snapshot,
            fetch_executor_trade_snapshot,
        )

        executor = action["executor_id"]
        # Missing/unknown/transport failures never become evidence of absence.
        trades = await fetch_executor_trade_snapshot(client, executor_id=executor)
        orders = await fetch_executor_order_snapshot(client, executor_id=executor)
        if not orders.orders:
            return {
                "executed": True,
                "pending": True,
                "reason": "awaiting_exact_order_evidence",
            }
        if len(orders.orders) != 1:
            raise ValueError("selected executor must own exactly one immutable order")
        order = orders.orders[0]
        if (
            order.side != action["kind"]
            or F(order.amount) != F(action["amount"])
            or order.order_type not in {"LIMIT", "LIMIT_MAKER"}
            or order.price is None
            or F(order.price) != F(action["price"])
        ):
            raise ValueError("external order differs from immutable selected dispatch")
        if any(
            fill.order_id != order.order_id or fill.side != action["kind"]
            for fill in trades.trades
        ):
            raise ValueError("selected fills reference a foreign order or side")
        observed = sum((F(fill.amount_base) for fill in trades.trades), F(0))
        if observed > F(order.amount) or observed != F(order.filled_amount):
            raise ValueError("selected order/trade totals differ")
        if (order.status == "FILLED" and observed != F(order.amount)) or (
            order.status in {"REJECTED", "FAILED"} and observed != 0
        ):
            raise ValueError("contradictory selected terminal order geometry")
        scope = action["scope"]
        held = self.ledger.selected_recovery_lock_owner(scope["parent"])
        terminal_replay = (
            held is None
            and not self.ledger.snapshot(scope["parent"]).has_active_inventory
            and self.book.snapshot()["pending"] is None
            and self.book.snapshot()["selected"] is None
        )
        if held != action["owner_id"] and not terminal_replay:
            raise ValueError("selected position lock changed")
        if action["kind"] == "BUY":
            plan = PatchPlan(
                **{**action["plan"], "amount_quote": F(action["plan"]["amount_quote"])}
            )
            if not self.governor.reconcile_execution(
                proposal_id=action["proposal_id"],
                executor_id=executor,
                account_name=scope["account"],
                connector_name=scope["connector"],
                controller_id=scope["controller"],
            ):
                # Existing identical receipts return True; no guessed ownership.
                raise ValueError("exact selected patch execution receipt unavailable")
            self.reconciler.bind_executor_order(
                order_id=order.order_id,
                position_id=scope["parent"],
                role=EconomicFillRole.VCAP_RECOVERY,
                proposal_id=action["proposal_id"],
                account_name=scope["account"],
                connector_name=scope["connector"],
                pair=scope["pair"],
                created_at=order.created_at,
            )
            book.bind_patch_order(
                plan, order_id=order.order_id, reconciler=self.reconciler
            )
            if (
                sum(
                    (
                        F(fill.amount_base) * F(fill.price) + F(fill.fee_paid)
                        for fill in trades.trades
                    ),
                    F(0),
                )
                > plan.amount_quote
            ):
                raise ValueError("confirmed patch total exceeds all-in capital claim")
            observation = executor_accounting_observation_from_raw(
                await client.executors.get_executor(executor),
                expected_executor_id=executor,
            )
            self.governor.reconcile_executor_accounting(observation)
            for fill in trades.trades:
                raw = {
                    "trade_id": fill.trade_id,
                    "order_id": fill.order_id,
                    "account_name": fill.account_name,
                    "connector_name": fill.connector_name,
                    "trading_pair": fill.pair,
                    "trade_type": fill.side,
                    "amount": str(fill.amount_base),
                    "price": str(fill.price),
                    "fee_paid": str(fill.fee_paid),
                    "fee_currency": fill.fee_currency,
                    "timestamp": datetime.fromtimestamp(
                        fill.occurred_at, timezone.utc
                    ).isoformat(),
                }
                self.reconciler.reconcile_trade_fill(raw, reconciled_at=self.clock())
                book.ingest_patch_fill(fill, reconciler=self.reconciler)
        else:
            plan = ExitPlan(
                **{
                    **action["plan"],
                    **{
                        key: F(action["plan"][key])
                        for key in (
                            "target_base",
                            "all_in_basis_quote",
                            "minimum_net_proceeds_quote",
                        )
                    },
                }
            )
            binding = replace(
                order,
                status="OPEN",
                filled_amount=Decimal(0),
                average_fill_price=None,
                updated_at=None,
            )
            book.bind_exit_order(plan, order=binding)
            for fill in trades.trades:
                event = f"selected-exit:{plan.manifest_id}:{fill.trade_id}"
                allocations = self.ledger.record_selected_recovery_exit(
                    manifest_id=plan.manifest_id,
                    owner_id=action["owner_id"],
                    event_id=event,
                    fill=fill,
                )
                book.ingest_sale_fill(
                    fill,
                    executor_id=executor,
                    manifest_id=plan.manifest_id,
                    source_allocations=allocations,
                )
                released = {}
                for row in allocations:
                    if row["owner"] == "VCAP":
                        released[row["proposal"]] = released.get(
                            row["proposal"], F(0)
                        ) + F(row["cost"])
                for proposal, cost in released.items():
                    if cost:
                        self.governor.record_recovery_recycle_release(
                            release_id=f"{event}:{proposal}",
                            proposal_id=proposal,
                            amount_quote=exact_decimal(cost),
                        )
        if order.status in TERMINAL:
            if action["kind"] == "BUY":
                if (
                    observation.status != "TERMINATED"
                    or observation.close_type != "POSITION_HOLD"
                ):
                    return {
                        "executed": True,
                        "pending": True,
                        "reason": "awaiting_passive_source_finality",
                    }
                self.governor.seal_selected_recovery_opening_basis(
                    ledger=self.ledger,
                    book=book,
                    owner_id=action["owner_id"],
                    proposal_id=action["proposal_id"],
                    order=replace(order, average_fill_price=None, updated_at=None),
                    fills=trades.trades,
                    observation=observation,
                )
            book.confirm_terminal(
                replace(order, average_fill_price=None, updated_at=None)
            )
            self._native_matches_book()
            if not self.ledger.snapshot(scope["parent"]).has_active_inventory:
                self.ledger.release_selected_recovery_lock(
                    scope["parent"], action["owner_id"]
                )
            self._phase(action["action_id"], "COMPLETE")
            self.release_unactivated_lock()
            return {
                "executed": True,
                "pending": False,
                "completed": True,
                "action": action["kind"],
                "executor_id": executor,
            }
        self._phase(action["action_id"], "AWAITING_RECONCILIATION")
        return {
            "executed": True,
            "pending": True,
            "action": action["kind"],
            "executor_id": executor,
        }


async def run_selected_recovery_cycle(
    client,
    *,
    runtime: SelectedRecoveryRuntime,
    child_id: str,
    ladder: tuple[RecoveryRung, ...],
    current_price: Decimal,
    market_observed_at: float,
    rule,
    health: AccountingHealthDecision,
    remaining_authorized_quote: Decimal,
    proposal_id: str | None = None,
    buy_fee_reserve_quote: Decimal = Decimal(0),
    expected_exit_cost_bps: Decimal = Decimal(0),
    evidence_allowed: bool = False,
):
    """Internal lifecycle entrypoint: resume, full-child exit, or next rung.

    This is deliberately not registered as an MCP tool and remains disabled by
    default. The caller must supply an existing exact all-in capital claim.
    """
    if runtime.active() is not None:
        return await runtime.synchronize(client)
    runtime.release_unactivated_lock()
    if not runtime._enabled():
        return {"executed": False, "reason": "selected_recovery_disabled"}
    exit_plan = runtime.book.plan_exit(
        current_price=current_price,
        expected_exit_cost_bps=expected_exit_cost_bps,
        minimum_base=rule.min_order_size,
        minimum_quote=rule.effective_min_quote,
        base_increment=rule.min_base_amount_increment,
    )
    plan = exit_plan or runtime.book.plan_patch(
        child_id=child_id,
        ladder=ladder,
        current_price=current_price,
        remaining_authorized_quote=remaining_authorized_quote,
        minimum_quote=rule.effective_min_quote,
        evidence_allowed=evidence_allowed,
    )
    if plan is None:
        return {"executed": False, "reason": "selected_recovery_hold"}
    return await runtime.dispatch(
        client,
        plan=plan,
        rule=rule,
        health=health,
        market_observed_at=market_observed_at,
        proposal_id=proposal_id if isinstance(plan, PatchPlan) else None,
        buy_fee_reserve_quote=buy_fee_reserve_quote,
    )
