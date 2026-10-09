"""Offline capital-quality overlay on the frozen parked-cash controller.

Only parked-source permission changes: shared versus strict purpose budgets,
and causal admission gates. Native ranking, planners, ownership, fees, reserves,
profit floors and exact source journals remain those of the frozen experiment.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal as D
import importlib.util
from pathlib import Path
import sys

_BASE_PATH = Path(__file__).resolve().parents[1] / "parked_cash" / "release_adapter.py"
_BASE_NAME = "spectra_frozen_parked_cash_adapter_for_quality"
if _BASE_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_BASE_NAME, _BASE_PATH)
    _base = importlib.util.module_from_spec(_spec)
    sys.modules[_BASE_NAME] = _base
    _spec.loader.exec_module(_base)
else:
    _base = sys.modules[_BASE_NAME]

Z, TOL = _base.Z, _base.TOL
KINDS = ("ENTRY", "RECOVERY")
MODES = {
    "OFF": {"cap": Z, "split": False, "quality": False},
    "SHARED_RAW": {"cap": D(100), "split": False, "quality": False},
    "SPLIT_RAW": {"cap": D(100), "split": True, "quality": False},
    "SHARED_QUALITY": {"cap": D(100), "split": False, "quality": True},
    "SPLIT_QUALITY": {"cap": D(100), "split": True, "quality": True},
}


def native_purpose(lot):
    source = getattr(lot.source, "value", lot.source)
    if source == "BASE_ENTRY":
        return "ENTRY"
    if source in {"VCAP_RECOVERY", "RECOVERY"}:
        return "RECOVERY"
    raise ArithmeticError(f"Unclassified native economic lot source: {source}")


class ReleaseController(_base.ReleaseController):
    def __init__(self, dataset, arm, gate=None):
        if arm not in MODES:
            raise ValueError(f"Unknown fixed capital-quality arm: {arm}")
        policy = MODES[arm]
        if policy["quality"] and gate is None:
            raise ValueError("Quality arm requires an explicit causal admission gate")
        super().__init__(dataset, "OFF" if arm == "OFF" else "BOTH")
        self.arm = arm
        self.cap = policy["cap"]
        self.kind_caps = {kind: D(50) for kind in KINDS} if policy["split"] else {}
        self.quality_enabled = policy["quality"]
        self.gate = gate
        # This history is append-only: sale does not rewrite a lot's purpose.
        self.lot_purposes = {lot_id: native_purpose(dataset.ledger.lots[lot_id])
                             for lot_id in self.lots}
        self.parked_spent_by_kind = {kind: Z for kind in KINDS}
        self.parked_retired_by_kind = {kind: Z for kind in KINDS}
        self.peak_committed_by_kind = {kind: Z for kind in KINDS}
        self.order_eligibility = {}
        self._decision_cache = {}

    def owned_for_kind(self, kind):
        return sum((lot.parked_basis for lot_id, lot in self.lots.items()
                    if self.lot_purposes[lot_id] == kind), Z)

    def reserved_for_kind(self, kind):
        return sum((claim.parked for claim in self.claims.values()
                    if claim.kind == kind), Z)

    def kind_remaining(self, kind):
        if kind not in self.kind_caps:
            return max(Z, self.cap - self.owned_parked_basis - self.reserved_parked)
        return max(Z, self.kind_caps[kind] - self.owned_for_kind(kind)
                   - self.reserved_for_kind(kind))

    def begin_planning(self, cash, equity, cfg, positions, timestamp):
        super().begin_planning(cash, equity, cfg, positions, timestamp)
        self._decision_cache.clear()

    def _eligible(self, pair, kind, timestamp):
        if not self.quality_enabled:
            return True
        if pair is None or timestamp is None:
            raise ArithmeticError("Causal admission requires pair and decision timestamp")
        key = (pair, kind, int(timestamp))
        if key not in self._decision_cache:
            # The gate is evaluated at the completed decision close, never at
            # the future bar used by native estimated fill execution.
            self._decision_cache[key] = bool(self.gate.eligible(*key))
            self.counts[f"quality_{kind}_eligible" if self._decision_cache[key]
                        else f"quality_{kind}_denied"] += 1
        return self._decision_cache[key]

    def authority(self, kind, pair=None, timestamp=None):
        if self.context is None:
            raise ArithmeticError("Funding authority requires native planning context")
        c = self.context
        physical = max(Z, c["cash"] - c["equity"] * c["cfg"].reserve_fraction
                       - self.reserved_total)
        ordinary = max(Z, c["cash"] - self.dataset.parked_cash() - self.reserved_ordinary)
        permitted = Z
        # A pair-free query is a loop's upper bound, not a per-asset permission.
        # The actual selected candidate is checked before tranche/order creation.
        eligible = pair is None or self._eligible(pair, kind, timestamp if timestamp is not None else c["timestamp"])
        if kind in self.allowed and eligible:
            permitted = min(max(Z, self.dataset.parked_cash() - self.reserved_parked),
                            max(Z, self.cap - self.owned_parked_basis - self.reserved_parked),
                            self.kind_remaining(kind))
        return min(physical, ordinary + permitted)

    def _make_claim(self, order, *, cash, conditional=False):
        ordinary_free = max(Z, cash - self.dataset.parked_cash() - self.reserved_ordinary)
        parked_needed = max(Z, order.budget - ordinary_free)
        if parked_needed > Z:
            eligible = self.order_eligibility.get(order.order_id)
            if eligible is None:
                eligible = self._eligible(order.pair, order.kind, order.submitted_at)
            if not eligible or parked_needed > self.kind_remaining(order.kind) + TOL:
                return None
        claim = super()._make_claim(order, cash=cash, conditional=conditional)
        if claim is not None:
            self.events[-1].update(
                decision_timestamp=order.submitted_at,
                purpose_cap=self.kind_caps.get(claim.kind),
                total_outstanding_cap=self.cap,
                purpose_committed=self.owned_for_kind(claim.kind) + self.reserved_for_kind(claim.kind),
                quality_required=self.quality_enabled and claim.parked > Z,
                qualified=self.order_eligibility.get(order.order_id) if self.quality_enabled else None,
            )
            self.peak_committed_by_kind[claim.kind] = max(
                self.peak_committed_by_kind[claim.kind],
                self.owned_for_kind(claim.kind) + self.reserved_for_kind(claim.kind))
        return claim

    def claim_order(self, order):
        if self.enabled and order.kind in KINDS:
            # Freeze conditional replacement eligibility before its exit has
            # settled. Future fill/capacity bars cannot change this permission.
            eligible = self._eligible(order.pair, order.kind, order.submitted_at)
            self.order_eligibility[order.order_id] = eligible
        return super().claim_order(order)

    def may_fill(self, order, total, cash, positions, index):
        if not super().may_fill(order, total, cash, positions, index):
            return False
        claim = self.claims.get(order.order_id)
        if claim is not None and claim.kind in self.kind_caps:
            if self.owned_for_kind(claim.kind) + self.reserved_for_kind(claim.kind) > self.kind_caps[claim.kind] + TOL:
                self.counts["purpose_fill_denials"] += 1
                return False
        return True

    def record_buy(self, fill, position, cash_before):
        lot_id = f"lot:{fill.order_id}"
        if lot_id in self.lot_purposes:
            raise ArithmeticError("Duplicate immutable source-purpose identity")
        if fill.kind not in KINDS:
            raise ArithmeticError("Unknown native acquisition purpose")
        claim = self.claims.get(fill.order_id)
        parked = Z if claim is None else claim.parked
        self.lot_purposes[lot_id] = fill.kind
        self.parked_spent_by_kind[fill.kind] += parked
        try:
            super().record_buy(fill, position, cash_before)
        except BaseException:
            # Preserve the original economic failure; this observer never
            # converts a failed native ownership mutation into successful cash.
            self.lot_purposes.pop(lot_id, None)
            self.parked_spent_by_kind[fill.kind] -= parked
            raise
        if native_purpose(self.dataset.ledger.lots[lot_id]) != fill.kind:
            raise ArithmeticError("Owned native lot/source acquisition purpose differs")

    def _source_sale(self, lots, net):
        records = super()._source_sale(lots, net)
        for record in records:
            record["purpose"] = self.lot_purposes[record["lot_id"]]
        return records

    def _retire(self, records, kind, order_id):
        for record in records:
            purpose = self.lot_purposes[record["lot_id"]]
            if record.get("purpose") != purpose:
                raise ArithmeticError("Disposal rewrote immutable funding purpose")
            self.parked_retired_by_kind[purpose] += record["parked_basis"]
        super()._retire(records, kind, order_id)

    def check(self, *, cash=None):
        super().check(cash=cash)
        for lot_id in self.lots:
            if self.lot_purposes[lot_id] != native_purpose(self.dataset.ledger.lots[lot_id]):
                raise ArithmeticError("Native and funding lot purposes differ")
        for kind in KINDS:
            committed = self.owned_for_kind(kind) + self.reserved_for_kind(kind)
            if kind in self.kind_caps and committed > self.kind_caps[kind] + TOL:
                raise ArithmeticError("Outstanding purpose budget exceeds its strict cap")
            if abs(self.parked_spent_by_kind[kind] - self.parked_retired_by_kind[kind]
                   - self.owned_for_kind(kind)) > TOL:
                raise ArithmeticError("Purpose acquisition basis conservation failed")
        return True

    def audit_snapshot(self):
        snapshot = super().audit_snapshot()
        snapshot.update(
            capital_quality_arm=self.arm,
            purpose_budget_caps=dict(self.kind_caps),
            purpose_cross_borrowing=False,
            quality_gate_enabled=self.quality_enabled,
            quality_applies_to="parked shortfall only; ordinary-first authority unchanged",
            owned_parked_basis_by_kind={kind: self.owned_for_kind(kind) for kind in KINDS},
            reserved_parked_by_kind={kind: self.reserved_for_kind(kind) for kind in KINDS},
            committed_parked_by_kind={kind: self.owned_for_kind(kind) + self.reserved_for_kind(kind) for kind in KINDS},
            peak_committed_by_kind=dict(self.peak_committed_by_kind),
            parked_spent_by_kind=dict(self.parked_spent_by_kind),
            parked_basis_retired_by_kind=dict(self.parked_retired_by_kind),
            terminal_order_states=dict(self.terminal),
            immutable_lot_purposes=dict(self.lot_purposes),
            purpose_by_lot=dict(self.lot_purposes),
            per_kind_owned_basis={kind: self.owned_for_kind(kind) for kind in KINDS},
            per_kind_reserved={kind: self.reserved_for_kind(kind) for kind in KINDS},
            per_kind_peak=dict(self.peak_committed_by_kind),
        )
        snapshot["active_source_lots"] = [dict(asdict(lot), purpose=self.lot_purposes[lot_id])
                                           for lot_id, lot in self.lots.items()]
        return snapshot


def attach(dataset, arm, gate=None):
    if hasattr(dataset, "release"):
        raise ArithmeticError("A funding controller is already attached")
    controller = ReleaseController(dataset, arm, gate)
    dataset.release = controller
    dataset.record_buy = controller.record_buy
    dataset.record_full_exit = controller.record_full_exit
    dataset.fill_partial = controller.fill_partial
    controller.check()
    return controller


def build_source(source):
    source, changes = _base.build_source(source)

    def change(before, after, purpose):
        nonlocal source
        if source.count(before) != 1:
            raise ArithmeticError(f"Capital-quality overlay boundary mismatch: {purpose}")
        source = source.replace(before, after)
        changes.append(dict(purpose=purpose, before=before, after=after))

    change('''            spendable = dataset.release.authority("RECOVERY")
            average = position.basis / position.quantity''',
           '''            spendable = dataset.release.authority("RECOVERY", pair, timestamp)
            average = position.basis / position.quantity''',
           "per-asset causal parked recovery permission before native tranche sizing")
    change('''            pair = candidates.pop(0)
            entry_orders.append(
                next_order(timestamp, pair, "ENTRY", budget=cfg.entry_budget)''',
           '''            pair = candidates.pop(0)
            if dataset.release.authority("ENTRY", pair, timestamp) < cfg.entry_budget:
                continue
            entry_orders.append(
                next_order(timestamp, pair, "ENTRY", budget=cfg.entry_budget)''',
           "raw native movement-ranked entry candidate retains ordinary permission; parked shortfall requires causal gate")
    return source, changes


def compile_hydrated(module, dataset, simulator):
    if not hasattr(dataset, "release"):
        raise ArithmeticError("Attach exact source controller before compilation")
    if not dataset.release.enabled:
        dataset.release_generated_source = dataset.hydration_generated_source
        dataset.release_source_changes = []
        return simulator
    source, changes = build_source(dataset.hydration_generated_source)
    namespace = dict(module.a.original.__dict__)
    namespace["FleetOrder"] = module.ResearchOrder
    exec(compile(source, str(Path(__file__).with_name("generated_quality_body.py")), "exec"), namespace)
    cls = type("ResearchCapitalQualitySimulator", (type(simulator),), {"run": namespace["run"]})
    simulator.__class__ = cls
    dataset.release_generated_source = source
    dataset.release_source_changes = changes
    dataset.manifest.update(
        parked_release_arm=dataset.release.arm,
        parked_release_policy="bounded outstanding source basis; causal P-only admission; strict purpose caps when declared",
        parked_release_cap=str(dataset.release.cap),
        purpose_budget_caps={kind: str(cap) for kind, cap in dataset.release.kind_caps.items()},
        purpose_cross_borrowing=False,
        quality_gate_enabled=dataset.release.quality_enabled,
        native_signal_planners_preserved=True,
        native_movement_entry_selector_preserved=True,
        predictive_winner_selector=False,
        no_new_history_or_wallet_freshness_gate=not dataset.release.quality_enabled,
    )
    return simulator
