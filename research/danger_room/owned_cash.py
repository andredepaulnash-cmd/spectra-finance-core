"""Receipt-bound cash ownership for the separately frozen paper campaign.

Native execution supplies exact quantity, gross basis and entry/exit fees.
This offline ledger cannot provide exchange terminal proof or live authority.
All methods validate a complete proposed transition before changing state.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import hashlib
import json
from types import MappingProxyType
from typing import Iterable, Mapping

from .module_coordination import Owner, ZERO, _difference, _money, _sum, _text, base_asset


PROTOCOL_SHA256 = "29f5abe77e7ed27aec6a63393411c7e9b0875cec7b1537d85ad5cb13ee9464f9"
PRODUCTIVE_FRACTION = Decimal("0.75")
VCAP_FRACTION = Decimal("0.25")
RESERVE_TARGET = Decimal("0.25")
RESERVE_FLOOR = Decimal("0.20")
BOOTSTRAP_FLOOR = Decimal("40")
ALLOCATION_TOLERANCE = Decimal("1E-18")


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    if not left.is_finite() or not right.is_finite():
        raise ValueError("multiplication requires finite Decimal values")
    with localcontext() as context:
        context.prec = max(28, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 2)
        return left * right


def _positive(value: Decimal, name: str) -> Decimal:
    _money(value, name)
    if value <= ZERO:
        raise ValueError(f"{name} must be positive")
    return value


def all_in_quote(gross_quote: Decimal, entry_fee: Decimal) -> Decimal:
    """Reserve source-confirmed components without rounding a cash claim away."""
    _positive(gross_quote, "gross_quote")
    _money(entry_fee, "entry_fee")
    return _sum((gross_quote, entry_fee))


class OwnerFundingError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Reservation:
    proposal_id: str
    kind: str
    parent_id: str
    pair: str
    owner: Owner
    limit_quote: Decimal
    spent_quote: Decimal = ZERO
    confirmed_parent_id: str | None = None
    terminal_status: str | None = None

    @property
    def remaining(self) -> Decimal:
        return ZERO if self.terminal_status is not None else _difference(self.limit_quote, self.spent_quote)


@dataclass(frozen=True)
class OwnedLot:
    lot_id: str
    owner: Owner
    parent_id: str
    pair: str
    quantity: Decimal
    gross_basis: Decimal
    entry_fees: Decimal

    @property
    def basis(self) -> Decimal:
        return _sum((self.gross_basis, self.entry_fees))


@dataclass(frozen=True)
class SaleAllocation:
    lot_id: str
    quantity: Decimal
    gross_basis_release: Decimal
    entry_fee_release: Decimal
    gross_proceeds: Decimal
    exit_fee: Decimal

    def __post_init__(self) -> None:
        _text(self.lot_id, "lot_id")
        _positive(self.quantity, "disposed quantity")
        _positive(self.gross_basis_release, "disposed gross basis")
        for name in ("entry_fee_release", "gross_proceeds", "exit_fee"):
            _money(getattr(self, name), name)
        if self.exit_fee > self.gross_proceeds:
            raise ValueError("sale fee cannot exceed gross proceeds")


class OwnedCashLedger:
    """One paper quote ledger with immutable source ownership and receipt replay."""

    def __init__(self, initial_cash: Decimal = Decimal("2000")) -> None:
        _positive(initial_cash, "initial_cash")
        self.initial_cash = initial_cash
        productive = _multiply(initial_cash, PRODUCTIVE_FRACTION)
        self._cash = {Owner.PRODUCTIVE: productive, Owner.VCAP: _difference(initial_cash, productive)}
        self._initial_by_owner = dict(self._cash)
        self._realized_by_owner = {Owner.PRODUCTIVE: ZERO, Owner.VCAP: ZERO}
        self._reservations: dict[str, Reservation] = {}
        self._reserve_requests: dict[str, tuple] = {}
        self._receipts: dict[str, tuple] = {}
        self._lots: dict[str, OwnedLot] = {}
        self._ever_lots: set[str] = set()
        self._parked: dict[str, Mapping[Owner, Decimal]] = {}
        self._sale_kinds: dict[str, str] = {}
        self.realized_pnl = ZERO
        self.entry_fees = ZERO
        self.exit_fees = ZERO
        self.assert_invariants()

    @property
    def cash_total(self) -> Decimal:
        return _sum(self._cash.values())

    @property
    def cash_by_owner(self) -> Mapping[Owner, Decimal]:
        return MappingProxyType(dict(self._cash))

    @property
    def lots(self) -> Mapping[str, OwnedLot]:
        return MappingProxyType(dict(self._lots))

    @property
    def journal(self) -> Mapping[str, tuple]:
        return MappingProxyType(dict(self._receipts))

    @property
    def reservations(self) -> Mapping[str, Reservation]:
        return MappingProxyType(dict(self._reservations))

    @property
    def fees_total(self) -> Decimal:
        return _sum((self.entry_fees, self.exit_fees))

    def held(self, owner: Owner | None = None) -> Decimal:
        if owner is not None:
            owner = Owner(owner)
        amounts = [r.remaining for r in self._reservations.values() if owner is None or r.owner == owner]
        for allocation in self._parked.values():
            amounts.extend(value for key, value in allocation.items() if owner is None or key == owner)
        return _sum(amounts)

    def parked(self, owner: Owner | None = None) -> Decimal:
        """Confirmed nondeployable sale proceeds; pending-buy holds are excluded."""
        if owner is not None:
            owner = Owner(owner)
        return _sum(value for allocation in self._parked.values()
                    for key, value in allocation.items() if owner is None or key == owner)

    @property
    def parked_cash_total(self) -> Decimal:
        return self.parked()

    def available(self, owner: Owner, equity_before: Decimal | None = None) -> Decimal:
        owner = Owner(owner)
        if owner not in self._cash:
            return ZERO
        own = _difference(self._cash[owner], self.held(owner))
        if equity_before is None:
            return own
        _money(equity_before, "equity_before")
        protected = max(BOOTSTRAP_FLOOR, _multiply(equity_before, RESERVE_TARGET),
                        _multiply(equity_before, RESERVE_FLOOR))
        physical = max(ZERO, _sum((self.cash_total, self.held().copy_negate(), protected.copy_negate())))
        return min(own, physical)

    def _replay(self, receipt_id: str, payload: tuple) -> bool:
        _text(receipt_id, "receipt_id")
        previous = self._receipts.get(receipt_id)
        if previous is None:
            return False
        if previous != payload:
            raise ValueError("immutable receipt identity replay differs from recorded transaction")
        return True

    def can_reserve(self, *, kind: str, all_in_limit: Decimal, equity_before: Decimal) -> tuple[bool, str]:
        if kind not in {"ENTRY", "RECOVERY"}:
            raise ValueError("cash funding kind must be ENTRY or RECOVERY")
        _positive(all_in_limit, "all_in_limit")
        _money(equity_before, "equity_before")
        owner = Owner.PRODUCTIVE if kind == "ENTRY" else Owner.VCAP
        if all_in_limit > self.available(owner):
            return False, "OWNER_CASH"
        if all_in_limit > self.available(owner, equity_before):
            return False, "PHYSICAL_RESERVE"
        return True, ""

    def reserve_buy(self, proposal_id: str, *, kind: str, parent_id: str, pair: str,
                    all_in_limit: Decimal, equity_before: Decimal) -> Reservation:
        _text(proposal_id, "proposal_id")
        _text(parent_id, "parent_id")
        asset = base_asset(pair)
        _positive(all_in_limit, "all_in_limit")
        _money(equity_before, "equity_before")
        if kind not in {"ENTRY", "RECOVERY"}:
            raise ValueError("cash funding kind must be ENTRY or RECOVERY")
        if asset in {"BTC", "ETH"}:
            raise OwnerFundingError("EXCLUDED_SPOT_ASSET")
        request = (kind, parent_id, pair, all_in_limit, equity_before)
        previous = self._reserve_requests.get(proposal_id)
        if previous is not None:
            if previous != request:
                raise ValueError("reservation identity differs from exact original request")
            return self._reservations[proposal_id]
        allowed, reason = self.can_reserve(kind=kind, all_in_limit=all_in_limit, equity_before=equity_before)
        if not allowed:
            raise OwnerFundingError(reason)
        active_parents = {lot.parent_id for lot in self._lots.values() if base_asset(lot.pair) == asset}
        pending_parents = {r.confirmed_parent_id or r.parent_id for r in self._reservations.values()
                           if r.remaining > ZERO and base_asset(r.pair) == asset}
        if kind == "ENTRY" and (active_parents or pending_parents):
            raise OwnerFundingError("DUPLICATE_ASSET_IDENTITY")
        claimed_assets = {base_asset(lot.pair) for lot in self._lots.values()}
        claimed_assets.update(base_asset(r.pair) for r in self._reservations.values() if r.remaining > ZERO)
        if kind == "ENTRY" and len(claimed_assets) >= 50:
            raise OwnerFundingError("MAXIMUM_SLOTS")
        if kind == "RECOVERY" and active_parents != {parent_id}:
            raise OwnerFundingError("RECOVERY_PARENT_SCOPE")
        if kind == "RECOVERY" and pending_parents:
            raise OwnerFundingError("PENDING_PARENT")
        owner = Owner.PRODUCTIVE if kind == "ENTRY" else Owner.VCAP
        reservation = Reservation(proposal_id, kind, parent_id, pair, owner, all_in_limit)
        self._reserve_requests[proposal_id] = request
        self._reservations[proposal_id] = reservation
        self.assert_invariants()
        return reservation

    def confirm_buy(self, receipt_id: str, proposal_id: str, *, lot_id: str, quantity: Decimal,
                    gross_quote: Decimal, entry_fee: Decimal, confirmed_parent_id: str,
                    equity_before: Decimal) -> OwnedLot | None:
        _text(lot_id, "lot_id")
        _text(confirmed_parent_id, "confirmed_parent_id")
        _positive(quantity, "quantity")
        _positive(gross_quote, "gross_quote")
        _money(entry_fee, "entry_fee")
        _money(equity_before, "equity_before")
        payload = ("BUY", proposal_id, lot_id, quantity, gross_quote, entry_fee, confirmed_parent_id, equity_before)
        if self._replay(receipt_id, payload):
            return self._lots.get(lot_id)
        reservation = self._reservations.get(proposal_id)
        if reservation is None or reservation.terminal_status is not None:
            raise ValueError("buy has no active exact pending reservation")
        if reservation.kind == "RECOVERY" and confirmed_parent_id != reservation.parent_id:
            raise ValueError("recovery fill crosses its economic parent")
        if reservation.confirmed_parent_id is not None and reservation.confirmed_parent_id != confirmed_parent_id:
            raise ValueError("pending entry fill changed its bound parent")
        amount = _sum((gross_quote, entry_fee))
        allowed, reason = self.can_confirm_buy(proposal_id, gross_quote=gross_quote,
                                               entry_fee=entry_fee, equity_before=equity_before)
        if not allowed:
            raise OwnerFundingError(reason)
        previous = self._lots.get(lot_id)
        if previous is None:
            if lot_id in self._ever_lots:
                raise ValueError("retired economic lot identity cannot be reused")
            lot = OwnedLot(lot_id, reservation.owner, confirmed_parent_id, reservation.pair,
                           quantity, gross_quote, entry_fee)
        else:
            if (previous.owner, previous.parent_id, previous.pair) != (reservation.owner, confirmed_parent_id, reservation.pair):
                raise ValueError("buy fill crosses existing lot ownership")
            lot = replace(previous, quantity=_sum((previous.quantity, quantity)),
                          gross_basis=_sum((previous.gross_basis, gross_quote)),
                          entry_fees=_sum((previous.entry_fees, entry_fee)))
        active_parents = {item.parent_id for item in self._lots.values()
                          if base_asset(item.pair) == base_asset(reservation.pair)}
        if active_parents - {confirmed_parent_id}:
            raise ValueError("confirmed buy duplicates another economic asset parent")
        self._cash[reservation.owner] = _difference(self._cash[reservation.owner], amount)
        self._reservations[proposal_id] = replace(reservation, spent_quote=_sum((reservation.spent_quote, amount)),
                                                  confirmed_parent_id=confirmed_parent_id)
        self._lots[lot_id] = lot
        self._ever_lots.add(lot_id)
        self.entry_fees = _sum((self.entry_fees, entry_fee))
        self._receipts[receipt_id] = payload
        self.assert_invariants()
        return lot

    def can_confirm_buy(self, proposal_id: str, *, gross_quote: Decimal, entry_fee: Decimal,
                        equity_before: Decimal) -> tuple[bool, str]:
        """Preflight before native position/quantity mutation; excludes no pending hold."""
        _positive(gross_quote, "gross_quote")
        _money(entry_fee, "entry_fee")
        _money(equity_before, "equity_before")
        reservation = self._reservations.get(proposal_id)
        if reservation is None or reservation.terminal_status is not None:
            return False, "NO_ACTIVE_RESERVATION"
        amount = _sum((gross_quote, entry_fee))
        if amount > reservation.remaining or amount > self._cash[reservation.owner]:
            return False, "CONFIRMED_BUY_OVERSPEND"
        active_parents = {lot.parent_id for lot in self._lots.values()
                          if base_asset(lot.pair) == base_asset(reservation.pair)}
        if reservation.kind == "RECOVERY" and active_parents != {reservation.parent_id}:
            return False, "RECOVERY_PARENT_SCOPE"
        expected_parent = reservation.confirmed_parent_id
        if reservation.kind == "ENTRY" and active_parents and active_parents != {expected_parent}:
            return False, "DUPLICATE_ASSET_IDENTITY"
        # Spending part of a reservation removes that same part from cash and
        # holds. Unfilled remainder remains held, so free cash is unchanged.
        protected = max(BOOTSTRAP_FLOOR, _multiply(equity_before, RESERVE_TARGET),
                        _multiply(equity_before, RESERVE_FLOOR))
        if _difference(self.cash_total, self.held()) < protected:
            return False, "PHYSICAL_RESERVE"
        return True, ""

    def confirm_terminal(self, proof_id: str, proposal_id: str, *, terminal_status: str,
                         exact_zero_remaining: bool) -> Decimal:
        """Release only the unfilled reservation after exact simulated terminal proof."""
        if terminal_status not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}:
            raise ValueError("terminal status is unproven")
        if type(exact_zero_remaining) is not bool or not exact_zero_remaining:
            raise ValueError("open order/queued cancel cannot release its reservation")
        payload = ("TERMINAL", proposal_id, terminal_status, exact_zero_remaining)
        if self._replay(proof_id, payload):
            return ZERO
        reservation = self._reservations.get(proposal_id)
        if reservation is None:
            raise ValueError("terminal proof has no exact reservation")
        if terminal_status == "FILLED" and reservation.spent_quote == ZERO:
            raise ValueError("filled terminal proof has no confirmed cash/inventory receipt")
        if reservation.terminal_status is not None and reservation.terminal_status != terminal_status:
            raise ValueError("terminal proof conflicts with recorded terminal state")
        remaining = reservation.remaining
        self._reservations[proposal_id] = replace(reservation, terminal_status=terminal_status)
        self._receipts[proof_id] = payload
        self.assert_invariants()
        return remaining

    def settle_sale(self, receipt_id: str, allocations: Iterable[SaleAllocation], *,
                    sale_kind: str, deployable: bool,
                    fill_gross_proceeds: Decimal | None = None,
                    fill_exit_fee: Decimal | None = None) -> Mapping[Owner, Decimal]:
        """Settle confirmed lot-specific proceeds, basis and fees exactly once.

        A positive profit is split per disposed lot after its own costs. Loss
        stays with the originating lot owner. Held control proceeds retain the
        same owner and cannot be spent until an explicit partial-release arm.
        """
        items = tuple(allocations)
        if sale_kind not in {"WHOLE", "PARTIAL"} or type(deployable) is not bool:
            raise ValueError("sale needs exact whole/partial kind and deployability")
        if not items or len({item.lot_id for item in items}) != len(items):
            raise ValueError("sale must select distinct exact owned lots")
        gross_total = _sum(item.gross_proceeds for item in items)
        fee_total = _sum(item.exit_fee for item in items)
        if fill_gross_proceeds is not None:
            _money(fill_gross_proceeds, "fill_gross_proceeds")
            if gross_total != fill_gross_proceeds:
                raise ValueError("lot gross proceeds do not sum to exact confirmed fill")
        if fill_exit_fee is not None:
            _money(fill_exit_fee, "fill_exit_fee")
            if fee_total != fill_exit_fee:
                raise ValueError("lot exit fees do not sum to exact confirmed fill")
        quantity_total = _sum(item.quantity for item in items)
        for item in items:
            ratio = Fraction(item.quantity) / Fraction(quantity_total)
            if abs(Fraction(item.gross_proceeds) - Fraction(gross_total) * ratio) > Fraction(ALLOCATION_TOLERANCE):
                raise ValueError("selected sale lots do not share the same confirmed fill price")
            if abs(Fraction(item.exit_fee) - Fraction(fee_total) * ratio) > Fraction(ALLOCATION_TOLERANCE):
                raise ValueError("selected sale lots do not share proportional confirmed exit fees")
        payload = ("SALE", items, sale_kind, deployable, fill_gross_proceeds, fill_exit_fee)
        if self._replay(receipt_id, payload):
            return MappingProxyType({Owner.PRODUCTIVE: ZERO, Owner.VCAP: ZERO})
        cash_added = {Owner.PRODUCTIVE: ZERO, Owner.VCAP: ZERO}
        basis_by_owner = {Owner.PRODUCTIVE: ZERO, Owner.VCAP: ZERO}
        next_lots = dict(self._lots)
        realized = ZERO
        exit_fees = ZERO
        selected_scope = {(self._lots[item.lot_id].parent_id, self._lots[item.lot_id].pair)
                          for item in items if item.lot_id in self._lots}
        if len(selected_scope) != 1:
            raise ValueError("one sale cannot cross exact economic parent/pair scope")
        for allocation in items:
            lot = self._lots.get(allocation.lot_id)
            if lot is None:
                raise ValueError("sale lot is missing, already disposed or not owned")
            if sale_kind == "PARTIAL" and lot.owner != Owner.VCAP:
                raise ValueError("partial recycle can dispose RECOVERY lots only")
            if allocation.quantity > lot.quantity or allocation.gross_basis_release > lot.gross_basis or allocation.entry_fee_release > lot.entry_fees:
                raise ValueError("sale exceeds exact owned quantity/basis/fees")
            if allocation.quantity == lot.quantity:
                if allocation.gross_basis_release != lot.gross_basis or allocation.entry_fee_release != lot.entry_fees:
                    raise ValueError("full lot disposal must release all its exact remaining basis and fees")
            else:
                ratio = Fraction(allocation.quantity) / Fraction(lot.quantity)
                for released, source in ((allocation.gross_basis_release, lot.gross_basis),
                                         (allocation.entry_fee_release, lot.entry_fees)):
                    if abs(Fraction(released) - Fraction(source) * ratio) > Fraction(ALLOCATION_TOLERANCE):
                        raise ValueError("partial lot disposal basis/fees are not source-proportional")
            net = _difference(allocation.gross_proceeds, allocation.exit_fee)
            basis = _sum((allocation.gross_basis_release, allocation.entry_fee_release))
            pnl = _difference(net, basis)
            principal = min(net, basis)
            cash_added[lot.owner] = _sum((cash_added[lot.owner], principal))
            if pnl > ZERO:
                productive_profit = _multiply(pnl, PRODUCTIVE_FRACTION)
                cash_added[Owner.PRODUCTIVE] = _sum((cash_added[Owner.PRODUCTIVE], productive_profit))
                cash_added[Owner.VCAP] = _sum((cash_added[Owner.VCAP], _difference(pnl, productive_profit)))
            basis_by_owner[lot.owner] = _sum((basis_by_owner[lot.owner], basis))
            realized = _sum((realized, pnl))
            exit_fees = _sum((exit_fees, allocation.exit_fee))
            remaining_quantity = _difference(lot.quantity, allocation.quantity)
            if remaining_quantity == ZERO:
                del next_lots[lot.lot_id]
            else:
                remaining_basis = _difference(lot.gross_basis, allocation.gross_basis_release)
                remaining_entry_fees = _difference(lot.entry_fees, allocation.entry_fee_release)
                if _sum((remaining_basis, remaining_entry_fees)) <= ZERO:
                    raise ValueError("remaining positive units cannot lose their entire owned basis")
                next_lots[lot.lot_id] = replace(lot, quantity=remaining_quantity,
                                               gross_basis=remaining_basis, entry_fees=remaining_entry_fees)
        for owner in self._cash:
            self._cash[owner] = _sum((self._cash[owner], cash_added[owner]))
            self._realized_by_owner[owner] = _sum((self._realized_by_owner[owner], cash_added[owner], basis_by_owner[owner].copy_negate()))
        self._lots = next_lots
        self.realized_pnl = _sum((self.realized_pnl, realized))
        self.exit_fees = _sum((self.exit_fees, exit_fees))
        if not deployable:
            self._parked[receipt_id] = MappingProxyType(dict(cash_added))
        self._sale_kinds[receipt_id] = sale_kind
        self._receipts[receipt_id] = payload
        self.assert_invariants()
        return MappingProxyType(cash_added)

    def release_confirmed_partial(self, proof_id: str, sale_receipt_id: str, *, declared_redeploy: bool) -> Mapping[Owner, Decimal]:
        if type(declared_redeploy) is not bool or not declared_redeploy:
            raise ValueError("partial proceeds release is disabled in this control")
        payload = ("RELEASE_PARTIAL", sale_receipt_id, declared_redeploy)
        if self._replay(proof_id, payload):
            return MappingProxyType({Owner.PRODUCTIVE: ZERO, Owner.VCAP: ZERO})
        if self._sale_kinds.get(sale_receipt_id) != "PARTIAL":
            raise ValueError("release does not reference a confirmed partial-sale receipt")
        allocation = self._parked.get(sale_receipt_id)
        if allocation is None:
            raise ValueError("partial proceeds already deployable/released or not recorded")
        del self._parked[sale_receipt_id]
        self._receipts[proof_id] = payload
        self.assert_invariants()
        return allocation

    def assert_invariants(self) -> None:
        if any(not value.is_finite() or value < ZERO for value in self._cash.values()):
            raise AssertionError("negative/nonfinite owned cash")
        for owner in self._cash:
            if self.held(owner) > self._cash[owner]:
                raise AssertionError("held funds exceed their exact owner cash")
            owned_basis = _sum(lot.basis for lot in self._lots.values() if lot.owner == owner)
            lhs = _sum((self._cash[owner], owned_basis))
            rhs = _sum((self._initial_by_owner[owner], self._realized_by_owner[owner]))
            if lhs != rhs:
                raise AssertionError("owner cash plus basis does not conserve its realized allocation")
        if _sum((self.cash_total, *[lot.basis for lot in self._lots.values()])) != _sum((self.initial_cash, self.realized_pnl)):
            raise AssertionError("total cash plus remaining basis is not conserved")
        if _sum(self._realized_by_owner.values()) != self.realized_pnl:
            raise AssertionError("realized owner allocation does not sum to account PnL")
        if any(lot.quantity <= ZERO or lot.gross_basis < ZERO or lot.entry_fees < ZERO for lot in self._lots.values()):
            raise AssertionError("invalid remaining owned lot")
        parents_by_asset: dict[str, set[str]] = {}
        for lot in self._lots.values():
            parents_by_asset.setdefault(base_asset(lot.pair), set()).add(lot.parent_id)
        if any(len(parents) > 1 for parents in parents_by_asset.values()):
            raise AssertionError("duplicate current economic base identity")
        claimed_assets = set(parents_by_asset)
        claimed_assets.update(base_asset(r.pair) for r in self._reservations.values() if r.remaining > ZERO)
        if len(claimed_assets) > 50:
            raise AssertionError("current plus pending identity claims exceed fifty")

    def health(self, equity_before: Decimal) -> dict:
        """Observe target/hard floor without authorizing a deposit or owner transfer."""
        _money(equity_before, "equity_before")
        self.assert_invariants()
        free = _difference(self.cash_total, self.held())
        hard = max(BOOTSTRAP_FLOOR, _multiply(equity_before, RESERVE_FLOOR))
        target = max(hard, _multiply(equity_before, RESERVE_TARGET))
        return {"free_physical_cash": str(free), "hard_floor": str(hard), "target": str(target),
                "hard_floor_ok": free >= hard, "target_ok": free >= target}

    def checkpoint(self) -> dict:
        """Export all replay and owner state. Save its hash in the artifact manifest."""
        self.assert_invariants()
        body = {
            "schema": 1, "protocol_sha256": PROTOCOL_SHA256,
            "initial_cash": str(self.initial_cash),
            "cash": {o.value: str(v) for o, v in self._cash.items()},
            "realized_by_owner": {o.value: str(v) for o, v in self._realized_by_owner.items()},
            "realized_pnl": str(self.realized_pnl), "entry_fees": str(self.entry_fees), "exit_fees": str(self.exit_fees),
            "reservations": _encode(tuple(tuple(getattr(r, f.name) for f in fields(Reservation)) for r in self._reservations.values())),
            "lots": _encode(tuple(tuple(getattr(lot, f.name) for f in fields(OwnedLot)) for lot in self._lots.values())),
            "reserve_requests": _encode(self._reserve_requests), "receipts": _encode(self._receipts),
            "ever_lots": sorted(self._ever_lots), "sale_kinds": dict(self._sale_kinds),
            "parked": {key: {o.value: str(v) for o, v in allocation.items()} for key, allocation in self._parked.items()},
        }
        return {"state": body, "sha256": _checkpoint_hash(body)}

    @classmethod
    def from_checkpoint(cls, checkpoint: dict, *, expected_sha256: str) -> "OwnedCashLedger":
        """Restore a manifest-bound checkpoint; internal hash alone is no provenance."""
        if not isinstance(checkpoint, dict) or set(checkpoint) != {"state", "sha256"}:
            raise ValueError("checkpoint wrapper has unexpected/missing fields")
        body = checkpoint["state"]
        actual = _checkpoint_hash(body)
        if actual != checkpoint["sha256"] or actual != expected_sha256:
            raise ValueError("checkpoint differs from externally bound expected hash")
        if type(body.get("schema")) is not int or body.get("schema") != 1 or body.get("protocol_sha256") != PROTOCOL_SHA256:
            raise ValueError("checkpoint protocol/version does not match the frozen ownership model")
        ledger = cls(Decimal(body["initial_cash"]))
        expected_fields = set(ledger.checkpoint()["state"])
        if set(body) != expected_fields:
            raise ValueError("checkpoint state has unexpected/missing fields")
        ledger._cash = _restore_cash(body["cash"], nonnegative=True)
        ledger._realized_by_owner = _restore_cash(body["realized_by_owner"], nonnegative=False)
        ledger.realized_pnl = _restore_signed(body["realized_pnl"])
        ledger.entry_fees = _money(Decimal(body["entry_fees"]), "restored entry fees")
        ledger.exit_fees = _money(Decimal(body["exit_fees"]), "restored exit fees")
        reservations = tuple(Reservation(*row) for row in _decode(body["reservations"]))
        if len({r.proposal_id for r in reservations}) != len(reservations):
            raise ValueError("restored reservation identities duplicate")
        ledger._reservations = {r.proposal_id: r for r in reservations}
        for r in reservations:
            _text(r.proposal_id, "restored proposal")
            _text(r.parent_id, "restored parent")
            base_asset(r.pair)
            _positive(r.limit_quote, "restored limit")
            _money(r.spent_quote, "restored spent")
            if r.spent_quote > r.limit_quote or r.kind not in {"ENTRY", "RECOVERY"}:
                raise ValueError("restored funding scope/limit is invalid")
            if r.owner != (Owner.PRODUCTIVE if r.kind == "ENTRY" else Owner.VCAP):
                raise ValueError("restored reservation changes its source owner")
            if r.confirmed_parent_id is not None:
                _text(r.confirmed_parent_id, "restored confirmed parent")
            if r.terminal_status not in {None, "FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}:
                raise ValueError("restored terminal status is invalid")
            if r.terminal_status == "FILLED" and r.spent_quote == ZERO:
                raise ValueError("restored filled terminal scope has no confirmed inventory")
        lots = tuple(OwnedLot(*row) for row in _decode(body["lots"]))
        if len({lot.lot_id for lot in lots}) != len(lots):
            raise ValueError("restored lot identities duplicate")
        ledger._lots = {lot.lot_id: lot for lot in lots}
        for lot in lots:
            _text(lot.lot_id, "restored lot")
            _text(lot.parent_id, "restored lot parent")
            base_asset(lot.pair)
            _positive(lot.quantity, "restored quantity")
            _money(lot.gross_basis, "restored basis")
            _money(lot.entry_fees, "restored lot fees")
            if lot.owner not in ledger._cash or lot.basis <= ZERO:
                raise ValueError("restored lot owner/basis is invalid")
        ledger._ever_lots = set(body["ever_lots"])
        if len(ledger._ever_lots) != len(body["ever_lots"]) or not set(ledger._lots).issubset(ledger._ever_lots):
            raise ValueError("restored used-lot registry is incomplete or duplicate")
        for lot_id in ledger._ever_lots:
            _text(lot_id, "restored used lot")
        ledger._reserve_requests = _decode(body["reserve_requests"])
        if set(ledger._reserve_requests) != set(ledger._reservations):
            raise ValueError("restored reservation request journal is incomplete")
        for key, request in ledger._reserve_requests.items():
            r = ledger._reservations[key]
            if len(request) != 5 or request[:4] != (r.kind, r.parent_id, r.pair, r.limit_quote):
                raise ValueError("restored reservation conflicts with exact request journal")
            _money(request[4], "restored request equity")
        ledger._receipts = _decode(body["receipts"])
        for key, payload in ledger._receipts.items():
            _text(key, "restored receipt")
            if not isinstance(payload, tuple) or not payload or payload[0] not in {"BUY", "SALE", "TERMINAL", "RELEASE_PARTIAL"}:
                raise ValueError("restored transaction receipt has unsupported scope")
        ledger._sale_kinds = dict(body["sale_kinds"])
        for key, kind in ledger._sale_kinds.items():
            if key not in ledger._receipts or ledger._receipts[key][0] != "SALE" or kind not in {"WHOLE", "PARTIAL"}:
                raise ValueError("restored sale identity has no exact receipt")
        ledger._parked = {key: MappingProxyType(_restore_cash(value, nonnegative=True)) for key, value in body["parked"].items()}
        if not set(ledger._parked).issubset(ledger._sale_kinds):
            raise ValueError("restored held proceeds have no confirmed sale identity")
        ledger.assert_invariants()
        return ledger

    def snapshot(self) -> dict:
        self.assert_invariants()
        return {
            "protocol_sha256": PROTOCOL_SHA256,
            "cash_total": str(self.cash_total),
            "cash_by_owner": {owner.value: str(amount) for owner, amount in self._cash.items()},
            "held_by_owner": {owner.value: str(self.held(owner)) for owner in self._cash},
            "available_by_owner": {owner.value: str(self.available(owner)) for owner in self._cash},
            "realized_by_owner": {owner.value: str(value) for owner, value in self._realized_by_owner.items()},
            "realized_pnl": str(self.realized_pnl),
            "entry_fees": str(self.entry_fees), "exit_fees": str(self.exit_fees),
            "remaining_basis": str(_sum(lot.basis for lot in self._lots.values())),
            "lots": {key: {"owner": lot.owner.value, "parent_id": lot.parent_id, "pair": lot.pair,
                           "quantity": str(lot.quantity), "gross_basis": str(lot.gross_basis),
                           "entry_fees": str(lot.entry_fees)} for key, lot in self._lots.items()},
            "active_reservation_ids": sorted(key for key, value in self._reservations.items() if value.remaining),
            "held_sale_receipt_ids": sorted(self._parked),
            "parked_cash_total": str(self.parked()),
            "recorded_receipts": len(self._receipts),
        }


def _encode(value):
    if isinstance(value, Decimal):
        return {"D": str(value)}
    if isinstance(value, Owner):
        return {"O": value.value}
    if isinstance(value, SaleAllocation):
        return {"S": [_encode(getattr(value, f.name)) for f in fields(SaleAllocation)]}
    if isinstance(value, tuple):
        return {"T": [_encode(item) for item in value]}
    if isinstance(value, dict):
        return {"M": {key: _encode(item) for key, item in value.items()}}
    if value is None or isinstance(value, (str, bool)):
        return value
    raise ValueError("unsupported checkpoint journal value")


def _decode(value):
    if value is None or isinstance(value, (str, bool)):
        return value
    if not isinstance(value, dict):
        raise ValueError("unsupported encoded checkpoint value")
    if set(value) == {"D"}:
        return _restore_signed(value["D"])
    if set(value) == {"O"}:
        return Owner(value["O"])
    if set(value) == {"S"}:
        return SaleAllocation(*[_decode(item) for item in value["S"]])
    if set(value) == {"T"}:
        return tuple(_decode(item) for item in value["T"])
    if set(value) == {"M"} and isinstance(value["M"], dict):
        return {key: _decode(item) for key, item in value["M"].items()}
    raise ValueError("unsupported encoded checkpoint mapping")


def _restore_signed(value: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("restored money must be an exact decimal string")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("restored money must be finite")
    return result


def _restore_cash(value: dict, *, nonnegative: bool) -> dict[Owner, Decimal]:
    if not isinstance(value, dict) or set(value) != {Owner.PRODUCTIVE.value, Owner.VCAP.value}:
        raise ValueError("restored cash must preserve exact productive/VCAP owners")
    result = {Owner(key): _restore_signed(amount) for key, amount in value.items()}
    if nonnegative and any(amount < ZERO for amount in result.values()):
        raise ValueError("restored cash cannot be negative")
    return result


def _checkpoint_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def allocate_sale_proceeds(lots: Iterable[OwnedLot], *, gross_proceeds: Decimal,
                           exit_fee: Decimal) -> tuple[SaleAllocation, ...]:
    """Allocate one native fill across entire selected lots, final residual last.

    Preserve the caller's source lot order. The first allocations use the
    archived precision-28 HALF_EVEN arithmetic; the last receives the exact
    remainder so neither proceeds nor fees are manufactured or lost.
    """
    selected = tuple(lots)
    _money(gross_proceeds, "gross_proceeds")
    _money(exit_fee, "exit_fee")
    if not selected or len({lot.lot_id for lot in selected}) != len(selected):
        raise ValueError("sale allocation needs distinct source lots")
    if exit_fee > gross_proceeds:
        raise ValueError("fill fee exceeds gross proceeds")
    quantity = _sum(lot.quantity for lot in selected)
    _positive(quantity, "selected quantity")
    allocated_gross = ZERO
    allocated_fee = ZERO
    result = []
    for index, lot in enumerate(selected):
        if index == len(selected) - 1:
            gross = _difference(gross_proceeds, allocated_gross)
            fee = _difference(exit_fee, allocated_fee)
        else:
            with localcontext() as context:
                context.prec = 28
                context.rounding = ROUND_HALF_EVEN
                gross = gross_proceeds * lot.quantity / quantity
                fee = exit_fee * lot.quantity / quantity
        result.append(SaleAllocation(lot.lot_id, lot.quantity, lot.gross_basis, lot.entry_fees, gross, fee))
        allocated_gross = _sum((allocated_gross, gross))
        allocated_fee = _sum((allocated_fee, fee))
    return tuple(result)
