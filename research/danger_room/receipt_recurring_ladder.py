"""Receipt-bound recurring recovery inventory; offline source-bound research only.

No method credits cash or moves owners. The preserved owner journal and native
consumed lots supply the evidence; completed observations supply price evidence.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, localcontext
from fractions import Fraction
import hashlib
import json

D = Decimal
ZERO = D(0)
PROTOCOL_SHA256 = "47c7e948fc510dfc67e29d4d294e75d238a93269da1f53a7f77370ea5d8dabe1"
SPEC_SHA256 = "b82b86cf78794dfd156b33cf8ee8c0b98068cf45387fd4095ce1c45701717750"
MAX_EPISODES = 3


def exact(value):
    if isinstance(value, Fraction):
        return value
    if not isinstance(value, D) or not value.is_finite():
        raise ValueError("finite recorded Decimal evidence is required")
    return Fraction(value)


def _positive(value):
    if exact(value) <= 0:
        raise ValueError("positive recorded evidence is required")
    return value


def _source(lot):
    return str(lot.source)


def _native_components(lot):
    return (lot.base_remaining, lot.cost_basis_remaining_quote, lot.entry_fees_remaining_quote)


def _fill_identity(fill):
    return tuple(getattr(fill, key, None) for key in ("timestamp", "order_id", "pair", "kind",
        "position_id", "quantity", "price", "gross_quote", "fee_quote", "basis_released",
        "realized_pnl", "dependency_order_id"))


def _lot_identity(lot):
    return (lot.lot_id, lot.position_id, lot.pair, _source(lot), *_native_components(lot))


def _encode(value):
    if isinstance(value, D):
        return {"decimal": str(value)}
    if isinstance(value, Fraction):
        return {"fraction": [value.numerator, value.denominator]}
    if isinstance(value, tuple):
        return {"tuple": [_encode(item) for item in value]}
    if isinstance(value, dict):
        return {"mapping": [[_encode(key), _encode(item)] for key, item in value.items()]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"decimal"}:
            item = D(value["decimal"])
            exact(item)
            return item
        if set(value) == {"fraction"}:
            return Fraction(*value["fraction"])
        if set(value) == {"tuple"}:
            return tuple(_decode(item) for item in value["tuple"])
        if set(value) == {"mapping"}:
            rows = [(_decode(key), _decode(item)) for key, item in value["mapping"]]
            out = dict(rows)
            if len(out) != len(rows):
                raise ValueError("duplicate checkpoint identity")
            return out
        raise ValueError("unknown checkpoint typed value")
    return value


def checkpoint_sha256(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class RecurringLadderBook:
    """A finite original progression plus at most two proved renewals per rung."""
    def __init__(self, profiles, *, emit=None):
        self.profiles = dict(profiles)
        self.parents = {}
        self.reservations = {}
        self.receipts = {}
        self.emit = emit or (lambda event: None)

    def _event(self, name, **values):
        self.emit({"event": name, **values})

    def _replay(self, key, payload):
        if key in self.receipts:
            if self.receipts[key] != payload:
                raise ValueError("recurrence receipt identity changed")
            return True
        return False

    @staticmethod
    def _buy_proof(fill, native_lot, owner_ledger, *, expected_kind):
        pid = str(fill.position_id)
        lot_id = f"lot:{fill.order_id}"
        receipt_id = f"fill:{fill.order_id}"
        source = "BASE_ENTRY" if expected_kind == "ENTRY" else "VCAP_RECOVERY"
        owned = owner_ledger.lots.get(lot_id)
        payload = owner_ledger.journal.get(receipt_id)
        terminal = owner_ledger.journal.get(f"terminal-filled:{fill.order_id}")
        if (fill.kind != expected_kind or native_lot.lot_id != lot_id
                or native_lot.position_id != pid or native_lot.pair != fill.pair
                or _source(native_lot) != source or owned is None
                or owned.parent_id != pid or owned.pair != fill.pair
                or str(owned.owner) != ("PRODUCTIVE" if expected_kind == "ENTRY" else "VCAP")
                or _native_components(native_lot) != (fill.quantity, fill.gross_quote, fill.fee_quote)
                or (owned.quantity, owned.gross_basis, owned.entry_fees) != _native_components(native_lot)
                or payload is None or payload[:7] != ("BUY", f"buy:{fill.order_id}", lot_id,
                    fill.quantity, fill.gross_quote, fill.fee_quote, pid)
                or terminal != ("TERMINAL", f"buy:{fill.order_id}", "FILLED", True)):
            raise ValueError("buy lacks exact native and owner receipt proof")
        return lot_id, receipt_id

    def confirmed_entry(self, position_id, pair, price, all_in, *, fill, native_lot, owner_ledger):
        if position_id in self.parents or fill.position_id != position_id or fill.pair != pair:
            raise ValueError("parent identities cannot be reused or substituted")
        lot_id, receipt_id = self._buy_proof(fill, native_lot, owner_ledger, expected_kind="ENTRY")
        if exact(all_in) != exact(fill.gross_quote) + exact(fill.fee_quote) or price != fill.price:
            raise ValueError("entry anchor/seed differs from actual fill")
        profile = self.profiles.get(pair)
        profile_rows = None if profile is None else tuple((r.drop_fraction, r.seed_multiple) for r in profile.rungs)
        parent = {"pair": pair, "anchor": _positive(price), "seed": _positive(all_in),
            "profile": profile, "profile_rows": profile_rows, "profile_name": None if profile is None else profile.name,
            "base_lot_id": lot_id, "entry_receipt_id": receipt_id, "consumed": 0,
            "turnover": Fraction(0), "turnover_cap": Fraction(0) if profile is None else
                MAX_EPISODES * exact(all_in) * sum((exact(row[1]) for row in profile_rows), Fraction(0)),
            "episodes": {} if profile is None else {i: [] for i in range(len(profile_rows))},
            "pending": {}, "retired": None, "last_observed_index": -1,
            "last_observed_timestamp": None, "last_close": None, "offer": None,
            "last_proposal_index": -1}
        self.parents[position_id] = parent
        parent["base_components"] = _native_components(native_lot)
        parent["native_quantity"] = fill.quantity
        parent["native_basis"] = fill.gross_quote + fill.fee_quote
        self.receipts[f"entry:{position_id}"] = (pair, price, all_in, lot_id, profile_rows,
                                                 parent["profile_name"], _fill_identity(fill))
        self._event("RECURRENCE_PARENT_BOUND", position_id=position_id, pair=pair,
                    entry_receipt_id=receipt_id, base_lot_id=lot_id, anchor=price, seed=all_in,
                    profile=parent["profile_name"], turnover_cap=parent["turnover_cap"])

    def observe_completed_close(self, position_id, pair, index, timestamp, close):
        parent = self.parents.get(position_id)
        if parent is None or parent["pair"] != pair or parent["retired"]:
            return
        _positive(close)
        if index <= parent["last_observed_index"]:
            raise ValueError("completed recurrence observations must be strictly chronological")
        if parent["last_observed_timestamp"] is not None and timestamp <= parent["last_observed_timestamp"]:
            raise ValueError("completed recurrence timestamp moved backward")
        parent.update(last_observed_index=index, last_observed_timestamp=timestamp, last_close=close, offer=None)
        for rung, episodes in parent["episodes"].items():
            if not episodes:
                continue
            episode = episodes[-1]
            if not episode["eligible_disposal"] or index < episode["disposed_index"]:
                continue
            threshold = parent["anchor"] * (D(1) - parent["profile_rows"][rung][0])
            if close > threshold:
                episode.update(reset_index=index, reset_timestamp=timestamp, crossing_index=None,
                               crossing_timestamp=None)
            elif episode["reset_index"] is not None and index > episode["reset_index"]:
                if episode["crossing_index"] is None:
                    episode.update(crossing_index=index, crossing_timestamp=timestamp)
        self._event("RECURRENCE_COMPLETED_OBSERVATION", position_id=position_id, pair=pair,
            index=index, completed_timestamp=timestamp, close=close,
            price_episodes=[{"rung": rung, "episode_id": episodes[-1]["episode_id"],
                "eligible_disposal": episodes[-1]["eligible_disposal"],
                "disposed_index": episodes[-1]["disposed_index"],
                "reset_index": episodes[-1]["reset_index"], "crossing_index": episodes[-1]["crossing_index"]}
                for rung, episodes in parent["episodes"].items() if episodes])

    def requested_budget(self, position_id, pair, completed_close, *, index,
                         profit_state, pending_parent_known, pending_parent):
        parent = self.parents.get(position_id)
        if parent is None or parent["pair"] != pair or parent["profile_rows"] is None:
            return None, "UNKNOWN_LADDER_PROFILE"
        parent["offer"] = None
        if parent["retired"]:
            return None, "PARENT_RETIRED"
        close = _positive(completed_close)
        original = parent["consumed"]
        # This branch intentionally retains original v2 first-pass budgeting and
        # overrides every renewed choice even when later funding rejects it.
        if original < len(parent["profile_rows"]):
            drop, multiple = parent["profile_rows"][original]
            if close <= parent["anchor"] * (D(1) - drop):
                parent["offer"] = {"rung": original, "ordinal": 1, "renewal": False, "index": index}
                return parent["seed"] * multiple, "LADDER_RUNG_ELIGIBLE"
        if index != parent["last_observed_index"] or close != parent["last_close"]:
            return None, "RENEWAL_COMPLETED_OBSERVATION_UNPROVEN"
        if parent["last_proposal_index"] == index:
            return None, "ONE_PARENT_PROPOSAL_PER_BAR"
        if not pending_parent_known or pending_parent or parent["pending"]:
            return None, "RENEWAL_PENDING_PARENT"
        if profit_state is None or not hasattr(profit_state, "profit_floor_reached_at"):
            return None, "RENEWAL_PROFIT_STATE_UNKNOWN"
        if profit_state.profit_floor_reached_at is not None:
            return None, "RENEWAL_ARMED_PROFIT_FLOOR"
        for rung in reversed(range(original)):
            episodes = parent["episodes"][rung]
            if not episodes or len(episodes) >= MAX_EPISODES:
                continue
            previous = episodes[-1]
            threshold = parent["anchor"] * (D(1) - parent["profile_rows"][rung][0])
            if (previous["eligible_disposal"] and previous["reset_index"] is not None
                    and previous["crossing_index"] is not None and close <= threshold):
                parent["offer"] = {"rung": rung, "ordinal": len(episodes) + 1, "renewal": True, "index": index,
                    "prior_episode_id": previous["episode_id"], "reset_index": previous["reset_index"],
                    "crossing_index": previous["crossing_index"]}
                return parent["seed"] * parent["profile_rows"][rung][1], "RENEWED_RUNG_ELIGIBLE"
        return None, "NO_RECEIPT_BOUND_RENEWAL"

    def _ceiling(self, parent, rung):
        nominal = exact(parent["seed"]) * exact(parent["profile_rows"][rung][1])
        remaining = parent["turnover_cap"] - parent["turnover"] - sum(parent["pending"].values(), Fraction(0))
        return min(nominal, remaining)

    def limit_renewal_budget(self, position_id, budget, fee_rate):
        parent = self.parents[position_id]
        offer = parent["offer"]
        if offer is None or not offer["renewal"]:
            return budget
        ceiling = self._ceiling(parent, offer["rung"])
        if ceiling <= 0:
            return ZERO
        with localcontext() as ctx:
            ctx.prec = 28
            ctx.rounding = "ROUND_HALF_EVEN"
            value = min(budget, D(ceiling.numerator) / D(ceiling.denominator))
            for _ in range(32):
                gross = value / (D(1) + fee_rate)
                fee = gross * fee_rate
                if exact(gross) + exact(fee) <= ceiling:
                    return value
                value = value.next_minus()
        raise ArithmeticError("renewed native budget could not contract below exact ceiling")

    def reserve_recovery(self, order_id, position_id, pair, budget, *, owner_ledger, index):
        parent = self.parents[position_id]
        offer = parent["offer"]
        reservation = owner_ledger.reservations.get(f"buy:{order_id}")
        if (parent["pair"] != pair or parent["retired"] or offer is None or offer["index"] != index
                or order_id in self.reservations or parent["pending"]
                or parent["last_proposal_index"] == index or reservation is None
                or reservation.kind != "RECOVERY" or reservation.parent_id != str(position_id)
                or reservation.pair != pair or str(reservation.owner) != "VCAP"
                or reservation.terminal_status is not None or reservation.spent_quote != ZERO):
            raise ValueError("recovery reservation lacks exact current episode/owner scope")
        amount = exact(reservation.limit_quote)
        base = owner_ledger.lots.get(parent["base_lot_id"])
        if (base is None or base.parent_id != str(position_id) or base.pair != pair
                or str(base.owner) != "PRODUCTIVE"
                or (base.quantity, base.gross_basis, base.entry_fees) != parent["base_components"]):
            raise ValueError("original immutable BASE_ENTRY inventory is not active")
        # Original source arithmetic remains unchanged. Its tiny Decimal
        # difference from the nominal rung is retained, but lifetime is strict.
        if amount > parent["turnover_cap"] - parent["turnover"]:
            raise ValueError("recovery reservation exceeds lifetime turnover")
        nominal = parent["seed"] * parent["profile_rows"][offer["rung"]][1]
        if not offer["renewal"] and budget > nominal + D("1e-18"):
            raise ValueError("original reservation exceeds preserved v2 declared rung maximum")
        if offer["renewal"] and amount > self._ceiling(parent, offer["rung"]):
            raise ValueError("renewed reservation exceeds exact nominal/remaining turnover")
        record = {**offer, "parent_id": position_id, "pair": pair, "maximum_cash": reservation.limit_quote,
                  "budget": budget, "proposal_id": f"buy:{order_id}"}
        self.reservations[order_id] = record
        parent["pending"][order_id] = amount
        parent["last_proposal_index"] = index
        self._event("RECURRENCE_RESERVE", order_id=order_id, position_id=position_id, **record)

    def is_renewal(self, order_id):
        if order_id in self.reservations:
            return self.reservations[order_id]["renewal"]
        for parent in self.parents.values():
            for episodes in parent["episodes"].values():
                for episode in episodes:
                    if episode["order_id"] == order_id:
                        return episode["renewal"]
        return False

    def rejected_order(self, order_id, *, owner_ledger, index):
        reservation = self.reservations.get(order_id)
        if reservation is None:
            return
        payload = owner_ledger.journal.get(f"terminal-rejected:{order_id}")
        source_reservation = owner_ledger.reservations.get(f"buy:{order_id}")
        if (payload != ("TERMINAL", f"buy:{order_id}", "REJECTED", True)
                or source_reservation is None or source_reservation.spent_quote != ZERO
                or f"fill:{order_id}" in owner_ledger.journal
                or f"lot:{order_id}" in owner_ledger.lots):
            raise ValueError("rejection has no exact zero-fill owner terminal receipt")
        self.parents[reservation["parent_id"]]["pending"].pop(order_id)
        del self.reservations[order_id]
        self._event("RECURRENCE_REJECTED", order_id=order_id, index=index, reservation=reservation,
                    terminal_receipt_id=f"terminal-rejected:{order_id}")

    def confirmed_recovery(self, position_id, pair, quantity, cash_consumed, *, order_id,
                           fill, native_lot, owner_ledger, index):
        payload = (position_id, pair, quantity, cash_consumed, _fill_identity(fill), _lot_identity(native_lot), index)
        if self._replay(f"buy:{order_id}", payload):
            return
        parent = self.parents[position_id]
        reservation = self.reservations.get(order_id)
        lot_id, receipt_id = self._buy_proof(fill, native_lot, owner_ledger, expected_kind="RECOVERY")
        amount = exact(fill.gross_quote) + exact(fill.fee_quote)
        if (reservation is None or reservation["parent_id"] != position_id or reservation["pair"] != pair
                or fill.order_id != order_id or fill.position_id != position_id or fill.pair != pair
                or fill.quantity != quantity or exact(cash_consumed) != amount or amount <= 0
                or amount > exact(reservation["maximum_cash"]) or parent["retired"]):
            raise ValueError("confirmed recovery differs from exact episode reservation")
        rung = reservation["rung"]
        episodes = parent["episodes"][rung]
        if len(episodes) + 1 != reservation["ordinal"] or len(episodes) >= MAX_EPISODES:
            raise ValueError("episode count/progression differs")
        if not reservation["renewal"] and rung != parent["consumed"]:
            raise ValueError("original progression differs")
        if parent["turnover"] + amount > parent["turnover_cap"]:
            raise ArithmeticError("confirmed lifetime recovery turnover exceeds exact ceiling")
        if reservation["renewal"] and amount > exact(parent["seed"]) * exact(parent["profile_rows"][rung][1]):
            raise ArithmeticError("renewed fill exceeds exact original nominal rung")
        episode = {"episode_id": f"parent:{position_id}:rung:{rung}:episode:{len(episodes)+1}",
            "order_id": order_id, "accepted_order_id": order_id, "proposal_id": f"buy:{order_id}",
            "reservation_receipt_id": f"buy:{order_id}", "buy_receipt_id": receipt_id,
            "rung": rung, "ordinal": len(episodes)+1, "renewal": reservation["renewal"],
            "confirmed_index": index, "actual_all_in": amount, "lot_ids": (lot_id,),
            "remaining": {lot_id: (fill.quantity, fill.gross_quote, fill.fee_quote)},
            "net_profit": {lot_id: Fraction(0)}, "sale_receipt_ids": [], "release_proof_ids": [],
            "eligible_disposal": False, "disposed_index": None, "reset_index": None,
            "reset_timestamp": None, "crossing_index": None, "crossing_timestamp": None,
            "eligibility": deepcopy(reservation)}
        episodes.append(episode)
        if not reservation["renewal"]:
            parent["consumed"] += 1
        parent["turnover"] += amount
        parent["native_quantity"] += fill.quantity
        parent["native_basis"] += fill.gross_quote + fill.fee_quote
        parent["pending"].pop(order_id)
        del self.reservations[order_id]
        parent["offer"] = None
        self.receipts[f"buy:{order_id}"] = payload
        self._event("RECURRENCE_BUY_CONFIRMED", position_id=position_id, pair=pair, episode=deepcopy(episode),
                    confirmed_turnover=parent["turnover"])

    def confirm_disposal(self, fill, native_lots_before, owner_ledger, native_ledger, index):
        parent = self.parents.get(fill.position_id)
        if parent is None or parent["retired"]:
            raise ValueError("partial disposal has no active recurrence parent")
        sale_id, release_id = f"sale:{fill.order_id}", f"release:{fill.order_id}"
        sale = owner_ledger.journal.get(sale_id)
        release = owner_ledger.journal.get(release_id)
        snapshots = {lot.lot_id: lot for lot in native_lots_before}
        payload = (_fill_identity(fill), tuple(_lot_identity(lot) for lot in native_lots_before), index)
        if self._replay(sale_id, payload):
            return
        if (fill.kind != "PARTIAL_RECYCLE" or fill.pair != parent["pair"] or sale is None
                or sale[0] != "SALE" or sale[2:4] != ("PARTIAL", False)
                or sale[4:] != (fill.gross_quote, fill.fee_quote)
                or release != ("RELEASE_PARTIAL", sale_id, True)
                or set(snapshots) != {item.lot_id for item in sale[1]}):
            raise ValueError("partial disposal lacks exact SALE and same-owner RELEASE_PARTIAL proof")
        allocations = {item.lot_id: item for item in sale[1]}
        known = {lot_id: episode for episodes in parent["episodes"].values()
                 for episode in episodes for lot_id in episode["lot_ids"]}
        if not set(snapshots) <= set(known):
            raise ValueError("sale substituted another episode/base/parent lot")
        if sum((lot.base_remaining for lot in native_lots_before), ZERO) != fill.quantity:
            raise ValueError("sale allocation quantity differs from exact fill")
        if (sum((lot.cost_basis_remaining_quote for lot in native_lots_before), ZERO)
                + sum((lot.entry_fees_remaining_quote for lot in native_lots_before), ZERO)) != fill.basis_released:
            raise ValueError("sale allocation basis differs from exact fill")
        updates = []
        for lot_id, snapshot in snapshots.items():
            episode = known[lot_id]
            allocation = allocations[lot_id]
            if (snapshot.position_id != str(fill.position_id) or snapshot.pair != fill.pair
                    or _source(snapshot) != "VCAP_RECOVERY"
                    or _native_components(snapshot) != episode["remaining"][lot_id]):
                raise ValueError("captured source-native episode inventory differs")
            old = episode["remaining"][lot_id]
            released = (allocation.quantity, allocation.gross_basis_release, allocation.entry_fee_release)
            remaining = tuple(exact(before)-exact(delta) for before, delta in zip(old, released))
            if any(value < 0 for value in remaining):
                raise ValueError("sale exceeds episode inventory")
            native = native_ledger.lots.get(lot_id)
            owned = owner_ledger.lots.get(lot_id)
            native_after = None if native is None else tuple(exact(value) for value in _native_components(native))
            owned_after = (Fraction(0),)*3 if owned is None else tuple(exact(value) for value in
                (owned.quantity, owned.gross_basis, owned.entry_fees))
            if native_after != remaining or owned_after != remaining:
                raise ValueError("native/owner post-sale inventory does not prove exact remaining inventory")
            profit = exact(allocation.gross_proceeds)-exact(allocation.exit_fee)-exact(allocation.gross_basis_release)-exact(allocation.entry_fee_release)
            with localcontext() as context:
                context.prec = max(28, *(len(str(abs(v.numerator)))+len(str(v.denominator))+2 for v in remaining))
                decimal_remaining = tuple(D(v.numerator)/D(v.denominator) for v in remaining)
            updates.append((episode, lot_id, decimal_remaining, profit))
        for episode, lot_id, remaining, profit in updates:
            episode["remaining"][lot_id] = remaining
            episode["net_profit"][lot_id] += profit
            if sale_id not in episode["sale_receipt_ids"]:
                episode["sale_receipt_ids"].append(sale_id)
                episode["release_proof_ids"].append(release_id)
            complete = all(all(exact(component) == 0 for component in values) for values in episode["remaining"].values())
            episode["eligible_disposal"] = complete and all(value > 0 for value in episode["net_profit"].values())
            if complete:
                episode["disposed_index"] = index
                episode.update(reset_index=None, reset_timestamp=None, crossing_index=None, crossing_timestamp=None)
        parent["native_quantity"] -= fill.quantity
        parent["native_basis"] -= fill.basis_released
        if parent["native_quantity"] <= ZERO or parent["native_basis"] < ZERO:
            raise ArithmeticError("partial proof did not retain the native base parent")
        self.receipts[sale_id] = payload
        self._event("RECURRENCE_DISPOSAL_PROVEN", position_id=fill.position_id, pair=fill.pair,
                    sale_receipt_id=sale_id, release_proof_id=release_id, index=index,
                    source_fill_identity=_fill_identity(fill),
                    captured_native_lots=[_lot_identity(lot) for lot in native_lots_before],
                    source_owner_allocations=[{"lot_id":item.lot_id,"quantity":item.quantity,
                        "gross_basis_release":item.gross_basis_release,"entry_fee_release":item.entry_fee_release,
                        "gross_proceeds":item.gross_proceeds,"exit_fee":item.exit_fee} for item in sale[1]],
                    episode_states=[deepcopy(episode) for episode in {known[lot_id]["episode_id"]:known[lot_id]
                        for lot_id in snapshots}.values()])

    def retire_parent(self, fill, owner_ledger, native_ledger, index):
        parent = self.parents.get(fill.position_id)
        if parent is None:
            return
        sale = owner_ledger.journal.get(f"sale:{fill.order_id}")
        if sale is None or sale[0] != "SALE":
            raise ValueError("whole retirement has no exact source owner sale receipt")
        payload = (_fill_identity(fill), tuple((item.lot_id,item.quantity,
            item.gross_basis_release,item.entry_fee_release,item.gross_proceeds,item.exit_fee) for item in sale[1]), index)
        if self._replay(f"sale:{fill.order_id}", payload):
            return
        if (fill.kind != "EXIT" or fill.pair != parent["pair"] or sale is None or sale[0] != "SALE"
                or sale[2:4] != ("WHOLE", True) or sale[4:] != (fill.gross_quote, fill.fee_quote)
                or fill.quantity != parent["native_quantity"] or fill.basis_released != parent["native_basis"]
                or any(lot.parent_id == str(fill.position_id) for lot in owner_ledger.lots.values())
                or any(lot.position_id == str(fill.position_id) and any(exact(value) != 0 for value in
                       _native_components(lot)) for lot in native_ledger.lots.values()) or parent["pending"]):
            raise ValueError("whole exit does not prove fully retired parent lineage")
        if parent["retired"] is not None and parent["retired"] != f"sale:{fill.order_id}":
            raise ValueError("retirement identity changed")
        parent["retired"] = f"sale:{fill.order_id}"
        parent["native_quantity"] = ZERO
        parent["native_basis"] = ZERO
        self.receipts[parent["retired"]] = payload
        for episodes in parent["episodes"].values():
            for episode in episodes:
                for allocation in sale[1]:
                    if allocation.lot_id in episode["lot_ids"]:
                        episode["net_profit"][allocation.lot_id] += (exact(allocation.gross_proceeds)
                            -exact(allocation.exit_fee)-exact(allocation.gross_basis_release)-exact(allocation.entry_fee_release))
                        episode["remaining"][allocation.lot_id] = (ZERO,ZERO,ZERO)
                        episode["retired_whole_sale_id"] = parent["retired"]
                        episode["eligible_disposal"] = False
        parent["offer"] = None
        self._event("RECURRENCE_PARENT_RETIRED", position_id=fill.position_id,
                    sale_receipt_id=parent["retired"], index=index,
                    source_fill_identity=_fill_identity(fill), source_owner_allocations=payload[1],
                    terminal_episode_states=[deepcopy(episode) for episodes in parent["episodes"].values() for episode in episodes])

    def checkpoint(self):
        parents = deepcopy(self.parents)
        for parent in parents.values():
            parent.pop("profile")
        body = {"schema_version": 1, "protocol_sha256": PROTOCOL_SHA256, "specification_sha256": SPEC_SHA256,
                "parents": _encode(parents), "reservations": _encode(self.reservations), "receipts": _encode(self.receipts)}
        return {**body, "checkpoint_sha256": checkpoint_sha256(body)}

    @classmethod
    def from_checkpoint(cls, payload, *, expected_sha256):
        body = {key: value for key, value in payload.items() if key != "checkpoint_sha256"}
        digest = checkpoint_sha256(body)
        if (digest != payload.get("checkpoint_sha256") or digest != expected_sha256
                or body.get("schema_version") != 1 or body.get("protocol_sha256") != PROTOCOL_SHA256
                or body.get("specification_sha256") != SPEC_SHA256):
            raise ValueError("recurrence checkpoint lacks external exact manifest binding")
        out = cls({})
        out.parents = _decode(body["parents"])
        out.reservations = _decode(body["reservations"])
        out.receipts = _decode(body["receipts"])
        for pid, parent in out.parents.items():
            rows = parent["profile_rows"]
            parent["profile"] = None
            cap = Fraction(0) if rows is None else MAX_EPISODES * exact(parent["seed"]) * sum((exact(row[1]) for row in rows), Fraction(0))
            episodes = parent["episodes"]
            all_episodes = [episode for items in episodes.values() for episode in items]
            entry = out.receipts.get(f"entry:{pid}")
            if (entry is None or entry[:6] != (parent["pair"], parent["anchor"], parent["seed"], parent["base_lot_id"],
                    rows, parent["profile_name"]) or parent["base_components"] != (entry[6][5],entry[6][7],entry[6][8])
                    or parent["turnover_cap"] != cap or parent["turnover"] != sum((episode["actual_all_in"] for episode in all_episodes), Fraction(0))
                    or parent["turnover"] + sum(parent["pending"].values(), Fraction(0)) > cap
                    or rows is not None and set(episodes) != set(range(len(rows)))
                    or parent["consumed"] != sum(bool(items) for items in episodes.values())
                    or set(rung for rung, items in episodes.items() if items) != set(range(parent["consumed"]))):
                raise ValueError("recurrence checkpoint frozen progression/turnover differs")
            seen = set()
            for rung, items in episodes.items():
                if len(items) > MAX_EPISODES:
                    raise ValueError("checkpoint episode cap exceeded")
                for ordinal, episode in enumerate(items, 1):
                    expected_id = f"parent:{pid}:rung:{rung}:episode:{ordinal}"
                    receipt = out.receipts.get(f"buy:{episode['order_id']}")
                    if (episode["episode_id"] != expected_id or episode["rung"] != rung or episode["ordinal"] != ordinal
                            or episode["renewal"] != (ordinal > 1) or episode["actual_all_in"] <= 0
                            or episode["order_id"] in seen or episode["buy_receipt_id"] != f"fill:{episode['order_id']}"
                            or episode["accepted_order_id"] != episode["order_id"]
                            or episode["proposal_id"] != f"buy:{episode['order_id']}"
                            or episode["reservation_receipt_id"] != episode["proposal_id"]
                            or receipt is None or receipt[0:2] != (pid,parent["pair"])
                            or receipt[4][1:5] != (episode["order_id"],parent["pair"],"RECOVERY",pid)
                            or episode["actual_all_in"] != exact(receipt[4][7])+exact(receipt[4][8])
                            or episode["lot_ids"] != (f"lot:{episode['order_id']}",)
                            or set(episode["remaining"]) != set(episode["lot_ids"])
                            or set(episode["net_profit"]) != set(episode["lot_ids"])
                            or any(any(exact(value) < 0 for value in remaining) for remaining in episode["remaining"].values())
                            or len(episode["sale_receipt_ids"]) != len(episode["release_proof_ids"])):
                        raise ValueError("checkpoint episode/lot/order identity differs")
                    seen.add(episode["order_id"])
                    for sale_id,release_id in zip(episode["sale_receipt_ids"],episode["release_proof_ids"]):
                        sale = out.receipts.get(sale_id)
                        if (sale is None or sale_id != f"sale:{sale[0][1]}"
                                or release_id != f"release:{sale[0][1]}" or sale[0][2:5] != (parent["pair"],"PARTIAL_RECYCLE",pid)
                                or not set(episode["lot_ids"]) & {lot[0] for lot in sale[1]}):
                            raise ValueError("checkpoint sale/release episode proof differs")
                    if episode["eligible_disposal"] and (any(any(exact(value) != 0 for value in remaining)
                        for remaining in episode["remaining"].values()) or any(value <= 0 for value in episode["net_profit"].values())
                        or not episode["sale_receipt_ids"] or episode["disposed_index"] is None):
                        raise ValueError("checkpoint disposal eligibility lacks complete positive receipts")
                    if episode["reset_index"] is not None and (episode["disposed_index"] is None
                            or episode["reset_index"] < episode["disposed_index"]):
                        raise ValueError("checkpoint reset precedes disposal")
                    if episode["crossing_index"] is not None and (episode["reset_index"] is None
                            or episode["crossing_index"] <= episode["reset_index"]):
                        raise ValueError("checkpoint crossing is not a distinct later completed bar")
            expected_pending = {oid: exact(record["maximum_cash"]) for oid, record in out.reservations.items()
                                if record["parent_id"] == pid}
            if parent["pending"] != expected_pending or parent["retired"] and expected_pending:
                raise ValueError("checkpoint pending/retired proof differs")
            for oid,record in out.reservations.items():
                if record["parent_id"] != pid:
                    continue
                rung = record["rung"]
                if (rows is None or rung not in episodes or record["pair"] != parent["pair"]
                        or record["proposal_id"] != f"buy:{oid}" or record["ordinal"] != len(episodes[rung])+1
                        or record["ordinal"] > MAX_EPISODES or record["renewal"] != bool(episodes[rung])
                        or record["maximum_cash"] <= ZERO or not record["renewal"] and rung != parent["consumed"]
                        or record["renewal"] and exact(record["maximum_cash"]) > exact(parent["seed"])*exact(rows[rung][1])):
                    raise ValueError("checkpoint pending episode/rung/reservation differs")
            if parent["retired"]:
                retired = out.receipts.get(parent["retired"])
                if retired is None or retired[0][2:5] != (parent["pair"],"EXIT",pid):
                    raise ValueError("checkpoint whole retirement receipt differs")
            # Replay the preserved controller's Decimal arithmetic, separately
            # from the exact rational lifetime authorization. Wide sums would
            # impose a different whole-parent quantity/basis recipe than v2.
            source_fills = [(entry[6][0],entry[6][1],entry[6])]
            source_fills += [(receipt[4][0],receipt[4][1],receipt[4])
                for key,receipt in out.receipts.items() if key.startswith("buy:") and receipt[0] == pid]
            source_fills += [(receipt[0][0],receipt[0][1],receipt[0])
                for key,receipt in out.receipts.items() if key.startswith("sale:") and receipt[0][4] == pid]
            with localcontext() as context:
                context.prec = 28
                context.rounding = "ROUND_HALF_EVEN"
                quantity,basis = ZERO,ZERO
                for _,_,fill_identity in sorted(source_fills):
                    if fill_identity[3] in {"ENTRY","RECOVERY"}:
                        quantity += fill_identity[5]
                        basis += fill_identity[7] + fill_identity[8]
                    elif fill_identity[3] == "PARTIAL_RECYCLE":
                        quantity -= fill_identity[5]
                        basis -= fill_identity[9]
                    elif fill_identity[3] == "EXIT":
                        if quantity != fill_identity[5] or basis != fill_identity[9]:
                            raise ValueError("checkpoint whole fill differs from native receipt arithmetic")
                        quantity,basis = ZERO,ZERO
                    else:
                        raise ValueError("checkpoint source fill kind is unknown")
            if quantity != parent["native_quantity"] or basis != parent["native_basis"]:
                raise ValueError("checkpoint native parent arithmetic shadow differs")
        if any(record["parent_id"] not in out.parents for record in out.reservations.values()):
            raise ValueError("checkpoint pending parent is unknown")
        return out
