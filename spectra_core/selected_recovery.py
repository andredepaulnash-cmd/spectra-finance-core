"""Opt-in selected-original-child partitions and source ownership accounting.

This book alone cannot authorize capital or submit orders. The separately gated
selected_recovery_runtime adapter performs native execution and reconciliation.
Read docs/selected-recovery.md before use. Rational plans are NOT exchange orders.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
from math import isfinite
from pathlib import Path

from .capital_governor import CapitalGovernor
from .economic_position import (
    EconomicLotSource,
    EconomicPositionLedger,
    EconomicPositionStatus,
    OwnershipBucket,
)
from .executor_provenance_bridge import ExecutorOrderEvidence
from .fill_reconciliation import (
    EconomicFillRole,
    LiveFillLedgerReconciler,
    TradeFillEvidence,
)
from .models import IntentKind

F = Fraction
ZERO = F(0)
BPS = F(10000)


def _text(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("nonempty, unambiguous identity required")
    return value


def _number(value: Decimal | Fraction | int, *, positive: bool = False) -> F:
    if isinstance(value, bool) or not isinstance(value, (Decimal, Fraction, int)):
        raise ValueError("exact Decimal, Fraction or integer required")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("finite amount required")
    result = F(value)
    if result < 0 or (positive and result == 0):
        raise ValueError("positive amount required" if positive else "negative amount")
    return result


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _total(lots: list[dict], field: str) -> F:
    return sum((F(lot[field]) for lot in lots), ZERO)


def _basis(lots: list[dict]) -> F:
    return _total(lots, "cost") + _total(lots, "fees")


def _all_lots(state: dict) -> list[dict]:
    if not state["split"]:
        return state["originals"]
    return [lot for lots in state["children"].values() for lot in lots]


@dataclass(frozen=True)
class RecoveryLimits:
    """Additional local ceilings; none grants authority to spend cash."""

    max_recovery_basis_quote: Decimal | Fraction
    max_asset_basis_quote: Decimal | Fraction
    protected_recovery_cash_quote: Decimal | Fraction = ZERO
    target_net_return_bps: Decimal | Fraction = F(174)

    def encoded(self) -> dict:
        values = {name: str(_number(value)) for name, value in asdict(self).items()}
        if F(values["max_recovery_basis_quote"]) <= 0:
            raise ValueError("explicit positive recovery exposure cap required")
        if F(values["max_asset_basis_quote"]) <= 0:
            raise ValueError("explicit positive whole-asset cap required")
        return values


@dataclass(frozen=True)
class RecoveryRung:
    rung_id: str
    trigger_price: Decimal | Fraction


def _ladder(rungs: tuple[RecoveryRung, ...]) -> list[dict]:
    result = [
        {
            "id": _text(rung.rung_id),
            "price": str(_number(rung.trigger_price, positive=True)),
        }
        for rung in rungs
    ]
    if not result or len({r["id"] for r in result}) != len(result):
        raise ValueError("distinct explicit ladder rung identities required")
    if any(F(a["price"]) <= F(b["price"]) for a, b in zip(result, result[1:])):
        raise ValueError("ladder trigger prices must strictly decrease")
    return result


def _provenance(
    governor: CapitalGovernor,
    proposal: str,
    executor: str,
    scope: dict,
    intent: IntentKind,
):
    proof = governor.get_execution_provenance(proposal)
    if (
        proof is None
        or governor.get_execution_receipt(proposal) != executor
        or proof.executor_id != executor
        or proof.proposal_id != proposal
        or proof.intent != intent
        or proof.pair != scope["pair"]
        or proof.account_name != scope["account"]
        or proof.connector_name != scope["connector"]
        or proof.controller_id != scope["controller"]
        or proof.leverage != Decimal(1)
    ):
        raise ValueError("exact scoped, unlevered execution provenance required")
    return proof


def original_inventory_manifest(
    *,
    ledger: EconomicPositionLedger,
    governor: CapitalGovernor,
    position_id: str,
    account_name: str,
    connector_name: str,
    controller_id: str,
) -> dict:
    """Read exact remaining ENTRY lots; wallet balances are never ownership."""
    snapshot = ledger.snapshot(_text(position_id))
    scope = {
        "parent": position_id,
        "pair": snapshot.pair,
        "account": _text(account_name),
        "connector": _text(connector_name),
        "controller": _text(controller_id),
    }
    if snapshot.status != EconomicPositionStatus.ACTIVE:
        raise ValueError("active original parent required")
    lots = []
    for lot in ledger.list_active_lots(position_id):
        if (
            lot.source != EconomicLotSource.BASE_ENTRY
            or lot.ownership != OwnershipBucket.ACTIVE_TRADE
            or not lot.proposal_id
            or not lot.executor_id
        ):
            raise ValueError(
                "parent must contain only exact owned original ENTRY inventory"
            )
        _provenance(governor, lot.proposal_id, lot.executor_id, scope, IntentKind.ENTRY)
        lots.append(
            {
                "id": lot.lot_id,
                "owner": "PRODUCTIVE",
                "kind": "ORIGINAL",
                "proposal": lot.proposal_id,
                "executor": lot.executor_id,
                "base": str(_number(lot.base_remaining, positive=True)),
                "cost": str(_number(lot.cost_basis_remaining_quote, positive=True)),
                "fees": str(_number(lot.entry_fees_remaining_quote)),
            }
        )
    if not lots:
        raise ValueError("original inventory required")
    return {"scope": scope, "lots": sorted(lots, key=lambda lot: lot["id"])}


@dataclass(frozen=True)
class PatchPlan:
    state_token: str
    child_id: str
    rung_id: str
    amount_quote: F
    # Canonical request is re-planned at bind time, so edited manifests fail closed.
    request_json: str


@dataclass(frozen=True)
class ExitPlan:
    state_token: str
    manifest_id: str
    child_id: str
    target_base: F
    all_in_basis_quote: F
    minimum_net_proceeds_quote: F
    slices_json: str
    request_json: str


class SelectedRecoveryBook:
    """Durable single-parent shadow accounting; disabled unless explicitly opted in.

    The SQL transaction covers the shadow state and receipt journal together.
    It is not an atomic transaction with exchange/capital/economic state.
    """

    def __init__(self, path: str | Path, *, enabled: bool = False):
        if type(enabled) is not bool:
            raise ValueError("enabled must be Boolean")
        self.enabled = enabled
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS selected_recovery_book (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS selected_recovery_events (
                    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _enabled(self):
        if not self.enabled:
            raise ValueError("selected recovery fidelity is disabled")

    @staticmethod
    def _read(conn) -> dict:
        row = conn.execute(
            "SELECT state FROM selected_recovery_book WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ValueError("book is not initialized")
        state = json.loads(row[0])
        _validate_state(state)
        return state

    def snapshot(self) -> dict:
        """Defensive JSON-compatible copy, with exact rational amount strings."""
        with self._connect() as conn:
            return self._read(conn)

    def initialize(
        self,
        *,
        ledger: EconomicPositionLedger,
        governor: CapitalGovernor,
        position_id: str,
        account_name: str,
        connector_name: str,
        controller_id: str,
        child_ids: tuple[str, ...],
        recovery_cash_quote: Decimal | Fraction,
        limits: RecoveryLimits,
    ) -> None:
        self._enabled()
        manifest = original_inventory_manifest(
            ledger=ledger,
            governor=governor,
            position_id=position_id,
            account_name=account_name,
            connector_name=connector_name,
            controller_id=controller_id,
        )
        ids = [_text(cid) for cid in child_ids]
        if len(ids) < 2 or len(ids) != len(set(ids)):
            raise ValueError("at least two distinct proposed children required")
        cash = str(_number(recovery_cash_quote))
        state = {
            "schema": 1,
            "revision": 0,
            "manifest": manifest,
            "manifest_id": _digest(manifest),
            "originals": manifest["lots"],
            "patch_sources": {},
            "child_ids": ids,
            "split": False,
            "children": {},
            "selected": None,
            "ladders": {},
            "pending": None,
            "limits": limits.encoded(),
            "orders": {},
            "seen_lots": [x["id"] for x in manifest["lots"]],
            "cash": {"PRODUCTIVE": "0", "VCAP": cash},
            "initial_cash": {"PRODUCTIVE": "0", "VCAP": cash},
            "spent": {"PRODUCTIVE": "0", "VCAP": "0"},
            "returned": {"PRODUCTIVE": "0", "VCAP": "0"},
            "released": {"PRODUCTIVE": "0", "VCAP": "0"},
            "realized": {"PRODUCTIVE": "0", "VCAP": "0"},
            "added_base": "0",
            "sold_base": "0",
            "exit_started": [],
            "floor_breaches": [],
            "sale_allocations": {},
        }
        _validate_state(state)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT state FROM selected_recovery_book WHERE singleton=1"
            ).fetchone()
            if existing:
                if json.loads(existing[0]) != state:
                    raise ValueError(
                        "book already initialized; no resets or cash top-ups"
                    )
                return
            conn.execute(
                "INSERT INTO selected_recovery_book VALUES(1, ?)", (_json(state),)
            )

    def _apply(self, event_id: str, payload: dict, transition) -> dict:
        self._enabled()
        _text(event_id)
        serialized = _json(payload)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._read(conn)
            previous = conn.execute(
                "SELECT payload FROM selected_recovery_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if previous:
                if previous[0] != serialized:
                    raise ValueError("receipt replay conflicts with recorded payload")
                return state
            transition(state)
            state["revision"] += 1
            _validate_state(state)
            conn.execute(
                "UPDATE selected_recovery_book SET state=? WHERE singleton=1",
                (_json(state),),
            )
            conn.execute(
                "INSERT INTO selected_recovery_events VALUES(?, ?)",
                (event_id, serialized),
            )
            return state

    def plan_patch(
        self,
        *,
        child_id: str,
        ladder: tuple[RecoveryRung, ...],
        current_price: Decimal | Fraction,
        remaining_authorized_quote: Decimal | Fraction,
        minimum_quote: Decimal | Fraction,
        evidence_allowed: bool,
        parent_action_pending: bool = False,
    ) -> PatchPlan | None:
        """Pure proposal: reads only, never splits, reserves, spends, or selects."""
        if not self.enabled:
            return None
        request = {
            "child": _text(child_id),
            "ladder": _ladder(ladder),
            "price": str(_number(current_price, positive=True)),
            "authorized": str(_number(remaining_authorized_quote)),
            "minimum": str(_number(minimum_quote, positive=True)),
            "evidence": evidence_allowed,
            "parent_pending": parent_action_pending,
        }
        if (
            type(evidence_allowed) is not bool
            or type(parent_action_pending) is not bool
        ):
            raise ValueError(
                "explicit Boolean evidence and parent action state required"
            )
        return _patch_plan(self.snapshot(), request)

    def bind_patch_order(
        self, plan: PatchPlan, *, order_id: str, reconciler: LiveFillLedgerReconciler
    ) -> dict:
        """Observe an already authorized, exactly bound external order.

        No order is submitted and no capital reservation is created here.
        """
        self._enabled()
        order_id = _text(order_id)
        binding = reconciler.get_binding(order_id)
        if binding is None:
            raise ValueError("persisted exact fill binding required")
        payload = {
            "type": "BIND_PATCH",
            "plan": _encode_plan(plan),
            "binding": {**asdict(binding), "role": binding.role.value},
        }

        def transition(state):
            if _patch_plan(state, json.loads(plan.request_json)) != plan:
                raise ValueError("stale or changed patch manifest")
            scope = state["manifest"]["scope"]
            proof = _patch_binding(binding, scope, reconciler.governor)
            if _number(proof.authorized_quote) != plan.amount_quote:
                raise ValueError("exact all-in rung remainder authorization required")
            if order_id in state["orders"] or any(
                x["proposal"] == binding.proposal_id for x in state["orders"].values()
            ):
                raise ValueError("order/proposal cannot fund two rung attempts")
            request = json.loads(plan.request_json)
            state["pending"] = {
                "kind": "BUY",
                "order": order_id,
                "executor": binding.executor_id,
                "proposal": binding.proposal_id,
                "child": plan.child_id,
                "rung": plan.rung_id,
                "ladder": request["ladder"],
                "budget": str(plan.amount_quote),
                "spent": "0",
                "filled_base": "0",
            }
            state["orders"][order_id] = {
                "proposal": binding.proposal_id,
                "status": "OPEN",
            }

        return self._apply(f"bind:{order_id}", payload, transition)

    def ingest_patch_fill(
        self, fill: TradeFillEvidence, *, reconciler: LiveFillLedgerReconciler
    ) -> dict:
        """Import a confirmed BUY already reconciled into an exact economic lot."""
        self._enabled()
        payload = {"type": "PATCH_FILL", "fill": _fill_payload(fill)}

        def transition(state):
            pending = _pending(state, fill.order_id, "BUY")
            scope = state["manifest"]["scope"]
            _validate_fill(fill, scope, "BUY")
            binding = reconciler.get_binding(fill.order_id)
            if binding is None:
                raise ValueError("persisted exact fill binding required")
            proof = _patch_binding(binding, scope, reconciler.governor)
            if F(proof.authorized_quote) != F(pending["budget"]):
                raise ValueError("recorded patch authorization changed")
            if (
                binding.proposal_id != pending["proposal"]
                or binding.executor_id != pending["executor"]
            ):
                raise ValueError("patch source ownership changed")
            lot = reconciler.ledger.get_lot(f"trade-fill:{fill.trade_id}")
            q, cost, fees = (
                _number(fill.amount_base, positive=True),
                F(fill.amount_base) * F(fill.price),
                F(fill.fee_paid),
            )
            if (
                lot.position_id != scope["parent"]
                or lot.pair != scope["pair"]
                or lot.source != EconomicLotSource.VCAP_RECOVERY
                or lot.ownership != OwnershipBucket.ACTIVE_TRADE
                or lot.proposal_id != binding.proposal_id
                or lot.executor_id != binding.executor_id
                or F(lot.base_original) != q
                or F(lot.base_remaining) != q
                or F(lot.cost_basis_original_quote) != cost
                or F(lot.cost_basis_remaining_quote) != cost
                or F(lot.entry_fees_original_quote) != fees
                or F(lot.entry_fees_remaining_quote) != fees
                or lot.created_at != fill.occurred_at
            ):
                raise ValueError(
                    "confirmed economic patch lot differs from exact trade"
                )
            # Require the public reconciler's persisted trade evidence as well as its lot.
            _persisted_trade(
                reconciler, fill, EconomicFillRole.VCAP_RECOVERY, scope["parent"]
            )
            if lot.lot_id in state["seen_lots"]:
                raise ValueError("patch lot already allocated")
            amount = cost + fees
            if F(pending["spent"]) + amount > F(pending["budget"]):
                raise ValueError("fill exceeds frozen all-in rung remainder")
            trigger = next(
                r["price"] for r in pending["ladder"] if r["id"] == pending["rung"]
            )
            if F(fill.price) > F(trigger):
                raise ValueError("confirmed patch execution is above its bound rung")
            if state["selected"] is None:
                starting = (
                    state["children"][pending["child"]]
                    if state["split"]
                    else state["originals"]
                )
                if _total(starting, "base") * F(fill.price) >= _basis(starting):
                    raise ValueError("cannot start recovery on healthy inventory")
            if not state["split"]:
                # Check original source lots did not drift between plan and first execution.
                for original in state["originals"]:
                    live = reconciler.ledger.get_lot(original["id"])
                    _provenance(
                        reconciler.governor,
                        original["proposal"],
                        original["executor"],
                        scope,
                        IntentKind.ENTRY,
                    )
                    if (
                        live.position_id != scope["parent"]
                        or live.pair != scope["pair"]
                        or live.source != EconomicLotSource.BASE_ENTRY
                        or live.ownership != OwnershipBucket.ACTIVE_TRADE
                        or F(live.base_remaining) != F(original["base"])
                        or F(live.cost_basis_remaining_quote) != F(original["cost"])
                        or F(live.entry_fees_remaining_quote) != F(original["fees"])
                        or live.proposal_id != original["proposal"]
                        or live.executor_id != original["executor"]
                    ):
                        raise ValueError(
                            "original parent manifest drifted before split"
                        )
                original_ids = {lot["id"] for lot in state["originals"]}
                live_original_ids = {
                    lot.lot_id
                    for lot in reconciler.ledger.list_active_lots(scope["parent"])
                    if lot.source == EconomicLotSource.BASE_ENTRY
                }
                if live_original_ids != original_ids:
                    raise ValueError("original parent manifest membership changed")
                count = len(state["child_ids"])
                state["children"] = {
                    cid: [
                        {
                            **source,
                            **{
                                field: str(F(source[field]) / count)
                                for field in ("base", "cost", "fees")
                            },
                        }
                        for source in state["originals"]
                    ]
                    for cid in state["child_ids"]
                }
                state["split"] = True
            child = pending["child"]
            state["selected"] = child
            if child not in state["ladders"]:
                state["ladders"][child] = [
                    {**r, "budget": None, "spent": "0"} for r in pending["ladder"]
                ]
            rung = next(
                r for r in state["ladders"][child] if r["id"] == pending["rung"]
            )
            if rung["budget"] is None:
                rung["budget"] = str(_basis(state["children"][child]))
            rung["spent"] = str(F(rung["spent"]) + amount)
            state["children"][child].append(
                {
                    "id": lot.lot_id,
                    "owner": "VCAP",
                    "kind": "PATCH",
                    "proposal": binding.proposal_id,
                    "executor": binding.executor_id,
                    "base": str(q),
                    "cost": str(cost),
                    "fees": str(fees),
                }
            )
            state["patch_sources"][lot.lot_id] = {
                **state["children"][child][-1],
                "child_id": child,
            }
            state["seen_lots"].append(lot.lot_id)
            pending["spent"] = str(F(pending["spent"]) + amount)
            pending["filled_base"] = str(F(pending["filled_base"]) + q)
            state["cash"]["VCAP"] = str(F(state["cash"]["VCAP"]) - amount)
            state["spent"]["VCAP"] = str(F(state["spent"]["VCAP"]) + amount)
            state["added_base"] = str(F(state["added_base"]) + q)

        return self._apply(f"trade:{fill.trade_id}", payload, transition)

    def confirm_terminal(self, order: ExecutorOrderEvidence) -> dict:
        """Require final external order totals to equal every imported fill.

        An order snapshot is evidence supplied by the caller, not proof fetched
        by this library. Adapters must fetch it from the authoritative executor.
        """
        self._enabled()
        if order.status not in {
            "FILLED",
            "CANCELED",
            "CANCELLED",
            "REJECTED",
            "FAILED",
        }:
            raise ValueError("authoritative terminal order status required")
        payload = {"type": "TERMINAL", "order": _order_payload(order)}

        def transition(state):
            pending = _pending(state, order.order_id, order.side)
            if order.executor_id != pending["executor"]:
                raise ValueError("terminal executor differs")
            if _number(order.filled_amount) != F(pending["filled_base"]):
                raise ValueError("terminal fill total not fully reconciled")
            amount = _number(order.amount, positive=True)
            if F(order.filled_amount) > amount or (
                order.status == "FILLED" and F(order.filled_amount) != amount
            ):
                raise ValueError("contradictory terminal amount")
            if order.status in {"REJECTED", "FAILED"} and F(order.filled_amount) != 0:
                raise ValueError("rejected order claims executed fills")
            state["orders"][order.order_id]["status"] = order.status
            state["pending"] = None
            if pending["kind"] == "SELL" and not state["children"][pending["child"]]:
                state["selected"] = None

        return self._apply(f"terminal:{order.order_id}", payload, transition)

    def plan_exit(
        self,
        *,
        current_price: Decimal | Fraction,
        expected_exit_cost_bps: Decimal | Fraction,
        minimum_base: Decimal | Fraction,
        minimum_quote: Decimal | Fraction,
        base_increment: Decimal | Fraction,
    ) -> ExitPlan | None:
        """Manifest the entire remaining selected original child AND all patches."""
        if not self.enabled:
            return None
        request = {
            "price": str(_number(current_price, positive=True)),
            "cost_bps": str(_number(expected_exit_cost_bps)),
            "min_base": str(_number(minimum_base, positive=True)),
            "min_quote": str(_number(minimum_quote, positive=True)),
            "increment": str(_number(base_increment, positive=True)),
        }
        if F(request["cost_bps"]) >= BPS:
            raise ValueError("exit cost must be below 100%")
        return _exit_plan(self.snapshot(), request)

    def bind_exit_order(self, plan: ExitPlan, *, order: ExecutorOrderEvidence) -> dict:
        """Observe a caller-authorized sell of the exact full manifest, not a subset.

        Deliberately no live economic-ledger sell adapter: its FIFO parent exit
        cannot express these original-child subdivisions safely.
        """
        self._enabled()
        payload = {
            "type": "BIND_EXIT",
            "plan": _encode_plan(plan),
            "order": _order_payload(order),
        }

        def transition(state):
            if _exit_plan(state, json.loads(plan.request_json)) != plan:
                raise ValueError("stale or changed selected-child exit manifest")
            if order.order_id in state["orders"] or order.side != "SELL":
                raise ValueError("unique exact SELL order required")
            if (
                order.status not in {"OPEN", "PENDING_CREATE"}
                or F(order.filled_amount) != 0
            ):
                raise ValueError("unfilled open exit order required")
            if F(order.amount) != plan.target_base:
                raise ValueError(
                    "exit must target the entire selected child plus patches"
                )
            request = json.loads(plan.request_json)
            if order.price is None or F(order.price) < F(request["price"]):
                raise ValueError("limit price must satisfy the planned fee-aware floor")
            if order.order_type not in {"LIMIT", "LIMIT_MAKER"}:
                raise ValueError("selected recovery exit requires a priced limit")
            state["pending"] = {
                "kind": "SELL",
                "order": order.order_id,
                "executor": order.executor_id,
                "child": plan.child_id,
                "manifest": plan.manifest_id,
                "filled_base": "0",
            }
            state["orders"][order.order_id] = {"proposal": None, "status": "OPEN"}
            if plan.child_id not in state["exit_started"]:
                state["exit_started"].append(plan.child_id)

        return self._apply(f"bind:{order.order_id}", payload, transition)

    def ingest_sale_fill(
        self,
        fill: TradeFillEvidence,
        *,
        executor_id: str,
        manifest_id: str,
        source_allocations: list[dict] | None = None,
    ) -> dict:
        """Apply exact caller-supplied confirmed sale facts to the shadow book.

        A worse-than-planned real fill is recorded and flagged, never hidden.
        Proceeds return to the same source owners. No external cash is credited.
        """
        self._enabled()
        payload = {
            "type": "SALE_FILL",
            "fill": _fill_payload(fill),
            "executor": _text(executor_id),
            "manifest": _text(manifest_id),
            "native_allocations": source_allocations,
        }

        def transition(state):
            pending = _pending(state, fill.order_id, "SELL")
            _validate_fill(fill, state["manifest"]["scope"], "SELL")
            if pending["executor"] != executor_id or pending["manifest"] != manifest_id:
                raise ValueError("sale executor or exact child manifest differs")
            lots = state["children"][pending["child"]]
            total_base = _total(lots, "base")
            sold = F(fill.amount_base)
            if sold > total_base:
                raise ValueError("sale exceeds exact selected inventory")
            gross, fee = sold * F(fill.price), F(fill.fee_paid)
            if fee > gross:
                raise ValueError("fee exceeds proceeds")
            fraction = sold / total_base
            pieces = {
                lot["id"]: {
                    key: F(lot[key]) * fraction for key in ("base", "cost", "fees")
                }
                for lot in lots
            }
            if source_allocations is not None:
                current = {lot["id"]: lot for lot in lots}
                pieces = {
                    lot["id"]: {key: ZERO for key in ("base", "cost", "fees")}
                    for lot in lots
                }
                seen = set()
                for row in source_allocations:
                    lot = current.get(row["id"])
                    if (
                        lot is None
                        or row["id"] in seen
                        or any(
                            row[k] != lot[k] for k in ("owner", "proposal", "executor")
                        )
                    ):
                        raise ValueError("native sale allocation ownership differs")
                    seen.add(row["id"])
                    for key in ("base", "cost", "fees"):
                        piece = _number(F(row[key]))
                        if piece > F(lot[key]):
                            raise ValueError(
                                "native sale allocation exceeds child source"
                            )
                        pieces[row["id"]][key] = piece
                if sum((row["base"] for row in pieces.values()), ZERO) != sold:
                    raise ValueError("native sale allocation quantity differs")
            released = sum(
                (piece["cost"] + piece["fees"] for piece in pieces.values()), ZERO
            )
            floor = released * (1 + F(state["limits"]["target_net_return_bps"]) / BPS)
            if gross - fee < floor:
                state["floor_breaches"].append(fill.trade_id)
            allocations = []
            for lot in lots:
                piece = pieces[lot["id"]]
                share = piece["base"] / sold
                owner = lot["owner"]
                net = (gross - fee) * share
                basis = piece["cost"] + piece["fees"]
                allocations.append(
                    {
                        "source_lot_id": lot["id"],
                        "owner": owner,
                        "proposal_id": lot["proposal"],
                        "executor_id": lot["executor"],
                        "child_id": pending["child"],
                        "base_sold": str(piece["base"]),
                        "cost_released": str(piece["cost"]),
                        "entry_fees_released": str(piece["fees"]),
                        "gross_proceeds": str(gross * share),
                        "exit_fee": str(fee * share),
                        "net_proceeds": str(net),
                        "realized_pnl": str(net - basis),
                    }
                )
                state["cash"][owner] = str(F(state["cash"][owner]) + net)
                state["returned"][owner] = str(F(state["returned"][owner]) + net)
                state["released"][owner] = str(F(state["released"][owner]) + basis)
                state["realized"][owner] = str(
                    F(state["realized"][owner]) + net - basis
                )
                for key in ("base", "cost", "fees"):
                    lot[key] = str(F(lot[key]) - piece[key])
            state["sale_allocations"][fill.trade_id] = allocations
            state["children"][pending["child"]] = [
                lot for lot in lots if F(lot["base"]) > 0
            ]
            state["sold_base"] = str(F(state["sold_base"]) + sold)
            pending["filled_base"] = str(F(pending["filled_base"]) + sold)

        return self._apply(f"trade:{fill.trade_id}", payload, transition)


def _encode_plan(plan) -> dict:
    return {k: str(v) if isinstance(v, F) else v for k, v in asdict(plan).items()}


def _patch_binding(binding, scope, governor):
    if (
        binding.role != EconomicFillRole.VCAP_RECOVERY
        or binding.position_id != scope["parent"]
        or binding.pair != scope["pair"]
        or binding.account_name != scope["account"]
        or binding.connector_name != scope["connector"]
    ):
        raise ValueError("recovery order binding crosses exact owner scope")
    return _provenance(
        governor, binding.proposal_id, binding.executor_id, scope, IntentKind.RECOVERY
    )


def _fill_payload(fill):
    result = asdict(fill)
    for key in ("amount_base", "price", "fee_paid"):
        result[key] = str(_number(result[key], positive=key != "fee_paid"))
    for key in (
        "trade_id",
        "order_id",
        "account_name",
        "connector_name",
        "pair",
        "side",
    ):
        _text(result[key])
    if (
        isinstance(fill.occurred_at, bool)
        or not isfinite(fill.occurred_at)
        or fill.occurred_at <= 0
    ):
        raise ValueError("confirmed positive fill time required")
    return result


def _order_payload(order):
    result = asdict(order)
    for key in ("executor_id", "order_id", "side", "status", "order_type"):
        _text(result[key])
    for key in ("amount", "filled_amount", "price", "average_fill_price"):
        if result[key] is not None:
            result[key] = str(
                _number(
                    result[key],
                    positive=key in {"amount", "price", "average_fill_price"},
                )
            )
    if order.created_at is None:
        raise ValueError("order creation time required")
    for key in ("created_at", "updated_at"):
        value = result[key]
        if value is not None and (
            isinstance(value, bool) or not isfinite(value) or value <= 0
        ):
            raise ValueError("finite positive order time required")
    return result


def _validate_fill(fill, scope, side):
    _fill_payload(fill)
    if (
        fill.pair != scope["pair"]
        or fill.account_name != scope["account"]
        or fill.connector_name != scope["connector"]
        or fill.side != side
    ):
        raise ValueError("fill differs from bound account, connector, pair or side")
    if fill.fee_paid and fill.fee_currency != scope["pair"].split("-")[1]:
        raise ValueError("only confirmed quote-denominated fees supported")


def _persisted_trade(reconciler, fill, role, position):
    # Deliberately read-only. The module never calls reconcile_trade_fill itself.
    with sqlite3.connect(f"file:{reconciler.ledger.path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM economic_trade_fill_reconciliation WHERE trade_id=?",
            (fill.trade_id,),
        ).fetchone()
    if row is None:
        raise ValueError("trade has not been durably reconciled")
    expected = {
        "order_id": fill.order_id,
        "account_name": fill.account_name,
        "connector_name": fill.connector_name,
        "pair": fill.pair,
        "side": fill.side,
        "role": role.value,
        "position_id": position,
        "result_id": f"trade-fill:{fill.trade_id}",
        "fee_currency": fill.fee_currency,
        "occurred_at": fill.occurred_at,
    }
    if any(row[key] != value for key, value in expected.items()) or any(
        F(row[key]) != F(value)
        for key, value in (
            ("amount_base", fill.amount_base),
            ("price", fill.price),
            ("fee_paid", fill.fee_paid),
        )
    ):
        raise ValueError("persisted trade evidence differs")


def _pending(state, order, side):
    pending = state["pending"]
    if pending is None or pending["order"] != order or pending["kind"] != side:
        raise ValueError("exact pending order required")
    return pending


def _patch_plan(state, request):
    if set(request) != {
        "child",
        "ladder",
        "price",
        "authorized",
        "minimum",
        "evidence",
        "parent_pending",
    }:
        raise ValueError("invalid patch planning request")
    if (
        type(request["evidence"]) is not bool
        or type(request["parent_pending"]) is not bool
    ):
        raise ValueError("explicit Boolean planning gates required")
    _text(request["child"])
    for key in ("price", "authorized", "minimum"):
        _number(F(request[key]), positive=key != "authorized")
    if (
        _ladder(tuple(RecoveryRung(r["id"], F(r["price"])) for r in request["ladder"]))
        != request["ladder"]
    ):
        raise ValueError("invalid bound ladder")
    child, ladder = request["child"], request["ladder"]
    if child not in state["child_ids"]:
        raise ValueError("unknown original child")
    if state["selected"] is not None and state["selected"] != child:
        raise ValueError("another original child owns the recovery lock")
    if state["pending"] or child in state["exit_started"] or state["floor_breaches"]:
        return None
    if not request["evidence"] or request["parent_pending"]:
        return None
    if not state["split"]:
        lots = [
            {
                **source,
                **{
                    key: str(F(source[key]) / len(state["child_ids"]))
                    for key in ("base", "cost", "fees")
                },
            }
            for source in state["originals"]
        ]
    else:
        lots = state["children"][child]
    if not lots or _total(lots, "base") * F(request["price"]) >= _basis(lots):
        return None
    bound = state["ladders"].get(child)
    if bound and [{"id": r["id"], "price": r["price"]} for r in bound] != ladder:
        raise ValueError("selected child's bound ladder cannot change")
    progress = bound or [{**r, "budget": None, "spent": "0"} for r in ladder]
    rung = next(
        (r for r in progress if r["budget"] is None or F(r["spent"]) < F(r["budget"])),
        None,
    )
    if rung is None or F(request["price"]) > F(rung["price"]):
        return None
    amount = (
        _basis(lots) if rung["budget"] is None else F(rung["budget"]) - F(rung["spent"])
    )
    limits = state["limits"]
    active = _all_lots(state)
    recovery_basis = _basis([lot for lot in active if lot["kind"] == "PATCH"])
    if (
        amount < F(request["minimum"])
        or amount > F(request["authorized"])
        or amount
        > F(state["cash"]["VCAP"]) - F(limits["protected_recovery_cash_quote"])
        or recovery_basis + amount > F(limits["max_recovery_basis_quote"])
        or _basis(active) + amount > F(limits["max_asset_basis_quote"])
    ):
        return None
    return PatchPlan(_digest(state), child, rung["id"], amount, _json(request))


def _exit_plan(state, request):
    if set(request) != {"price", "cost_bps", "min_base", "min_quote", "increment"}:
        raise ValueError("invalid exit planning request")
    for key, value in request.items():
        _number(F(value), positive=key != "cost_bps")
    if F(request["cost_bps"]) >= BPS:
        raise ValueError("exit cost must be below 100%")
    child = state["selected"]
    if child is None or state["pending"]:
        return None
    lots = state["children"][child]
    quantity, basis = _total(lots, "base"), _basis(lots)
    if not lots:
        return None
    gross = quantity * F(request["price"])
    floor = basis * (1 + F(state["limits"]["target_net_return_bps"]) / BPS)
    if (
        quantity < F(request["min_base"])
        or gross < F(request["min_quote"])
        or quantity % F(request["increment"]) != 0
        or gross * (1 - F(request["cost_bps"]) / BPS) < floor
    ):
        return None
    token = _digest(state)
    manifest = _digest(
        {"state": token, "child": child, "slices": lots, "request": request}
    )
    return ExitPlan(
        token, manifest, child, quantity, basis, floor, _json(lots), _json(request)
    )


def _validate_state(state):
    """Conservation checks run before every read and before transaction commit."""
    if state["schema"] != 1:
        raise ValueError("unsupported selected recovery schema")
    if (
        _digest(state["manifest"]) != state["manifest_id"]
        or state["originals"] != state["manifest"]["lots"]
    ):
        raise ValueError("original manifest identity changed")
    lots = _all_lots(state)
    if state["split"] != bool(state["children"]):
        raise ValueError("inconsistent split state")
    if state["split"] and set(state["children"]) != set(state["child_ids"]):
        raise ValueError("original child manifest changed")
    if state["selected"] is not None and state["selected"] not in state["children"]:
        raise ValueError("selected original child missing")
    for lot in lots:
        if lot["owner"] not in {"PRODUCTIVE", "VCAP"}:
            raise ValueError("unknown cash owner")
        for key in ("base", "cost", "fees"):
            _number(F(lot[key]), positive=key != "fees")
    if _total(lots, "base") + F(state["sold_base"]) != _total(
        state["originals"], "base"
    ) + F(state["added_base"]):
        raise ValueError("base conservation failed")
    for owner in ("PRODUCTIVE", "VCAP"):
        cash, spent, returned, released, realized, initial = (
            F(state[key][owner])
            for key in (
                "cash",
                "spent",
                "returned",
                "released",
                "realized",
                "initial_cash",
            )
        )
        if (
            cash < 0
            or cash != initial - spent + returned
            or realized != returned - released
        ):
            raise ValueError("owner cash or realized P&L conservation failed")
        original_basis = _basis(
            [lot for lot in state["originals"] if lot["owner"] == owner]
        )
        remaining_basis = _basis([lot for lot in lots if lot["owner"] == owner])
        if remaining_basis + released != original_basis + spent:
            raise ValueError("owner basis conservation failed")
    for child, rungs in state["ladders"].items():
        incomplete = False
        for rung in rungs:
            spent = F(rung["spent"])
            budget = None if rung["budget"] is None else F(rung["budget"])
            if (
                spent < 0
                or (budget is None and spent)
                or (budget is not None and not 0 <= spent <= budget)
            ):
                raise ValueError("rung accounting failed")
            if incomplete and (budget is not None or spent):
                raise ValueError("ladder rung was skipped")
            incomplete = incomplete or budget is None or spent < budget

    # Check conservation by exact original/patch source AND original child, not
    # merely aggregate balances. This catches owner swaps and sibling drift.
    sources = {lot["id"]: lot for lot in state["originals"]}
    sources.update(state["patch_sources"])
    if len(state["seen_lots"]) != len(set(state["seen_lots"])) or set(sources) != set(
        state["seen_lots"]
    ):
        raise ValueError("source identity manifest changed")
    if not state["split"]:
        if state["patch_sources"] or state["sale_allocations"] or state["selected"]:
            raise ValueError("unsplit parent has recovery activity")
        return
    conserved = {}
    for child, remaining in state["children"].items():
        for lot in remaining:
            key = (child, lot["id"])
            if key in conserved:
                raise ValueError("duplicate child source slice")
            source = sources.get(lot["id"])
            if source is None or any(
                lot[field] != source[field]
                for field in ("owner", "kind", "proposal", "executor")
            ):
                raise ValueError("remaining source owner changed")
            conserved[key] = [F(lot[field]) for field in ("base", "cost", "fees")]
    allocations = [row for rows in state["sale_allocations"].values() for row in rows]
    for row in allocations:
        source = sources.get(row["source_lot_id"])
        if source is None or (row["owner"], row["proposal_id"], row["executor_id"]) != (
            source["owner"],
            source["proposal"],
            source["executor"],
        ):
            raise ValueError("settled source owner changed")
        values = [
            F(row[field])
            for field in ("base_sold", "cost_released", "entry_fees_released")
        ]
        if any(value < 0 for value in values) or F(row["gross_proceeds"]) - F(
            row["exit_fee"]
        ) != F(row["net_proceeds"]):
            raise ValueError("invalid exact source settlement")
        if F(row["net_proceeds"]) - values[1] - values[2] != F(row["realized_pnl"]):
            raise ValueError("source settlement P&L differs")
        key = (row["child_id"], row["source_lot_id"])
        before = conserved.setdefault(key, [ZERO, ZERO, ZERO])
        conserved[key] = [a + b for a, b in zip(before, values)]
    expected = {}
    for source in sources.values():
        children = (
            state["child_ids"] if source["kind"] == "ORIGINAL" else [source["child_id"]]
        )
        divisor = len(state["child_ids"]) if source["kind"] == "ORIGINAL" else 1
        for child in children:
            expected[(child, source["id"])] = [
                F(source[field]) / divisor for field in ("base", "cost", "fees")
            ]
    if conserved != expected:
        raise ValueError("exact source/child conservation failed")
    for owner in ("PRODUCTIVE", "VCAP"):
        rows = [row for row in allocations if row["owner"] == owner]
        if sum((F(row["net_proceeds"]) for row in rows), ZERO) != F(
            state["returned"][owner]
        ):
            raise ValueError("owner settlement return differs")
        if sum((F(row["realized_pnl"]) for row in rows), ZERO) != F(
            state["realized"][owner]
        ):
            raise ValueError("owner settlement P&L differs")
