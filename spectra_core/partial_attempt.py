"""Opt-in, read-only feasibility assessment for an existing partial-sale intent.

An eligible assessment is not fill assurance or buy/sell authorization. The
original execution, accounting, reservation and profit protection gates remain
mandatory. A deferred manifest is neither narrowed, completed nor discarded.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from math import isfinite

from .capital_governor import CapitalGovernor
from .economic_position import EconomicPositionLedger
from .kraken_market_fabric import KrakenTradingRule
from .partial_recycle import (
    PartialRecycleManifest,
    PartialRecycleManifestStatus,
    PartialRecycleStore,
    validate_partial_recycle_manifest,
)

ZERO = Decimal(0)
BPS = Decimal(10000)


class PartialAttemptDisposition(StrEnum):
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    DEFER_PRICE = "DEFER_PRICE"
    ELIGIBLE = "ELIGIBLE"


@dataclass(frozen=True)
class PartialAttemptPolicy:
    enabled: bool = False
    max_observation_age_seconds: float = 30.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be bool")
        age = self.max_observation_age_seconds
        if isinstance(age, bool) or not isfinite(float(age)) or float(age) <= 0:
            raise ValueError("max observation age must be finite and positive")
        object.__setattr__(self, "max_observation_age_seconds", float(age))


@dataclass(frozen=True)
class PartialAttemptAssessment:
    manifest_id: str
    disposition: PartialAttemptDisposition
    reasons: tuple[str, ...]
    observed_at: float | None
    quantity: Decimal = ZERO
    basis_quote: Decimal = ZERO
    estimated_gross_quote: Decimal = ZERO
    estimated_net_quote: Decimal = ZERO
    required_net_quote: Decimal = ZERO

    @property
    def permits_dispatch(self) -> bool:
        """No assessment confers execution or capital authority."""
        return False


def assess_partial_attempt(
    *,
    manifest: PartialRecycleManifest,
    ledger: EconomicPositionLedger,
    governor: CapitalGovernor,
    store: PartialRecycleStore,
    trading_rule: KrakenTradingRule,
    current_price: Decimal,
    observed_at: float | None,
    now: float,
    expected_exit_fee_bps: Decimal,
    slippage_allowance_bps: Decimal,
    target_net_profit_bps: Decimal,
    pending_order_ids: tuple[str, ...] | None = None,
    policy: PartialAttemptPolicy | None = None,
) -> PartialAttemptAssessment:
    """Assess only the exact still-open manifest using current known evidence.

    Callers must provide a complete exact-position pending-order observation;
    unknown is blocked, not assumed empty. This is a synchronous planning
    snapshot only. Dispatch must recheck pending orders and ownership under
    its existing mutation boundary, including after await/restart.
    """
    config = policy or PartialAttemptPolicy()

    def result(disposition, *reasons, **geometry):
        return PartialAttemptAssessment(
            manifest.manifest_id, disposition, tuple(reasons), observed_at, **geometry
        )

    if not config.enabled:
        return result(
            PartialAttemptDisposition.DISABLED, "partial_attempt_gate_disabled"
        )
    if pending_order_ids is None:
        return result(
            PartialAttemptDisposition.BLOCKED, "pending_order_evidence_missing"
        )
    if not isinstance(pending_order_ids, tuple) or any(
        not isinstance(value, str) or not value.strip() for value in pending_order_ids
    ):
        return result(
            PartialAttemptDisposition.BLOCKED, "pending_order_evidence_invalid"
        )
    if pending_order_ids:
        return result(
            PartialAttemptDisposition.BLOCKED, "existing_position_order_pending"
        )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
        for value in (now, observed_at)
    ):
        return result(
            PartialAttemptDisposition.BLOCKED, "price_observation_time_invalid"
        )
    if observed_at > now or now - observed_at > config.max_observation_age_seconds:
        return result(
            PartialAttemptDisposition.BLOCKED, "price_observation_missing_or_stale"
        )
    if (
        not isinstance(current_price, Decimal)
        or not current_price.is_finite()
        or current_price <= ZERO
    ):
        return result(PartialAttemptDisposition.BLOCKED, "price_observation_invalid")
    costs = (expected_exit_fee_bps, slippage_allowance_bps, target_net_profit_bps)
    if (
        any(
            not isinstance(value, Decimal) or not value.is_finite() or value < ZERO
            for value in costs
        )
        or sum(costs[:2], ZERO) >= BPS
    ):
        return result(PartialAttemptDisposition.BLOCKED, "cost_policy_invalid")
    current = store.open_for_position(manifest.position_id)
    if current != manifest or manifest.status != PartialRecycleManifestStatus.OPEN:
        return result(
            PartialAttemptDisposition.BLOCKED, "manifest_not_current_open_intent"
        )
    if trading_rule.trading_pair.strip().upper() != manifest.pair:
        return result(PartialAttemptDisposition.BLOCKED, "trading_rule_pair_changed")
    rules = (
        trading_rule.min_order_size,
        trading_rule.effective_min_quote,
        trading_rule.min_base_amount_increment,
    )
    if (
        any(
            not isinstance(value, Decimal) or not value.is_finite() or value < ZERO
            for value in rules
        )
        or rules[-1] <= ZERO
    ):
        return result(PartialAttemptDisposition.BLOCKED, "trading_rule_invalid")
    if not trading_rule.supports_market_orders:
        return result(PartialAttemptDisposition.BLOCKED, "market_order_not_supported")
    if (
        not manifest.owners
        or not manifest.lot_ids
        or len(set(manifest.lot_ids)) != len(manifest.lot_ids)
    ):
        return result(PartialAttemptDisposition.BLOCKED, "manifest_scope_invalid")
    try:
        validate_partial_recycle_manifest(manifest, ledger=ledger, governor=governor)
    except (RuntimeError, ValueError):
        return result(
            PartialAttemptDisposition.BLOCKED, "native_manifest_validation_failed"
        )
    scope = None
    for owner in manifest.owners:
        provenance = governor.get_execution_provenance(owner.proposal_id)
        if (
            provenance is None
            or provenance.pair.strip().upper() != manifest.pair
            or not provenance.account_name
            or not provenance.connector_name
        ):
            return result(
                PartialAttemptDisposition.BLOCKED, "manifest_owner_scope_invalid"
            )
        owner_scope = (provenance.account_name, provenance.connector_name.lower())
        if scope is not None and scope != owner_scope:
            return result(
                PartialAttemptDisposition.BLOCKED, "manifest_owner_scope_changed"
            )
        scope = owner_scope
    active = {lot.lot_id: lot for lot in ledger.list_active_lots(manifest.position_id)}
    # Do not silently drop a consumed owner from the immutable executor scope.
    if any(
        lot_id not in active or active[lot_id].base_remaining <= ZERO
        for lot_id in manifest.lot_ids
    ):
        return result(
            PartialAttemptDisposition.BLOCKED, "selected_lot_requires_reconciliation"
        )
    lots = [active[lot_id] for lot_id in manifest.lot_ids]
    if any(
        not value.is_finite() or value < ZERO
        for lot in lots
        for value in (
            lot.base_remaining,
            lot.cost_basis_remaining_quote,
            lot.entry_fees_remaining_quote,
        )
    ):
        return result(
            PartialAttemptDisposition.BLOCKED, "selected_inventory_geometry_invalid"
        )
    for owner in manifest.owners:
        owner_lots = [active[lot_id] for lot_id in owner.lot_ids]
        if (
            sum((lot.cost_basis_remaining_quote for lot in owner_lots), ZERO)
            > owner.target_cost_basis_quote
            or sum((lot.entry_fees_remaining_quote for lot in owner_lots), ZERO)
            > owner.target_entry_fees_quote
        ):
            return result(
                PartialAttemptDisposition.BLOCKED, "selected_owner_basis_increased"
            )
    quantity = sum((lot.base_remaining for lot in lots), ZERO)
    basis = sum(
        (
            lot.cost_basis_remaining_quote + lot.entry_fees_remaining_quote
            for lot in lots
        ),
        ZERO,
    )
    if (
        not quantity.is_finite()
        or not basis.is_finite()
        or quantity <= ZERO
        or basis < ZERO
    ):
        return result(
            PartialAttemptDisposition.BLOCKED, "selected_inventory_geometry_invalid"
        )
    step = trading_rule.min_base_amount_increment
    rounded = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
    gross = quantity * current_price * (Decimal(1) - slippage_allowance_bps / BPS)
    # Match the native planner's conservative additive cost convention. The
    # research paper executor's compounded fee convention is not imported.
    net = (
        quantity
        * current_price
        * (Decimal(1) - (slippage_allowance_bps + expected_exit_fee_bps) / BPS)
    )
    required = basis * (Decimal(1) + target_net_profit_bps / BPS)
    geometry = dict(
        quantity=quantity,
        basis_quote=basis,
        estimated_gross_quote=gross,
        estimated_net_quote=net,
        required_net_quote=required,
    )
    if rounded != quantity:
        return result(
            PartialAttemptDisposition.BLOCKED,
            "quantity_rounding_requires_explicit_reconciliation",
            **geometry,
        )
    if quantity < max(manifest.min_order_size, trading_rule.min_order_size):
        return result(
            PartialAttemptDisposition.BLOCKED,
            "base_minimum_requires_explicit_repair",
            **geometry,
        )
    reasons = []
    if gross < max(manifest.min_notional_quote, trading_rule.effective_min_quote):
        reasons.append("current_price_quote_minimum")
    if net < required:
        reasons.append("current_price_net_profit_floor")
    return result(
        (
            PartialAttemptDisposition.DEFER_PRICE
            if reasons
            else PartialAttemptDisposition.ELIGIBLE
        ),
        *(reasons or ["current_evidence_passes_not_fill_guarantee"]),
        **geometry,
    )
