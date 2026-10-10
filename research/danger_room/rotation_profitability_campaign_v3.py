"""Separately admitted, source-bound whole-parent rotation research campaign.

This module adapts the preserved native rotating-fleet research bridge. It does
not substitute a generic strategy for the product, activate a live module, or
assert exchange fills. Controls retain parked partial proceeds and the original
finite shared-cash reserve. All optional policy changes must be declared in a
frozen external protocol before historical outcomes are evaluated.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, getcontext
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from typing import Mapping

D = Decimal
ZERO = D(0)
ONE = D(1)
BPS = D(10000)
REFERENCE_ARRAY_SHA256 = "bf9ad858e2b696d189a42cb7f5d084f5312611bdc4b841f8a10219110ce552b1"
REFERENCE_METADATA_SHA256 = "0e8753e7436e8bff4a2065b9abdaddb8846a2546f1a107cefa4e7baaa3f00732"
ADAPTER_SHA256 = "45249e0273bfa4b15cfc54db2bede37b81c0488239fab1503c8911584127583e"
BRIDGE_SHA256 = "87247fd166775c3230445ed06e29f5a4ba2ad0e9253a9a52e3ff5e085f7db6c7"
GENERATED_NATIVE_SHA256 = "9260c013c5c85088f0098d76d39f754b456d581d93e8dda98c5bbf0711d6b838"
NATIVE_MANIFEST_SHA256 = "01fc7540c03c72113aeefea5187cbaae5d14f559e8b6dc90da8cc7aa9d6b4d08"
OWNED_PROTOCOL_SHA256 = "29f5abe77e7ed27aec6a63393411c7e9b0875cec7b1537d85ad5cb13ee9464f9"
SIZING_PROTOCOL_SHA256 = "de40c6ec9d865175dd6ecd9d59cb9af0bbc4a79d9bfa589dbc2fe406a0a23575"
PROFIT_PROTOCOL_SHA256 = "f622c1ce307adb7bb25fcb5f6a1726aab0b15cd664bd6c6fe764180c2afcbaa7"
ROTATION_PROTOCOL_SHA256 = "6b6b3a83a159e09d0df8d9407d169cd85bf1b800c6462644a3c30d6a06e91da2"
PARENT_V2_SHA256 = "8acfb0aadf570294fe0855a0ca49cc87dfaa093d728238474d7de031e6b14024"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decimal(value, name: str, *, positive=False) -> D:
    out = D(str(value))
    if not out.is_finite() or out < ZERO or (positive and out <= ZERO):
        raise ValueError(f"{name} must be finite and nonnegative")
    return out


@dataclass(frozen=True)
class RecoveryRung:
    """Explicit hypothesis anchored to the original confirmed entry price."""
    drop_fraction: D
    seed_multiple: D

    def __post_init__(self):
        object.__setattr__(self, "drop_fraction", decimal(self.drop_fraction, "drop", positive=True))
        object.__setattr__(self, "seed_multiple", decimal(self.seed_multiple, "multiple", positive=True))
        if self.drop_fraction >= ONE:
            raise ValueError("a drawdown threshold must be below 100%")


@dataclass(frozen=True)
class RecoveryLadder:
    name: str
    rungs: tuple[RecoveryRung, ...]
    hypothesis: bool = True

    def __post_init__(self):
        if not self.name or not self.rungs or not self.hypothesis:
            raise ValueError("only explicitly named research hypotheses are admitted")
        drops = tuple(r.drop_fraction for r in self.rungs)
        if tuple(sorted(set(drops))) != drops:
            raise ValueError("ladder thresholds must be distinct and increasing")


class LadderBook:
    """Immutable parent anchors; rejected orders never consume a rung."""
    def __init__(self, profiles: Mapping[str, RecoveryLadder]):
        self.profiles = dict(profiles)
        self.parents: dict[int, dict] = {}
        self.reservations: dict[int, dict] = {}

    def confirmed_entry(self, position_id: int, pair: str, price: D, all_in: D):
        if position_id in self.parents:
            raise ValueError("parent identities cannot be reused")
        self.parents[position_id] = {"pair": pair, "anchor": decimal(price, "price", positive=True),
                                     "seed": decimal(all_in, "entry cash", positive=True), "consumed": 0,
                                     "profile": self.profiles.get(pair)}

    def requested_budget(self, position_id: int, pair: str, completed_close: D):
        parent = self.parents.get(position_id)
        profile = None if parent is None else parent["profile"]
        if parent is None or parent["pair"] != pair or profile is None:
            return None, "UNKNOWN_LADDER_PROFILE"
        index = parent["consumed"]
        if index >= len(profile.rungs):
            return None, "LADDER_EXHAUSTED"
        rung = profile.rungs[index]
        close = decimal(completed_close, "close", positive=True)
        if close > parent["anchor"] * (ONE - rung.drop_fraction):
            return None, "LADDER_THRESHOLD_NOT_REACHED"
        return parent["seed"] * rung.seed_multiple, "LADDER_RUNG_ELIGIBLE"

    def reserve_recovery(self, order_id: int, position_id: int, pair: str, budget: D):
        parent = self.parents.get(position_id)
        if (order_id in self.reservations or parent is None or parent["pair"] != pair
                or parent["profile"] is None):
            raise ValueError("recovery reservation identity differs")
        if any(r["parent_id"] == position_id for r in self.reservations.values()):
            raise ValueError("one pending rung per parent is permitted")
        rung = parent["consumed"]
        if rung >= len(parent["profile"].rungs):
            raise ValueError("no unconsumed rung remains")
        requested = decimal(budget, "reservation", positive=True)
        maximum = parent["seed"] * parent["profile"].rungs[rung].seed_multiple
        if requested > maximum + D("1e-18"):
            raise ValueError("reservation exceeds the declared rung maximum")
        self.reservations[order_id] = {"parent_id": position_id, "pair": pair,
                                       "rung": rung, "maximum_cash": requested}

    def rejected_order(self, order_id: int):
        """Release an exact FOK rejection; consumption is unchanged."""
        self.reservations.pop(order_id, None)

    def confirmed_recovery(self, position_id: int, pair: str, quantity: D, cash_consumed: D,
                           *, order_id: int):
        if decimal(quantity, "quantity", positive=True) <= ZERO or decimal(cash_consumed, "cash", positive=True) <= ZERO:
            raise ValueError("positive confirmed recovery is required")
        parent = self.parents.get(position_id)
        if parent is None or parent["pair"] != pair or parent["profile"] is None:
            raise ValueError("recovery has no immutable ladder parent")
        reservation = self.reservations.get(order_id)
        if (reservation is None or reservation["parent_id"] != position_id
                or reservation["pair"] != pair or reservation["rung"] != parent["consumed"]
                or cash_consumed > reservation["maximum_cash"] + D("1e-18")):
            raise ValueError("confirmed recovery differs from its accepted rung reservation")
        if parent["consumed"] >= len(parent["profile"].rungs):
            raise ValueError("no unconsumed rung remains")
        parent["consumed"] += 1
        del self.reservations[order_id]


def replace_once(source: str, before: str, after: str):
    count = source.count(before)
    if count != 1:
        raise ValueError(f"research adapter boundary must occur exactly once: {count}")
    return source.replace(before, after), {"before": before, "after": after}


def build_campaign_body(native_source: str, *, selection=False, directional=False,
                        recovery_quality=False, ladder=False, entry_gate=False):
    """Apply auditable boundaries; retain the native accounting/exit controller."""
    source = native_source
    changes = []
    def change(before, after):
        nonlocal source
        source, receipt = replace_once(source, before, after)
        changes.append(receipt)
    # A passive end-of-frame observer does not alter native decisions.
    change("        pending = exit_orders + partial_orders + recovery_orders + entry_orders",
           "        pending = exit_orders + partial_orders + recovery_orders + entry_orders\n"
           "        dataset.observe_account(locals())")
    if selection:
        change("        candidates.sort(key=movement_rank)",
               "        candidates = dataset.rank_candidates(candidates, timestamp)")
    if directional:
        change("            and pair not in closed_this_frame\n",
               "            and pair not in closed_this_frame\n"
               "            and dataset.directional_entry_allowed(pair, index)\n")
    if entry_gate:
        change("            and pair not in pending_pairs\n",
               "            and pair not in pending_pairs\n"
               "            and dataset.entry_gate_allowed(index)\n")
    if recovery_quality:
        change("            proposal = CapitalProposal(\n",
               "            if not dataset.recovery_quality_allowed(pair, index):\n"
               "                continue\n"
               "            proposal = CapitalProposal(\n")
    if ladder:
        change("            if not dataset.recovery_enabled or bar.close >= average * (ONE - cfg.recovery_trigger) or spendable <= ZERO:\n",
               "            ladder_budget = dataset.ladder_budget(position, bar.close)\n"
               "            if not dataset.recovery_enabled or ladder_budget is None or spendable <= ZERO:\n")
        change("                recovery.planned_amount_quote,\n",
               "                recovery.planned_amount_quote,\n"
               "                ladder_budget / (ONE + fee_rate),\n")
        change("            recovery_orders.append(\n"
               "                next_order(\n"
               "                    timestamp,\n"
               "                    pair,\n"
               '                    "RECOVERY",\n'
               "                    budget=budget,\n"
               "                    position_id=position.position_id,\n"
               "                )\n"
               "            )",
               "            recovery_order = next_order(timestamp, pair, 'RECOVERY',\n"
               "                budget=budget, position_id=position.position_id)\n"
               "            dataset.note_recovery_order(recovery_order, position)\n"
               "            recovery_orders.append(recovery_order)")
        change("        rejection_counts[reason] += 1\n",
               "        rejection_counts[reason] += 1\n"
               "        dataset.note_rejection(order, reason)\n")
    return source, changes


def build_owned_body(source: str, *, ladder=False):
    """Bind current-owner funding before native mutations; retain actual fills.

    Conditional replacement proposals cannot claim an occupied future slot.
    A confirmed closure therefore admits refill at that frame's completed close
    and executes through the same next-open model as every other entry.
    """
    changes = []
    def change(before, after):
        nonlocal source
        source, receipt = replace_once(source, before, after)
        changes.append(receipt)
    if not ladder:
        change("        rejection_counts[reason] += 1\n",
               "        rejection_counts[reason] += 1\n"
               "        dataset.note_rejection(order, reason)\n")
    change("            if reserve_breached and not recycled_replacement:\n",
           "            if reserve_breached:\n")
    change("            if order.kind == \"ENTRY\":\n                if total < cfg.minimum_position_basis:\n",
           "            owner_allowed, owner_reason = dataset.owner_preflight(order, gross, fee, equity_before, cfg)\n"
           "            if not owner_allowed:\n"
           "                reject(index, order, owner_reason)\n"
           "                continue\n"
           "            if order.kind == \"ENTRY\":\n                if total < cfg.minimum_position_basis:\n")
    change("            dataset.record_buy(fills[-1], position, cash + total)",
           "            dataset.record_buy(fills[-1], position, cash + total, equity_before=equity_before)")
    change("                spendable / (ONE + fee_rate),\n",
           "                spendable / (ONE + fee_rate),\n"
           "                dataset.owner_recovery_available(equity) / (ONE + fee_rate),\n")
    native_recovery = ("            recovery_orders.append(\n"
        "                next_order(\n                    timestamp,\n                    pair,\n"
        '                    "RECOVERY",\n                    budget=budget,\n'
        "                    position_id=position.position_id,\n                )\n            )")
    ladder_recovery = ("            recovery_order = next_order(timestamp, pair, 'RECOVERY',\n"
        "                budget=budget, position_id=position.position_id)\n"
        "            dataset.note_recovery_order(recovery_order, position)\n"
        "            recovery_orders.append(recovery_order)")
    change(ladder_recovery if ladder else native_recovery,
           "            recovery_order = next_order(timestamp, pair, 'RECOVERY',\n"
           "                budget=budget, position_id=position.position_id)\n"
           "            if not dataset.owner_authorize(recovery_order, equity, cfg):\n"
           "                continue\n"
           + ("            dataset.note_recovery_order(recovery_order, position)\n" if ladder else "")
           + "            recovery_orders.append(recovery_order)")
    # Local ticket is frozen at this proposal time. Native held quantities,
    # lot basis and recovery ladder seeds are never resized by this policy.
    change("        entry_gross = cfg.entry_budget / (ONE + fee_rate)",
           "        entry_ticket = dataset.owner_entry_ticket(equity, cfg)\n"
           "        entry_gross = entry_ticket / (ONE + fee_rate)")
    change("            for pair in bars\n", "            for pair in bars\n            if entry_ticket > ZERO\n")
    # This inserts a second if only at the comprehension's original first if.
    change("            if pair not in positions\n", "            and pair not in positions\n")
    change("        while shortfall > 0 and candidates and spendable >= cfg.entry_budget:\n"
           "            pair = candidates.pop(0)\n"
           "            entry_orders.append(\n"
           '                next_order(timestamp, pair, "ENTRY", budget=cfg.entry_budget)\n'
           "            )\n"
           "            pending_pairs.add(pair)\n"
           "            spendable -= cfg.entry_budget\n"
           "            shortfall -= 1",
           "        while shortfall > 0 and candidates and entry_ticket > ZERO and spendable >= entry_ticket:\n"
           "            pair = candidates.pop(0)\n"
           "            entry_order = next_order(timestamp, pair, 'ENTRY', budget=entry_ticket)\n"
           "            if not dataset.owner_authorize(entry_order, equity, cfg):\n"
           "                break\n"
           "            entry_orders.append(entry_order)\n"
           "            pending_pairs.add(pair)\n"
           "            spendable -= entry_ticket\n"
           "            shortfall -= 1")
    change("                if spendable < cfg.entry_budget\n", "                if spendable < entry_ticket or entry_ticket <= ZERO\n")
    start = "        for exit_order in exit_orders:\n            assigned = 0\n"
    end = '                rejection_counts["no_distinct_replacement_candidate"] += 1'
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError("conditional replacement source boundary differs")
    before = source[source.index(start):source.index(end) + len(end)]
    change(before,
           "        # Owner control never admits a slot before its actual closure.\n"
           "        dataset.research_counts['deferred_until_confirmed_slot_closure'] += len(exit_orders)")
    return source, changes


def build_profit_body(source: str, *, native_dttp=False):
    if not native_dttp:
        return source, []
    protected, receipt = replace_once(source,
        "            average = position.basis / position.quantity\n",
        "            if exit_plan.state.profit_floor_reached_at is not None:\n"
        "                dataset.research_counts['armed_profit_floor_blocks_recovery'] += 1\n"
        "                continue\n"
        "            average = position.basis / position.quantity\n")
    return protected, [receipt]


def build_rotation_body(source: str, *, rotation=None):
    """Explicit native source boundaries; disabled control is byte-identical v2.

    An immutable research tag exempts only its exact whole sale from the hard
    profit floor. Ordinary whole/partial exits retain the inherited floor.
    """
    if rotation is None:
        return source, []
    if rotation not in {"stale", "fee_hurdle"}:
        raise ValueError("rotation mode was not frozen")
    changes = []
    def change(before, after):
        nonlocal source
        source, receipt = replace_once(source, before, after)
        changes.append(receipt)
    change('        due = sorted(pending, key=lambda order: {"EXIT":0,"PARTIAL_RECYCLE":1}.get(order.kind,2))',
           '        due = sorted(pending, key=dataset.rotation_execution_priority)')
    change('                gross = position.quantity * price\n',
           '                gross = position.quantity * price\n'
           '                rotation_allowed, rotation_reason = dataset.rotation_sale_preflight(order, position, index, gross, cfg)\n'
           '                if not rotation_allowed:\n'
           '                    reject(index, order, rotation_reason)\n'
           '                    continue\n')
    change('                if not dataset.floor_passes(position.basis, proceeds):\n',
           '                if not dataset.rotation_floor_bypass(order, position) and not dataset.floor_passes(position.basis, proceeds):\n')
    change('        dataset.roll_volume(index, volume_24h, bars)\n',
           '        dataset.roll_volume(index, volume_24h, bars)\n'
           '        dataset.rotation_update(index, timestamp)\n')
    change('        exit_orders: list[FleetOrder] = []\n',
           '        exit_orders: list[FleetOrder] = []\n'
           '        rotation_orders: list[FleetOrder] = []\n')
    change('            average = position.basis / position.quantity\n',
           '            rotation_offer = dataset.rotation_offer(position, bar.close, timestamp, index, cfg,\n'
           '                positions, bars, fingerprints, volume_24h, closed_this_frame, pending_pairs, exit_plan)\n'
           '            if rotation_offer is not None:\n'
           '                rotation_order = next_order(timestamp, pair, "EXIT", position_id=position.position_id)\n'
           '                dataset.note_rotation_order(rotation_order, position, rotation_offer)\n'
           '                rotation_orders.append(rotation_order)\n'
           '                pending_pairs.add(pair)\n'
           '                continue\n'
           '            average = position.basis / position.quantity\n')
    change('        candidates.sort(key=movement_rank)\n',
           '        candidates.sort(key=movement_rank)\n'
           '        candidates = dataset.rotation_replacement_candidates(candidates, timestamp)\n')
    change('            if not dataset.owner_authorize(entry_order, equity, cfg):\n'
           '                break\n'
           '            entry_orders.append(entry_order)\n',
           '            if not dataset.owner_authorize(entry_order, equity, cfg):\n'
           '                dataset.rotation_replacement_denied(entry_order, "OWNER_ADMISSION_DENIED")\n'
           '                break\n'
           '            dataset.rotation_note_entry_order(entry_order)\n'
           '            entry_orders.append(entry_order)\n')
    change('        pending = exit_orders + partial_orders + recovery_orders + entry_orders\n',
           '        pending = exit_orders + partial_orders + rotation_orders + recovery_orders + entry_orders\n')
    change('        dataset.observe_account(locals())\n',
           '        dataset.rotation_finish_frame(timestamp)\n'
           '        dataset.observe_account(locals())\n')
    return source, changes


class FrameRankingCache:
    """Cache only the native ranking of a single completed decision frame.

    Position, pending-pair, closed-pair, alias and liquidity eligibility remain
    live filters outside this cache. Its tuple cannot be consumed or reordered
    by another parent's proposal. No future row participates in its key.
    """
    def __init__(self):
        self.index = -1
        self.pairs = ()
        self.builds = 0

    def ordered(self, index, bars, rank_key):
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index < self.index:
            raise ValueError("candidate ranking must use a monotonic completed frame")
        if index != self.index:
            self.pairs = tuple(sorted(bars, key=rank_key))
            self.index = index
            self.builds += 1
        return self.pairs


class RotationHooks:
    """Receipt-bound sale tags and unfunded replacement intentions.

    This object never changes native cash, quantities, owner balances or slots.
    Confirmation requires the exact source tag and complete parent sale.
    """
    def __init__(self, policy, *, mode, pairs, rows, emit=None):
        if mode not in {"stale", "fee_hurdle"}:
            raise ValueError("rotation mode is not declared")
        self.policy = policy
        self.mode = mode
        self.rows = rows
        self.tracker = policy.RotationTracker(pairs)
        self.emit = emit or (lambda event: None)
        self.tags = {}
        self.closed = []
        self.entry_links = {}
        self.candidate_claims = set()
        self.counts = Counter()
        self.current_index = -1
        self.current_at = None

    def event(self, name, **payload):
        self.emit({"event": name, **payload})

    def update(self, index, timestamp):
        if index != self.current_index + 1:
            raise ValueError("rotation evidence requires consecutive completed frames")
        self.tracker.update(timestamp, self.rows[index])
        self.current_index = index
        self.current_at = timestamp
        self.candidate_claims.clear()

    def offer(self, position, close, *, timestamp, index, occupied, ranked_candidates,
              armed_floor=False):
        if timestamp != self.current_at or index != self.current_index:
            raise ValueError("rotation proposal differs from current completed evidence")
        if armed_floor:
            self.counts["ARMED_PROFIT_FLOOR_VETO"] += 1
            return None
        if any(tag["status"] == "SUBMITTED" and tag["parent_id"] == position.position_id
               for tag in self.tags.values()):
            self.counts["PARENT_ALREADY_CLAIMED"] += 1
            return None
        if self.mode == "stale":
            allowed, diagnostic = self.tracker.stale_gate(position.pair, position.opened_at)
            self.counts[diagnostic.get("reason", "STALE_ALLOWED" if allowed else "STALE_DENIED")] += 1
            if not allowed:
                return None
            return {"mode": self.mode, "evidence_cutoff": timestamp,
                    "decision_index": index, "candidate": None, "trigger": diagnostic}
        if occupied != 50:
            self.counts["FEE_GATE_REQUIRES_50_OCCUPIED"] += 1
            return None
        old_mean, old_diagnostic = self.tracker.daily_forecast(position.pair)
        if old_mean is None:
            self.counts["OLD_FORECAST_UNAVAILABLE"] += 1
            return None
        for candidate in ranked_candidates:
            if candidate in self.candidate_claims:
                continue
            new_mean, new_diagnostic = self.tracker.daily_forecast(candidate)
            if new_mean is None:
                continue
            allowed, hurdle = self.policy.fee_hurdle(position.quantity, close, old_mean, new_mean)
            if not allowed:
                continue
            self.candidate_claims.add(candidate)
            return {"mode": self.mode, "evidence_cutoff": timestamp, "decision_index": index,
                "candidate": candidate, "old_forecast": old_diagnostic,
                "candidate_forecast": new_diagnostic, "trigger": hurdle,
                "replacement_budget_forecast_only": "10.25", "funded": False}
        self.counts["NO_POSITIVE_NATIVE_ELIGIBLE_FEE_CANDIDATE"] += 1
        return None

    def submit(self, order, position, offer):
        if (order.kind != "EXIT" or order.position_id != position.position_id
                or order.pair != position.pair or order.order_id in self.tags
                or order.depends_on_order_id is not None or order.budget != ZERO
                or offer["decision_index"] != self.current_index
                or offer["evidence_cutoff"] != self.current_at):
            raise ValueError("rotation tag must bind the exact current whole-parent sale")
        tag = {"intent_id": f"rotation:{order.order_id}", "order": asdict(order),
            "parent_id": position.position_id, "pair": position.pair,
            "quantity": position.quantity, "basis": position.basis,
            "opened_at": position.opened_at, "status": "SUBMITTED", **offer}
        self.tags[order.order_id] = tag
        self.counts["attempted_rotations"] += 1
        self.event("ROTATION_SUBMIT", **tag, profit_floor_bypass_for_this_order_only=True,
                   anticipated_cash_released=False, anticipated_slot_released=False)

    def priority(self, order):
        if order.order_id in self.tags:
            return 2
        return {"EXIT": 0, "PARTIAL_RECYCLE": 1}.get(order.kind, 3)

    def preflight(self, order, position, index, gross, cfg):
        tag = self.tags.get(order.order_id)
        if tag is None:
            return True, "ORDINARY_EXIT_UNCHANGED"
        if (tag["status"] != "SUBMITTED" or asdict(order) != tag["order"]
                or position.position_id != tag["parent_id"] or position.pair != tag["pair"]
                or position.quantity != tag["quantity"] or position.basis != tag["basis"]):
            return False, "ROTATION_IMMUTABLE_PARENT_ORDER_MISMATCH"
        if index != tag["decision_index"] + 1:
            return False, "ROTATION_NOT_IMMEDIATE_NEXT_BAR"
        if gross < cfg.simulated_min_order_quote or position.quantity < cfg.simulated_base_step:
            return False, "ROTATION_EXECUTION_MINIMUM"
        return True, "EXACT_TAGGED_RESEARCH_WHOLE_SALE"

    def bypass(self, order, position):
        tag = self.tags.get(order.order_id)
        return bool(tag is not None and tag["status"] == "SUBMITTED"
            and asdict(order) == tag["order"] and position.position_id == tag["parent_id"]
            and position.pair == tag["pair"] and position.quantity == tag["quantity"]
            and position.basis == tag["basis"])

    def reject(self, order, reason):
        tag = self.tags.get(order.order_id)
        if tag is not None:
            if tag["status"] != "SUBMITTED" or asdict(order) != tag["order"]:
                raise ValueError("rotation rejection is repeated or differs from immutable order")
            tag["status"] = "REJECTED"
            tag["terminal_reason"] = reason
            self.counts["rejected_rotations"] += 1
            self.counts[f"rotation_rejected_{reason}"] += 1
            self.event("ROTATION_REJECTED", intent_id=tag["intent_id"], order=asdict(order),
                reason=reason, parent_cash_slot_released=False)
        link = self.entry_links.pop(order.order_id, None)
        if link is not None:
            link["replacement_status"] = "REJECTED"
            link["replacement_terminal_reason"] = reason
            self.counts["replacement_rejected"] += 1
            self.event("ROTATION_REPLACEMENT_REJECTED", intent_id=link["intent_id"],
                order=asdict(order), reason=reason)

    def confirm_sale(self, fill, *, owner_before, owner_after, parked_before, parked_after):
        tag = self.tags.get(fill.order_id)
        if tag is None:
            return
        if (tag["status"] != "SUBMITTED" or fill.kind != "EXIT"
                or fill.position_id != tag["parent_id"] or fill.pair != tag["pair"]
                or fill.quantity != tag["quantity"] or fill.basis_released != tag["basis"]
                or fill.dependency_order_id is not None):
            raise ValueError("rotation confirmation requires an exact complete parent sale")
        if parked_before != parked_after:
            raise ArithmeticError("tagged whole sale must not release unrelated parked receipts")
        from fractions import Fraction
        released = {key: str(Fraction(owner_after[key]) - Fraction(owner_before[key]))
                    for key in owner_before}
        if sum((Fraction(value) for value in released.values()), Fraction(0)) != (
                Fraction(fill.gross_quote) - Fraction(fill.fee_quote)):
            raise ArithmeticError("receipt-scoped owner release differs from confirmed proceeds")
        tag["status"] = "CONFIRMED"
        tag["sale_receipt_id"] = f"sale:{fill.order_id}"
        tag["closed_at"] = fill.timestamp
        self.closed.append(tag)
        self.counts["confirmed_rotations"] += 1
        self.event("ROTATION_CONFIRMED", intent_id=tag["intent_id"], fill=asdict(fill),
            sale_receipt_id=tag["sale_receipt_id"], exact_owned_proceeds_released=released,
            unrelated_parked_proceeds_released=False, full_signed_realized_pnl=fill.realized_pnl)

    def replacement_candidates(self, candidates, timestamp):
        ordered = list(candidates)
        for tag in self.closed:
            if tag.get("replacement_status") is not None:
                continue
            if tag["closed_at"] != timestamp:
                raise ValueError("replacement proposal must use confirmed-closure current frame")
            candidate = tag["candidate"]
            if candidate is None:
                candidate = next((pair for pair in ordered if not any(
                    other.get("replacement_status") is None and other.get("replacement_candidate") == pair
                    for other in self.closed)), None)
            if candidate is None or candidate not in ordered:
                tag["replacement_status"] = "EXPIRED"
                self.counts["replacement_intention_expired"] += 1
                self.event("ROTATION_REPLACEMENT_EXPIRED", intent_id=tag["intent_id"],
                    candidate=tag["candidate"], reason="CANDIDATE_CURRENT_NATIVE_GUARDS_FAILED")
                continue
            tag["replacement_candidate"] = candidate
            # Reverse iteration below is unnecessary: each eligible intention
            # is explicitly linked by candidate rather than list position.
        prioritized = [tag["replacement_candidate"] for tag in self.closed
            if tag.get("replacement_status") is None and "replacement_candidate" in tag]
        return prioritized + [pair for pair in ordered if pair not in prioritized]

    def note_entry(self, order):
        link = next((tag for tag in self.closed if tag.get("replacement_status") is None
            and tag.get("replacement_candidate") == order.pair), None)
        if link is None:
            return
        if order.kind != "ENTRY" or order.depends_on_order_id is not None:
            raise ValueError("rotation refill may not borrow future closure")
        link["replacement_status"] = "SUBMITTED"
        link["replacement_order_id"] = order.order_id
        self.entry_links[order.order_id] = link
        self.event("ROTATION_REPLACEMENT_SUBMIT", intent_id=link["intent_id"],
            sale_receipt_id=link["sale_receipt_id"], order=asdict(order),
            closed_at=link["closed_at"], funded_after_confirmed_close=True)

    def replacement_denied(self, order, reason):
        link = next((tag for tag in self.closed if tag.get("replacement_status") is None
            and tag.get("replacement_candidate") == order.pair), None)
        if link is not None:
            link["replacement_status"] = "EXPIRED"
            self.counts["replacement_intention_expired"] += 1
            self.event("ROTATION_REPLACEMENT_EXPIRED", intent_id=link["intent_id"],
                candidate=order.pair, reason=reason)

    def confirm_entry(self, fill):
        link = self.entry_links.get(fill.order_id)
        if link is None:
            return
        if fill.kind != "ENTRY" or fill.pair != link["replacement_candidate"] or fill.timestamp <= link["closed_at"]:
            raise ValueError("replacement fill must follow confirmed exact closure")
        del self.entry_links[fill.order_id]
        link["replacement_status"] = "CONFIRMED"
        self.counts["replacement_confirmed"] += 1
        self.event("ROTATION_REPLACEMENT_CONFIRMED", intent_id=link["intent_id"],
            sale_receipt_id=link["sale_receipt_id"], fill=asdict(fill))

    def finish_frame(self, timestamp):
        for tag in self.closed:
            if tag.get("replacement_status") is None:
                tag["replacement_status"] = "EXPIRED"
                self.counts["replacement_intention_expired"] += 1
                self.event("ROTATION_REPLACEMENT_EXPIRED", intent_id=tag["intent_id"],
                    candidate=tag.get("replacement_candidate", tag["candidate"]),
                    reason="NO_CURRENT_FUNDED_SLOT_PROPOSAL", timestamp=timestamp)


class OwnedCashHooks:
    """Separate exact owner journal; native physical cash is mutated once."""
    def __init__(self, module, *, initial_cash=D(2000), redeploy=False, emit=None):
        self.module = module
        self.ledger = module.OwnedCashLedger(initial_cash=initial_cash)
        self.redeploy = redeploy
        self.emit = emit or (lambda event: None)
        self.counts = Counter()

    def compact(self):
        book = self.ledger
        return {"cash_total": str(book.cash_total),
            "cash_by_owner": {owner.value: str(value) for owner, value in book.cash_by_owner.items()},
            "held_by_owner": {owner.value: str(book.held(owner)) for owner in book.cash_by_owner},
            "parked_cash": str(book.parked_cash_total), "realized_pnl": str(book.realized_pnl),
            "fees_total": str(book.fees_total),
            "remaining_basis": str(sum((lot.basis for lot in book.lots.values()), ZERO))}

    def record(self, event, **payload):
        self.emit({"event": event, **payload, "after": self.compact()})

    def authorize(self, order, *, equity, cfg):
        if order.kind not in {"ENTRY", "RECOVERY"} or order.depends_on_order_id is not None:
            raise ValueError("owner admission requires an independent current-slot buy proposal")
        fee_rate = cfg.fee_bps / BPS
        gross = order.budget / (ONE + fee_rate)
        fee = gross * fee_rate
        all_in = self.module.all_in_quote(gross, fee)
        if order.kind == "ENTRY" and all_in > equity * cfg.pair_cap_fraction:
            self.counts["ENTRY_PAIR_CAP"] += 1
            return False
        if gross < cfg.simulated_min_order_quote or (order.kind == "ENTRY" and gross + fee < cfg.minimum_position_basis):
            self.counts["NATIVE_MINIMUM_ADMISSION"] += 1
            return False
        proposal = f"buy:{order.order_id}"
        parent = f"pending-entry:{order.order_id}" if order.kind == "ENTRY" else str(order.position_id)
        try:
            reservation = self.ledger.reserve_buy(proposal, kind=order.kind, parent_id=parent,
                pair=order.pair, all_in_limit=all_in, equity_before=equity)
        except self.module.OwnerFundingError as exc:
            self.counts[exc.reason] += 1
            self.record("DENIED", proposal_id=proposal, order=asdict(order), reason=exc.reason,
                        equity_before=equity, requested_all_in=all_in)
            return False
        self.record("RESERVE", proposal_id=proposal, order=asdict(order),
                    reservation=asdict(reservation), equity_before=equity)
        return True

    def preflight(self, order, gross, fee, equity, cfg):
        if order.kind == "ENTRY" and self.module.all_in_quote(gross, fee) > equity * cfg.pair_cap_fraction:
            return False, "ENTRY_PAIR_CAP"
        return self.ledger.can_confirm_buy(f"buy:{order.order_id}", gross_quote=gross,
                                          entry_fee=fee, equity_before=equity)

    def reject(self, order, reason):
        proposal = f"buy:{order.order_id}"
        if order.kind in {"ENTRY", "RECOVERY"} and proposal in self.ledger.reservations:
            released = self.ledger.confirm_terminal(f"terminal-rejected:{order.order_id}", proposal,
                terminal_status="REJECTED", exact_zero_remaining=True)
            self.record("TERMINAL", proposal_id=proposal, proof_id=f"terminal-rejected:{order.order_id}",
                        status="REJECTED", reason=reason, released=released)

    def buy(self, fill, *, equity):
        proposal = f"buy:{fill.order_id}"
        self.ledger.confirm_buy(f"fill:{fill.order_id}", proposal, lot_id=f"lot:{fill.order_id}",
            quantity=fill.quantity, gross_quote=fill.gross_quote, entry_fee=fill.fee_quote,
            confirmed_parent_id=str(fill.position_id), equity_before=equity)
        self.record("BUY", receipt_id=f"fill:{fill.order_id}", proposal_id=proposal,
                    fill=asdict(fill), equity_before=equity)
        released = self.ledger.confirm_terminal(f"terminal-filled:{fill.order_id}", proposal,
            terminal_status="FILLED", exact_zero_remaining=True)
        self.record("TERMINAL", proposal_id=proposal, proof_id=f"terminal-filled:{fill.order_id}",
                    status="FILLED", released=released)

    def sale(self, fill, native_lots, *, partial):
        selected = []
        for native_lot in native_lots:
            owned = self.ledger.lots[native_lot.lot_id]
            if (owned.parent_id != str(fill.position_id) or owned.pair != fill.pair
                    or owned.quantity != native_lot.base_remaining
                    or owned.gross_basis != native_lot.cost_basis_remaining_quote
                    or owned.entry_fees != native_lot.entry_fees_remaining_quote):
                raise ArithmeticError("native disposal differs from exact source-owned lot")
            selected.append(owned)
        if abs(sum((lot.quantity for lot in selected), ZERO) - fill.quantity) > D("1e-18"):
            raise ArithmeticError("selected source-owned quantity differs from confirmed sale")
        if abs(sum((lot.basis for lot in selected), ZERO) - fill.basis_released) > D("1e-18"):
            raise ArithmeticError("selected source-owned basis differs from confirmed sale")
        allocations = self.module.allocate_sale_proceeds(selected, gross_proceeds=fill.gross_quote,
                                                        exit_fee=fill.fee_quote)
        receipt = f"sale:{fill.order_id}"
        self.ledger.settle_sale(receipt, allocations, sale_kind="PARTIAL" if partial else "WHOLE",
            deployable=not partial, fill_gross_proceeds=fill.gross_quote, fill_exit_fee=fill.fee_quote)
        self.record("SALE", receipt_id=receipt, fill=asdict(fill),
                    sale_kind="PARTIAL" if partial else "WHOLE", deployable=not partial,
                    allocations=[asdict(item) for item in allocations])
        if partial and self.redeploy:
            released = self.ledger.release_confirmed_partial(f"release:{fill.order_id}", receipt,
                                                             declared_redeploy=True)
            self.record("RELEASE_PARTIAL", proof_id=f"release:{fill.order_id}",
                        sale_receipt_id=receipt, released={owner.value: value for owner, value in released.items()})

    def observe(self, state):
        book = self.ledger
        book.assert_invariants()
        native = {"cash_total": state["cash"], "realized_pnl": state["realized"],
                  "fees_total": state["fees"], "remaining_basis": state["committed"]}
        owned = {"cash_total": book.cash_total, "realized_pnl": book.realized_pnl,
                 "fees_total": book.fees_total,
                 "remaining_basis": sum((lot.basis for lot in book.lots.values()), ZERO)}
        for name, expected in native.items():
            if abs(owned[name] - expected) > D("1e-18"):
                raise ArithmeticError(f"parallel source-owned/native {name} differs")
        parent_owners = {}
        for lot in book.lots.values():
            key = (lot.parent_id, lot.pair)
            quantity, basis = parent_owners.get(key, (ZERO, ZERO))
            parent_owners[key] = (quantity + lot.quantity, basis + lot.basis)
        native_parents = {(str(position.position_id), pair): position
                          for pair, position in state["positions"].items()}
        if set(parent_owners) != set(native_parents):
            raise ArithmeticError("source-owned/native parent and pair scopes differ")
        for key, (quantity, basis) in parent_owners.items():
            if (abs(quantity - native_parents[key].quantity) > D("1e-18")
                    or abs(basis - native_parents[key].basis) > D("1e-18")):
                raise ArithmeticError("source-owned/native per-parent quantity or basis differs")
        actual_pending = {f"buy:{order.order_id}" for order in state["pending"]
                          if order.kind in {"ENTRY", "RECOVERY"}}
        held_pending = {key for key, reservation in book.reservations.items() if reservation.remaining > ZERO}
        if actual_pending != held_pending:
            raise ArithmeticError("pending native orders differ from exact owner locks")
        return self.compact()


def verify_reference(prepared: Path):
    """Verify the entire original six-month tape before loading outcomes."""
    import numpy as np
    prepared = Path(prepared)
    identities = {"metadata_sha256": sha256(prepared / "metadata.json"),
                  "array_sha256": sha256(prepared / "ohlcv.npy")}
    if identities != {"metadata_sha256": REFERENCE_METADATA_SHA256,
                      "array_sha256": REFERENCE_ARRAY_SHA256}:
        raise ValueError("the original six-month input identity differs")
    meta = json.loads((prepared / "metadata.json").read_text())
    rows = np.load(prepared / "ohlcv.npy", mmap_mode="r")
    if rows.shape != (52128, 619, 6) or rows.dtype != np.dtype("float64"):
        raise ValueError("the original fixed-universe geometry differs")
    if (meta["start"], meta["end"], meta["interval_seconds"]) != (1767225600, 1782864000, 300):
        raise ValueError("unexpected replay period")
    return {**identities, "shape": list(rows.shape), "observed_only": True, "holdout_accessed": False}


def verify_native(core: Path, manifest: Path, adapter: Path, bridge: Path):
    if sha256(manifest) != NATIVE_MANIFEST_SHA256:
        raise ValueError("archived prerequisite manifest identity differs")
    inventory = json.loads(Path(manifest).read_text())["external_native_core"]["actually_imported_modules"]
    records = []
    for record in inventory:
        path = Path(core) / Path(record["path"]).name
        actual = sha256(path)
        if actual != record["sha256"] or path.stat().st_size != record["bytes"]:
            raise ValueError(f"preserved native source differs: {path.name}")
        records.append({"file": path.name, "sha256": actual})
    if sha256(adapter) != ADAPTER_SHA256 or sha256(bridge) != BRIDGE_SHA256:
        raise ValueError("preserved research bridge differs")
    if len(records) != 23:
        raise ValueError("all 23 imported preserved native modules are required")
    return records


def load_native(core: Path, adapter: Path, bridge: Path, native_inventory=None):
    """Import only after verify_native; each CLI invocation is a fresh process."""
    if "spectra_core" in sys.modules or "sparse_adapter" in sys.modules:
        raise RuntimeError("native replay requires a fresh isolated interpreter")
    sys.path.insert(0, str(Path(core).parent.resolve()))
    spec = importlib.util.spec_from_file_location("sparse_adapter", Path(adapter))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    expected = {record["file"]: record["sha256"] for record in native_inventory or ()}
    for name, imported in tuple(sys.modules.items()):
        if name == "spectra_core" or name.startswith("spectra_core."):
            actual_file = Path(imported.__file__).resolve()
            if actual_file.parent != Path(core).resolve():
                raise ValueError(f"native module imported outside the verified source tree: {name}")
            if expected and (actual_file.name not in expected
                             or sha256(actual_file) != expected[actual_file.name]):
                raise ValueError(f"loaded native module differs from its verified bytes: {name}")
    # The import uses the explicit parent path first. Later source references
    # are bound to these same verified files, rather than the obsolete workspace.
    module.ARCHIVE_SOURCE = Path(core).parent.resolve()
    spec = importlib.util.spec_from_file_location("spectra_profitability_native_bridge", Path(bridge))
    bridge_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bridge_module
    spec.loader.exec_module(bridge_module)
    if hashlib.sha256(bridge_module.GENERATED_SOURCE.encode()).hexdigest() != GENERATED_NATIVE_SHA256:
        raise ValueError("generated preserved native financial body differs")
    bridge_module.CASES["hard_174_park"] = dict(whole_target_bps="174", partial_target_bps="174",
        hard_target=True, execution_floor_bps="174", park_proceeds=True)
    return bridge_module


def causal_anchor_allowed(rows, pair_index: int, index: int):
    """Entry evidence at this completed close; no execution-bar information."""
    import numpy as np
    if index < 288:
        return False
    values = rows[[index, index - 12, index - 288], pair_index, :]
    return bool(np.isfinite(values).all() and (values[:, :4] > 0).all()
                and values[0, 3] > values[1, 3] and values[0, 3] > values[2, 3])


def causal_recovery_allowed(rows, pair_index: int, index: int):
    """A frozen, exploratory six-bar improvement gate; no profitability claim."""
    import numpy as np
    if index < 5:
        return False
    values = rows[index - 5:index + 1, pair_index, :]
    return bool(np.isfinite(values).all() and (values[:, :4] > 0).all()
                and values[-1, 3] > values[-4, 3]
                and values[-3:, 2].min() > values[:3, 2].min())


def validate_declaration(protocol, *, expected_sha256: str, protocol_path: Path, arm: str):
    if sha256(protocol_path) != expected_sha256:
        raise ValueError("protocol differs from the root freeze acknowledgment")
    if protocol.get("research_only") is not True or protocol.get("production_activation") is not False:
        raise ValueError("research-only authority is required")
    if protocol.get("holdout_access") is not False:
        raise ValueError("sealed holdout access must be disabled")
    arms = {a["name"]: a for a in protocol["arms"]}
    if len(arms) != len(protocol["arms"]) or arm not in arms:
        raise ValueError("arm is not uniquely predeclared")
    return arms[arm]


def resolve_parent_arm(protocol, args):
    """Only exact root-frozen supplements extend the original arm registry."""
    # Reuse authority checks even when the selected name lives in a supplement.
    validate_declaration(protocol, expected_sha256=args.protocol_sha256,
        protocol_path=Path(args.protocol), arm=protocol["arms"][0]["name"])
    registry = {item["name"]: dict(item) for item in protocol["arms"]}
    supplements = {}
    for label, attribute, expected in (
            ("owned", "owned_protocol", OWNED_PROTOCOL_SHA256),
            ("sizing", "sizing_protocol", SIZING_PROTOCOL_SHA256),
            ("profit_policy", "profit_protocol", PROFIT_PROTOCOL_SHA256)):
        path = getattr(args, attribute, None)
        if path is None:
            continue
        if sha256(path) != expected:
            raise ValueError(f"{label} supplement differs from root freeze")
        supplement = json.loads(Path(path).read_text())
        if (supplement.get("parent_protocol_sha256") != args.protocol_sha256
                or supplement.get("research_only") is not True
                or supplement.get("production_activation") is not False
                or supplement.get("holdout_access") is not False):
            raise ValueError(f"{label} supplement lacks exact parent/research authority")
        supplements[label] = {"sha256": expected, "declaration": supplement}
        if label == "owned":
            for name in supplement["existing_arms_using_matched_owner_control"]:
                registry[name]["owned_cash"] = True
                registry[name].pop("implementation_gate", None)
        for item in supplement.get("additional_arms", supplement.get("arms", [])):
            if item["name"] in registry:
                raise ValueError("supplement may not replace an earlier arm")
            registry[item["name"]] = dict(item)
            if label == "sizing":
                registry[item["name"]]["productive_sizing"] = True
            if label == "profit_policy":
                registry[item["name"]]["native_dttp"] = True
    if args.arm not in registry:
        raise ValueError("selected arm is not predeclared in a frozen protocol")
    selected = registry[args.arm]
    if selected.get("owned_cash") and "owned" not in supplements:
        raise ValueError("owner cash authority supplement is required")
    if selected.get("productive_sizing") and "sizing" not in supplements:
        raise ValueError("productive sizing authority supplement is required")
    return selected, supplements


def resolve_arm(protocol, args):
    """Only this exact supplement can replace the two original gated arms."""
    if sha256(Path(__file__).with_name("owned_profitability_campaign_v2.py")) != PARENT_V2_SHA256:
        raise ValueError("admitted immutable parent v2 differs")
    path = Path(args.rotation_protocol)
    if sha256(path) != ROTATION_PROTOCOL_SHA256:
        raise ValueError("rotation supplement differs from root freeze")
    rotation = json.loads(path.read_text())
    if (rotation.get("original_protocol_sha256") != args.protocol_sha256
            or rotation.get("owned_protocol_sha256") != OWNED_PROTOCOL_SHA256
            or rotation.get("parent_adapter_sha256") != PARENT_V2_SHA256
            or rotation.get("research_only") is not True
            or rotation.get("production_activation") is not False
            or rotation.get("holdout_access") is not False):
        raise ValueError("rotation supplement lacks exact parent/research authority")
    registry = {item["name"]: item for item in rotation["arms"]}
    if len(registry) != 3 or len(registry) != len(rotation["arms"]) or args.arm not in registry:
        raise ValueError("v3 only admits three frozen rotation/control arms")
    parent_args = argparse.Namespace(**vars(args))
    parent_args.arm = "owned_control_174"
    _, supplements = resolve_parent_arm(protocol, parent_args)
    if "owned" not in supplements:
        raise ValueError("exact ownership authority is required")
    supplements["rotation"] = {"sha256": ROTATION_PROTOCOL_SHA256, "declaration": rotation}
    return dict(registry[args.arm]), supplements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--native-core", type=Path, required=True)
    parser.add_argument("--native-manifest", type=Path, required=True)
    parser.add_argument("--native-adapter", type=Path, required=True)
    parser.add_argument("--native-bridge", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument("--owned-protocol", type=Path)
    parser.add_argument("--sizing-protocol", type=Path)
    parser.add_argument("--profit-protocol", type=Path)
    parser.add_argument("--rotation-protocol", type=Path, required=True)
    parser.add_argument("--owner-adapter-admission", type=Path)
    parser.add_argument("--owner-adapter-admission-sha256")
    parser.add_argument("--matched-owned-control-receipt", type=Path)
    parser.add_argument("--matched-owned-control-receipt-sha256")
    parser.add_argument("--arm", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_campaign(args)


def run_campaign(args):
    protocol = json.loads(Path(args.protocol).read_text())
    arm, supplements = resolve_arm(protocol, args)
    owned = bool(arm.get("owned_cash"))
    blocked = arm.get("blocked")
    if arm.get("redeploy_confirmed_partial") and not owned:
        blocked = "Exact productive/VCAP cash-owner integration is unavailable; native shared-cash reuse is not permitted as an owner-preserving substitute."
    if arm.get("ladder_assignment") and not owned:
        blocked = "The new ladder's exact productive/VCAP cash-owner authorization and protected pending locks require a separately validated owner adapter. Native shared-cash authorization is insufficient."
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("refusing to overwrite earlier campaign evidence")
    output.mkdir(parents=True, exist_ok=True)
    if blocked:
        (output / "blocked.json").write_text(json.dumps({"arm": args.arm, "status": "BLOCKED",
            "completed_strategy_evaluation": False, "reason": blocked,
            "protocol_sha256": args.protocol_sha256}, indent=2) + "\n")
        raise RuntimeError(blocked)
    admitted_engine_sha256 = sha256(Path(__file__))
    policy_source_paths = {"owned_cash": Path(__file__).with_name("owned_cash.py"),
        "module_coordination": Path(__file__).with_name("module_coordination.py"),
        "productive_sizing": Path(__file__).with_name("productive_sizing.py"),
        "opportunity_selector": Path(__file__).with_name("opportunity_selector.py"),
        "market_regime": Path(__file__).with_name("market_regime.py"),
        "rotation_policy": Path(__file__).with_name("rotation_policy.py"),
        "parent_v2": Path(__file__).with_name("owned_profitability_campaign_v2.py")}
    policy_sources = {name: sha256(path) for name, path in policy_source_paths.items()}
    if owned:
        if (getattr(args, "owner_adapter_admission", None) is None
                or not getattr(args, "owner_adapter_admission_sha256", None)
                or sha256(args.owner_adapter_admission) != args.owner_adapter_admission_sha256):
            raise ValueError("source-bound synthetic owner integration admission is required")
        gate = json.loads(Path(args.owner_adapter_admission).read_text())
        if (gate.get("status") != "SYNTHETIC_ROTATION_INTEGRATION_PASS"
                or gate.get("research_adapter_sha256") != admitted_engine_sha256
                or gate.get("policy_sources") != policy_sources
                or gate.get("rotation_protocol_sha256") != ROTATION_PROTOCOL_SHA256
                or gate.get("neutral_v2_financial_body_identical") is not True):
            raise ValueError("rotation integration admission does not bind current source bytes")
        if args.arm != "rotation_owned_control_174":
            control_path = getattr(args, "matched_owned_control_receipt", None)
            receipt_sha = getattr(args, "matched_owned_control_receipt_sha256", None)
            if control_path is None or not receipt_sha or sha256(control_path) != receipt_sha:
                raise ValueError("integrated owner candidates require independently audited owned control")
            control_receipt = json.loads(Path(control_path).read_text())
            control_arm = "rotation_owned_control_174"
            control_results_path = output.parent / control_arm / "results.json"
            if (control_receipt.get("passed") is not True
                    or control_receipt.get("full_cash_owner_adapter_verified") is not True
                    or control_receipt.get("strategy_imported") is not False
                    or control_receipt.get("arm") != control_arm
                    or control_receipt.get("input_array_sha256") != REFERENCE_ARRAY_SHA256
                    or control_receipt.get("input_metadata_sha256") != REFERENCE_METADATA_SHA256
                    or control_receipt.get("results_sha256") != sha256(control_results_path)):
                raise ValueError("audit receipt is not bound to the exact matched owner control")
            control_result = json.loads(control_results_path.read_text())
            if (control_result.get("research_adapter_sha256") != admitted_engine_sha256
                    or control_result.get("policy_sources") != policy_sources):
                raise ValueError("matched owner control used different frozen sources")
    admission = {"arm": args.arm, "protocol_sha256": args.protocol_sha256,
        "research_adapter_sha256": admitted_engine_sha256,
        "native_manifest_sha256": sha256(Path(args.native_manifest)),
        "native_adapter_sha256": sha256(Path(args.native_adapter)),
        "native_bridge_sha256": sha256(Path(args.native_bridge)),
        "expected_reference_array_sha256": REFERENCE_ARRAY_SHA256,
        "expected_reference_metadata_sha256": REFERENCE_METADATA_SHA256,
        "supplement_sha256s": {name: item["sha256"] for name, item in supplements.items()},
        "policy_sources": policy_sources,
        "owned_adapter_admission_sha256": getattr(args, "owner_adapter_admission_sha256", None),
        "matched_owned_control_receipt_sha256": getattr(args, "matched_owned_control_receipt_sha256", None),
        "research_only": True, "production_activation": False, "holdout_accessed": False}
    from datetime import datetime, timezone
    admission["recorded_before_strategy_imports_at"] = datetime.now(timezone.utc).isoformat()
    (output / "run_admission.receipt.json").write_text(json.dumps(admission, indent=2) + "\n")
    # Every source/input check precedes strategy imports and outcomes.
    native_sources = verify_native(Path(args.native_core), Path(args.native_manifest),
        Path(args.native_adapter), Path(args.native_bridge))
    input_identity = verify_reference(Path(args.prepared))
    if getcontext().prec != 28 or getcontext().rounding != "ROUND_HALF_EVEN":
        raise ValueError("preserved native Decimal context must remain 28/HALF_EVEN")
    native = load_native(Path(args.native_core), Path(args.native_adapter), Path(args.native_bridge), native_sources)
    selection = bool(arm.get("selection"))
    ladder = arm.get("ladder_assignment") == "prior_completed_drawdown"
    use_selector = selection or ladder
    source, changes = build_campaign_body(native.GENERATED_SOURCE,
        selection=selection, directional=bool(arm.get("directional")),
        recovery_quality=bool(arm.get("recovery_quality")),
        ladder=ladder, entry_gate=bool(arm.get("warmup_days")) or bool(arm.get("regime_gate")))
    if owned:
        source, owner_changes = build_owned_body(source, ladder=ladder)
        changes.extend(owner_changes)
    source, profit_changes = build_profit_body(source, native_dttp=bool(arm.get("native_dttp")))
    changes.extend(profit_changes)
    source, rotation_changes = build_rotation_body(source, rotation=arm.get("rotation"))
    changes.extend(rotation_changes)
    # Research helpers are imported only after all native/source/input checks.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    owned_module = None
    sizing_module = None
    if owned:
        from research.danger_room import owned_cash as owned_module
    rotation_module = None
    if arm.get("rotation"):
        from research.danger_room import rotation_policy as rotation_module
        if rotation_module.FROZEN_ROTATION_PROTOCOL_SHA256 != ROTATION_PROTOCOL_SHA256:
            raise ValueError("rotation helper protocol binding differs")
    if arm.get("productive_sizing"):
        from research.danger_room import productive_sizing as sizing_module
    selector_module = None
    if use_selector:
        selector_path = Path(__file__).with_name("opportunity_selector.py")
        spec = importlib.util.spec_from_file_location("spectra_campaign_selector", selector_path)
        selector_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = selector_module
        spec.loader.exec_module(selector_module)
    regime_module = None
    if arm.get("regime_gate"):
        regime_path = Path(__file__).with_name("market_regime.py")
        spec = importlib.util.spec_from_file_location("spectra_campaign_regime", regime_path)
        regime_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = regime_module
        spec.loader.exec_module(regime_module)
    account_stream = gzip.open(output / "account_5m.csv.gz", "wt", newline="", compresslevel=1)
    account_writer = csv.DictWriter(account_stream, fieldnames=["timestamp", "cash", "committed_basis",
        "marked_value", "equity", "active_positions", "unique_base_assets", "parked_cash",
        "deployable_cash", "cumulative_realized", "cumulative_fees", "pending_orders"])
    account_writer.writeheader()
    pending_stream = gzip.open(output / "pending_orders.jsonl.gz", "wt", compresslevel=1)
    watch_stream = gzip.open(output / "watchlist_updates.jsonl.gz", "wt", compresslevel=1)
    owner_stream = gzip.open(output / "owner_account_5m.jsonl.gz", "wt", compresslevel=1) if owned else None
    owner_journal = gzip.open(output / "owner_transactions.jsonl.gz", "wt", compresslevel=1) if owned else None
    rotation_stream = gzip.open(output / "rotation_events.jsonl.gz", "wt", compresslevel=1)
    OriginalDataset = native.Dataset
    profiles = {name: RecoveryLadder(name, tuple(RecoveryRung(drop, multiple)
        for drop, multiple in zip(profile["drop_fractions"], profile["seed_multiples"])))
        for name, profile in protocol["ladder_hypotheses"]["profiles"].items()}

    class CampaignDataset(OriginalDataset):
        def __init__(self):
            original_init = native.a.SparseDataset.__init__
            def redirected(instance, ignored_path, **kwargs):
                return original_init(instance, Path(args.prepared), **kwargs)
            native.a.SparseDataset.__init__ = redirected
            try:
                super().__init__(case=arm["case"], enabled=True, frames=None, trace_path=None)
            finally:
                native.a.SparseDataset.__init__ = original_init
            self.pair_indices = {pair: i for i, pair in enumerate(self.pairs)}
            self.ladders = LadderBook({})
            self.owner = (OwnedCashHooks(owned_module, initial_cash=D(arm["capital"]),
                redeploy=bool(arm.get("redeploy_confirmed_partial")),
                emit=lambda event: owner_journal.write(json.dumps({"index": self.current_index,
                    "timestamp": self.timestamp(self.current_index) + self.interval_seconds,
                    **event}, default=str) + "\n")) if owned else None)
            self.research_counts = Counter()
            self.rotation = (RotationHooks(rotation_module, mode=arm["rotation"], pairs=self.pairs,
                rows=self.rows, emit=lambda event: rotation_stream.write(json.dumps(event,
                    default=str) + "\n")) if rotation_module else None)
            self.rotation_ranking = FrameRankingCache() if self.rotation else None
            self.selector = (selector_module.MatrixOpportunitySelector(self.rows, self.pairs,
                self.start, config=selector_module.SelectorConfig(**protocol["selector_config"]))
                if use_selector else None)
            self.weekly_members = None
            self.next_weekly_refresh = None
            self.last_watched_day = None
            self.decision_digest = None
            self.final_pending = []
            self.manifest.update(campaign_protocol_sha256=args.protocol_sha256,
                research_adapter_sha256=admitted_engine_sha256, experimental_arm=args.arm,
                continuous_month_boundary_resets=False,
                cash_owner_model=("separate exact PRODUCTIVE/VCAP research owner journal; current-cash reservations and confirmed slot closure" if owned
                    else "original shared fungible cash with source-colored parked recovery proceeds; no complete production cash-owner buckets"),
                selector_screening_proxy_only=selection, recovered_original_coin_ladders=False)

        def observe_account(self, state):
            sample = state["samples"][-1]
            row = asdict(sample)
            parked = self.parked_cash()
            row.update(parked_cash=parked, deployable_cash=sample.cash - parked,
                cumulative_realized=state["realized"], cumulative_fees=state["fees"],
                pending_orders=len(state["pending"]))
            account_writer.writerow(row)
            pending_stream.write(json.dumps({"timestamp": sample.timestamp,
                "orders": [asdict(o) for o in state["pending"]]}, default=str) + "\n")
            self.decision_digest = state["decision_digest"].hexdigest()
            self.final_pending = [asdict(o) for o in state["pending"]]
            if self.owner:
                observation = self.owner.observe(state)
                owner_stream.write(json.dumps({"timestamp": sample.timestamp, **observation}, default=str) + "\n")

        def parked_cash(self):
            return self.owner.ledger.parked_cash_total if getattr(self, "owner", None) else super().parked_cash()

        def owner_authorize(self, order, equity, cfg):
            return self.owner.authorize(order, equity=equity, cfg=cfg)

        def owner_preflight(self, order, gross, fee, equity, cfg):
            return self.owner.preflight(order, gross, fee, equity, cfg)

        def owner_recovery_available(self, equity):
            return self.owner.ledger.available(owned_module.Owner.VCAP, equity_before=equity)

        def owner_entry_ticket(self, equity, cfg):
            available = self.owner.ledger.available(owned_module.Owner.PRODUCTIVE, equity_before=equity)
            if sizing_module is None:
                return cfg.entry_budget if available >= cfg.entry_budget else ZERO
            budget, diagnostic = sizing_module.productive_entry_budget(equity,
                self.owner.ledger.available(owned_module.Owner.PRODUCTIVE),
                equity * cfg.pair_cap_fraction, available)
            self.research_counts[f"sizing_{'admitted' if diagnostic['admitted'] else diagnostic['reason']}"] += 1
            return budget

        def rank_candidates(self, candidates, timestamp):
            ordered, diagnostics = self.selector.rank_at_index(self.current_index + 1)
            if arm.get("list_refresh_days"):
                if self.next_weekly_refresh is None or timestamp >= self.next_weekly_refresh:
                    self.weekly_members = tuple(ordered)
                    self.next_weekly_refresh = timestamp + int(arm["list_refresh_days"]) * 86400
                ordered = [pair for pair in self.weekly_members
                    if diagnostics[pair].get("eligible")]
            allowed = set(candidates)
            selected = [pair for pair in ordered if pair in allowed]
            self.research_counts["selector_denied_candidate_steps"] += len(allowed) - len(selected)
            day = timestamp // 86400
            if day != self.last_watched_day:
                watch_stream.write(json.dumps({"timestamp": timestamp,
                    "evidence_cutoff": timestamp, "ordered_pairs": ordered,
                    "diagnostics": diagnostics}, default=str) + "\n")
                self.last_watched_day = day
            return selected

        def directional_entry_allowed(self, pair, index):
            allowed = causal_anchor_allowed(self.rows, self.pair_indices[pair], index)
            self.research_counts["directional_allowed" if allowed else "directional_denied"] += 1
            return allowed

        def entry_gate_allowed(self, index):
            if index + 1 < int(arm.get("warmup_days", 0)) * 288:
                self.research_counts["warmup_denied"] += 1
                return False
            if arm.get("regime_gate"):
                allowed, diagnostic = regime_module.causal_market_regime(self.rows, index)
                self.research_counts["regime_allowed" if allowed else "regime_denied"] += 1
                return allowed
            return True

        def recovery_quality_allowed(self, pair, index):
            allowed = causal_recovery_allowed(self.rows, self.pair_indices[pair], index)
            self.research_counts["recovery_quality_allowed" if allowed else "recovery_quality_denied"] += 1
            return allowed

        def ladder_budget(self, position, close):
            budget, reason = self.ladders.requested_budget(position.position_id, position.pair, close)
            self.research_counts[reason] += 1
            return budget

        def note_recovery_order(self, order, position):
            self.ladders.reserve_recovery(order.order_id, position.position_id, position.pair, order.budget)

        def note_rejection(self, order, reason):
            self.ladders.rejected_order(order.order_id)
            if self.owner:
                self.owner.reject(order, reason)
            if self.rotation:
                self.rotation.reject(order, reason)

        def record_buy(self, fill, position, cash_before, *, equity_before=None):
            if self.owner:
                self.owner.buy(fill, equity=equity_before)
            super().record_buy(fill, position, cash_before)
            if self.rotation:
                self.rotation.confirm_entry(fill)
            if not ladder:
                return
            confirmed_all_in = (owned_module.all_in_quote(fill.gross_quote, fill.fee_quote)
                                if self.owner else fill.gross_quote + fill.fee_quote)
            if fill.kind == "ENTRY":
                # Fill happens before the execution candle is ingested. Only
                # rows strictly before this actual next-open fill enter the
                # category, never the execution bar's high/low/close.
                _, diagnostics = self.selector.rank_at_index(self.current_index)
                evidence = diagnostics[fill.pair]
                if (evidence.get("observed_completed_days", 0) >= protocol["selector_config"]["minimum_history_days"]
                        and evidence.get("recent_coverage", 0) >= protocol["selector_config"]["minimum_recent_coverage"]):
                    drawdown = evidence["observed_close_drawdown_fraction"]
                    name = "shallow" if drawdown <= 0.1 else "medium" if drawdown <= 0.2 else "deep"
                    self.ladders.profiles[fill.pair] = profiles[name]
                else:
                    name = "UNKNOWN"
                    self.ladders.profiles.pop(fill.pair, None)
                self.research_counts[f"ladder_parent_category_{name}"] += 1
                self.ladders.confirmed_entry(fill.position_id, fill.pair, fill.price,
                    confirmed_all_in)
                self.events.append({"event": "LADDER_ASSIGNMENT", "position_id": fill.position_id,
                    "pair": fill.pair, "category": name, "entry_anchor": fill.price,
                    "entry_seed_all_in": confirmed_all_in,
                    "evidence_cutoff": fill.timestamp - self.interval_seconds,
                    "source": "new predeclared causal research hypothesis"})
            elif fill.kind == "RECOVERY":
                self.ladders.confirmed_recovery(fill.position_id, fill.pair, fill.quantity,
                    confirmed_all_in, order_id=fill.order_id)

        def record_full_exit(self, fill):
            owner_before = dict(self.owner.ledger.cash_by_owner) if self.rotation else None
            parked_before = self.owner.ledger.parked_cash_total if self.rotation else None
            if self.owner:
                self.owner.sale(fill, self.ledger.list_active_lots(str(fill.position_id)), partial=False)
            if self.rotation:
                self.rotation.confirm_sale(fill,
                    owner_before={key.value: value for key, value in owner_before.items()},
                    owner_after={key.value: value for key, value in self.owner.ledger.cash_by_owner.items()},
                    parked_before=parked_before, parked_after=self.owner.ledger.parked_cash_total)
            return super().record_full_exit(fill)

        def rotation_update(self, index, timestamp):
            self.rotation.update(index, timestamp)

        def rotation_offer(self, position, close, timestamp, index, cfg, positions, bars,
                           fingerprints, volume_24h, closed_this_frame, pending_pairs, exit_plan):
            if self.rotation.mode == "stale":
                return self.rotation.offer(position, close, timestamp=timestamp, index=index,
                    occupied=len(positions), ranked_candidates=(),
                    armed_floor=exit_plan.state.profit_floor_reached_at is not None)
            def movement_rank(pair):
                window = fingerprints[pair].windows[native.a.original.FingerprintHorizon.FAST]
                movement = window.ema_abs_return_bps
                if movement is None:
                    bar = bars[pair]
                    movement = float((bar.high - bar.low) / bar.open * BPS)
                return (-movement, -float(volume_24h[pair]), pair)
            active_bases = {native.a.original.base_asset(pair) for pair in positions}
            ordered = self.rotation_ranking.ordered(index, bars, movement_rank)
            candidates = [pair for pair in ordered if pair not in positions
                and pair not in pending_pairs and pair not in closed_this_frame
                and native.a.original.base_asset(pair) not in active_bases | {"BTC", "ETH"}
                and bars[pair].quote_volume * cfg.volume_participation >= D("10.25") / (ONE + cfg.fee_bps / BPS)]
            return self.rotation.offer(position, close, timestamp=timestamp, index=index,
                occupied=len(positions), ranked_candidates=candidates,
                armed_floor=exit_plan.state.profit_floor_reached_at is not None)

        def note_rotation_order(self, order, position, offer):
            self.rotation.submit(order, position, offer)

        def rotation_execution_priority(self, order):
            return self.rotation.priority(order)

        def rotation_sale_preflight(self, order, position, index, gross, cfg):
            return self.rotation.preflight(order, position, index, gross, cfg)

        def rotation_floor_bypass(self, order, position):
            return self.rotation.bypass(order, position)

        def rotation_replacement_candidates(self, candidates, timestamp):
            return self.rotation.replacement_candidates(candidates, timestamp)

        def rotation_note_entry_order(self, order):
            self.rotation.note_entry(order)

        def rotation_replacement_denied(self, order, reason):
            self.rotation.replacement_denied(order, reason)

        def rotation_finish_frame(self, timestamp):
            self.rotation.finish_frame(timestamp)

        def fill_partial(self, order, position, index, timestamp, cfg, capacity):
            selected = ([self.ledger.lots[lot_id] for lot_id in self.orders[order.order_id].lot_ids]
                        if self.owner and order.order_id in self.orders else [])
            outcome, reason = super().fill_partial(order, position, index, timestamp, cfg, capacity)
            if self.owner and outcome is not None:
                self.owner.sale(outcome[0], selected, partial=True)
            return outcome, reason

    data = CampaignDataset()
    namespace = dict(native.a.original.__dict__)
    namespace["FleetOrder"] = native.ResearchOrder
    exec(compile(source, str(output / "generated_campaign_body.py"), "exec"), namespace)
    class CampaignSimulator(native.a.original.RotatingFleetSimulator):
        run = namespace["run"]
    cfg = replace(native.a.original.FleetConfig(), initial_cash=D(arm["capital"]),
        fee_bps=D(arm["fee_bps"]), slippage_bps=D(arm["slippage_bps"]))
    simulator = CampaignSimulator(cfg,
        exit_policy=replace(native.a.VCAPProfitExitPolicy(), target_net_profit_bps=data.whole_target_bps),
        exit_strategy=replace(native.a.original.FleetExitStrategy(), mode=(native.a.original.FleetExitMode.DTTP
            if arm.get("native_dttp") else native.a.original.FleetExitMode.HARD_TARGET)))
    started = time.monotonic()
    try:
        result = simulator.run(data)
    finally:
        account_stream.close()
        pending_stream.close()
        watch_stream.close()
        if owner_stream:
            owner_stream.close()
            owner_journal.close()
        rotation_stream.close()
    elapsed = time.monotonic() - started
    if sha256(Path(__file__)) != admitted_engine_sha256:
        raise ValueError("campaign adapter changed while the admitted evaluation was running")
    if {name: sha256(path) for name, path in policy_source_paths.items()} != policy_sources:
        raise ValueError("a frozen policy helper changed during the admitted evaluation")
    if owned:
        (output / "owner_checkpoint.json").write_text(json.dumps(data.owner.ledger.checkpoint(), indent=2) + "\n")
        (output / "owner_terminal.json").write_text(json.dumps(data.owner.ledger.snapshot(), indent=2) + "\n")
    # Every fill and surviving lot is serialized for an independent replay of
    # cash/quantity/basis arithmetic; this is not just a summary test.
    with (output / "fills.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(asdict(result.fills[0])) if result.fills else
            ["timestamp", "order_id", "pair", "kind", "position_id", "quantity", "price", "gross_quote",
             "fee_quote", "basis_released", "realized_pnl", "dependency_order_id"])
        writer.writeheader()
        writer.writerows(asdict(fill) for fill in result.fills)
    with gzip.open(output / "ownership_events.json.gz", "wt", compresslevel=1) as stream:
        json.dump(data.events, stream, default=str)
    final_at = data.timestamp(data.frames - 1) + data.interval_seconds
    terminal = [{**asdict(position), "mark": data.marks[position.pair],
        "mark_timestamp": data.last_seen[position.pair],
        "mark_age_seconds": final_at - data.last_seen[position.pair],
        "position_age_seconds": final_at - position.opened_at}
        for position in result.positions]
    (output / "terminal.json").write_text(json.dumps(terminal, default=str, indent=2) + "\n")
    (output / "terminal_lots.json").write_text(json.dumps([asdict(lot) for lot in data.ledger.lots.values()],
        default=str, indent=2) + "\n")
    months = {}
    from datetime import datetime, timezone
    previous_equity = cfg.initial_cash
    for sample in result.samples:
        month = datetime.fromtimestamp(sample.timestamp - data.interval_seconds, timezone.utc).strftime("%Y-%m")
        if month not in months:
            months[month] = {"starting_equity": previous_equity, "ending_equity": sample.equity,
                "realized_pnl": ZERO, "fees": ZERO, "frames": 0}
        months[month]["ending_equity"] = sample.equity
        months[month]["frames"] += 1
        previous_equity = sample.equity
    for fill in result.fills:
        month = datetime.fromtimestamp(fill.timestamp - data.interval_seconds, timezone.utc).strftime("%Y-%m")
        months[month]["realized_pnl"] += fill.realized_pnl
        months[month]["fees"] += fill.fee_quote
    for values in months.values():
        values["marked_pnl"] = values["ending_equity"] - values["starting_equity"]
    payload = {"arm": args.arm, "status": "COMPLETED", "completed_strategy_evaluation": True,
        "research_only": True, "production_activation": False, "holdout_accessed": False,
        "protocol_sha256": args.protocol_sha256, "input_identity": input_identity,
        "native_sources": native_sources, "native_manifest_sha256": sha256(Path(args.native_manifest)),
        "native_generated_body_sha256": GENERATED_NATIVE_SHA256,
        "research_generated_body_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "research_adapter_sha256": admitted_engine_sha256, "declared_changes": changes,
        "policy_sources": policy_sources,
        "supplement_sha256s": admission["supplement_sha256s"],
        "declared_arm": arm,
        "effective_profit_policy": {"mode": str(simulator.exit_strategy.mode),
            "hard_target": data.hard_target, "execution_floor_bps": data.execution_floor_bps,
            "armed_profit_floor_blocks_recovery": bool(arm.get("native_dttp"))},
        "owner_model": ({"initial_productive": "1500", "initial_VCAP": "500",
            "strict_target_after_all_pending_holds": "0.25", "conditional_replacements_preauthorized": False,
            "refill": "confirmed closure then completed-close proposal; immediate next observed open FOK",
            "future_exit_cash_or_slot_borrowing": False, "funding_counts": dict(data.owner.counts)} if owned else None),
        "financial_metrics": result.metrics, "elapsed_seconds": elapsed,
        "decision_digest_sha256": data.decision_digest, "rejection_counts": result.rejections,
        "research_counts": dict(data.research_counts), "ending_parked_cash": data.parked_cash(),
        "rotation_counts": dict(data.rotation.counts) if data.rotation else {},
        "rotation_terminal": list(data.rotation.tags.values()) if data.rotation else [],
        "ending_deployable_cash": result.cash - data.parked_cash(),
        "partial_fills": sum(fill.kind == "PARTIAL_RECYCLE" for fill in result.fills),
        "partial_principal_reused": data.principal_reused,
        "capital_dollar_days": sum((sample.committed_basis for sample in result.samples), ZERO) / D(288),
        "pending_terminal": data.final_pending, "monthly": months,
        "coverage": {"native_spot_selection": True, "native_recovery_planners": True,
            "native_partial_lot_recycling": True, "native_protected_net_exit_surrogate": True,
            "continuous_finite_cash_and_inventory": True, "recorded_volume_fok_surrogate": True,
            "productive_VCAP_cash_owner_integration": owned, "full_API_lifecycle": False,
            "live_orderbook_or_pair_specific_historical_minima": False, "actual_hedge_route": False,
            "DTSS_enabled": False, "DTSSR_enabled": False, "PSSM_enabled": False},
        "interpretation": "Previously inspected development history; recorded OHLC, estimated next-open FOK execution and historical marks. Positive development PnL does not establish independent or live profitability."}
    (output / "results.json").write_text(json.dumps(payload, default=str, indent=2) + "\n")
    (output / "generated_campaign_body.py").write_text(source)
    artifacts = [{"file": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(output.iterdir()) if path.is_file()]
    (output / "artifact_manifest.json").write_text(json.dumps(artifacts, indent=2) + "\n")
    print(json.dumps({"arm": args.arm, "status": "COMPLETED", "elapsed_seconds": elapsed,
        "ending_equity": str(result.metrics["ending_equity"]),
        "total_pnl": str(result.metrics["total_pnl"]),
        "drawdown": str(result.metrics["max_drawdown_fraction"]),
        "source_owner_integration": owned}), flush=True)
    return payload


if __name__ == "__main__":
    main()
