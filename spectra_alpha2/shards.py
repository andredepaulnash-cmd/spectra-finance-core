"""Independent VCAP shard state and fee-aware fill accounting."""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

ZERO = Decimal("0")


class ShardStatus(StrEnum):
    AVAILABLE = "Available"
    RESERVED = "Reserved"
    SUBMITTED = "Submitted"
    FILLED = "Filled"
    ACTIVE = "Active"
    RESOLVED = "Resolved"
    RECYCLED = "Recycled"


OCCUPIES_CONCURRENCY_SLOT = frozenset({
    ShardStatus.RESERVED,
    ShardStatus.SUBMITTED,
    ShardStatus.FILLED,
    ShardStatus.ACTIVE,
})


@dataclass(frozen=True, slots=True)
class VCAPShard:
    shard_id: str
    position_id: str
    asset_id: str
    step_id: str
    generation: int
    authorized_gross_notional: Decimal
    status: ShardStatus = ShardStatus.AVAILABLE
    reservation_id: str | None = None
    order_id: str | None = None
    gross_buy_notional: Decimal = ZERO
    buy_fees_quote: Decimal = ZERO
    filled_quantity: Decimal = ZERO
    remaining_quantity: Decimal = ZERO
    remaining_cost_basis_quote: Decimal = ZERO
    gross_sell_proceeds: Decimal = ZERO
    sell_fees_quote: Decimal = ZERO
    realized_pnl_quote: Decimal = ZERO

    def __post_init__(self) -> None:
        if not all((self.shard_id, self.position_id, self.asset_id, self.step_id)):
            raise ValueError("shard, position, asset and step identifiers are required")
        if isinstance(self.generation, bool) or self.generation < 1:
            raise ValueError("generation must be a positive integer")
        for name in (
            "authorized_gross_notional", "gross_buy_notional", "buy_fees_quote",
            "filled_quantity", "remaining_quantity", "remaining_cost_basis_quote",
            "gross_sell_proceeds", "sell_fees_quote",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        if not isinstance(self.realized_pnl_quote, Decimal) or not self.realized_pnl_quote.is_finite():
            raise ValueError("realized_pnl_quote must be a finite Decimal")
        if self.authorized_gross_notional <= ZERO:
            raise ValueError("authorized_gross_notional must be positive")

    @property
    def occupies_concurrency_slot(self) -> bool:
        return self.status in OCCUPIES_CONCURRENCY_SLOT

    def transition(self, target: ShardStatus, *, reservation_id: str | None = None, order_id: str | None = None) -> "VCAPShard":
        allowed = {
            ShardStatus.AVAILABLE: {ShardStatus.RESERVED},
            ShardStatus.RESERVED: {ShardStatus.SUBMITTED, ShardStatus.AVAILABLE},
            ShardStatus.SUBMITTED: {ShardStatus.FILLED, ShardStatus.AVAILABLE},
            ShardStatus.FILLED: {ShardStatus.ACTIVE},
            ShardStatus.ACTIVE: set(),
            ShardStatus.RESOLVED: {ShardStatus.RECYCLED},
            ShardStatus.RECYCLED: set(),
        }
        if target not in allowed[self.status]:
            raise ValueError(f"invalid shard transition: {self.status} -> {target}")
        if target == ShardStatus.RESERVED and not reservation_id:
            raise ValueError("reservation_id is required when reserving a shard")
        if target == ShardStatus.SUBMITTED and not (order_id or self.order_id):
            raise ValueError("order_id is required when submitting a shard")
        return replace(
            self,
            status=target,
            reservation_id=reservation_id if reservation_id is not None else self.reservation_id,
            order_id=order_id if order_id is not None else self.order_id,
        )

    def record_buy_fill(self, quantity: Decimal, price: Decimal, fee_quote: Decimal) -> "VCAPShard":
        if self.status not in {ShardStatus.SUBMITTED, ShardStatus.FILLED, ShardStatus.ACTIVE}:
            raise ValueError("buy fill requires a submitted or partially filled shard")
        _positive(quantity, "quantity")
        _positive(price, "price")
        _non_negative(fee_quote, "fee_quote")
        notional = quantity * price
        if self.gross_buy_notional + notional > self.authorized_gross_notional:
            raise ValueError("buy fill exceeds authorized shard notional")
        next_status = ShardStatus.FILLED if self.status == ShardStatus.SUBMITTED else self.status
        return replace(
            self,
            status=next_status,
            gross_buy_notional=self.gross_buy_notional + notional,
            buy_fees_quote=self.buy_fees_quote + fee_quote,
            filled_quantity=self.filled_quantity + quantity,
            remaining_quantity=self.remaining_quantity + quantity,
            remaining_cost_basis_quote=self.remaining_cost_basis_quote + notional + fee_quote,
        )

    def activate(self) -> "VCAPShard":
        if self.status != ShardStatus.FILLED:
            raise ValueError("only a filled shard can become active")
        return self.transition(ShardStatus.ACTIVE)

    def record_sell_fill(self, quantity: Decimal, price: Decimal, fee_quote: Decimal) -> "VCAPShard":
        if self.status != ShardStatus.ACTIVE:
            raise ValueError("sell fill requires an active shard")
        _positive(quantity, "quantity")
        _positive(price, "price")
        _non_negative(fee_quote, "fee_quote")
        if quantity > self.remaining_quantity:
            raise ValueError("sell quantity exceeds remaining shard quantity")
        allocated_cost = self.remaining_cost_basis_quote * quantity / self.remaining_quantity
        gross_proceeds = quantity * price
        realized_delta = gross_proceeds - fee_quote - allocated_cost
        remaining_quantity = self.remaining_quantity - quantity
        remaining_cost = self.remaining_cost_basis_quote - allocated_cost
        return replace(
            self,
            status=ShardStatus.RESOLVED if remaining_quantity == ZERO else ShardStatus.ACTIVE,
            remaining_quantity=remaining_quantity,
            remaining_cost_basis_quote=remaining_cost,
            gross_sell_proceeds=self.gross_sell_proceeds + gross_proceeds,
            sell_fees_quote=self.sell_fees_quote + fee_quote,
            realized_pnl_quote=self.realized_pnl_quote + realized_delta,
        )


def _positive(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
        raise ValueError(f"{name} must be a finite positive Decimal")


def _non_negative(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
        raise ValueError(f"{name} must be a finite non-negative Decimal")


def assert_shard_slot_available(asset_id: str, shards: Iterable[VCAPShard]) -> None:
    occupied = sum(1 for shard in shards if shard.asset_id == asset_id and shard.occupies_concurrency_slot)
    from .policy import MAX_ACTIVE_VCAP_SHARDS_PER_ASSET
    if occupied >= MAX_ACTIVE_VCAP_SHARDS_PER_ASSET:
        raise ValueError("per-asset VCAP shard concurrency limit reached")
