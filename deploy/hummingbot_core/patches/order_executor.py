# Spectra modification notice (2026-10-10): fixed LIMIT single-attempt claims,
# durable accounting-only recovery, retained late fills and uncertainty guards.
# Derived from Hummingbot core 9af100d6822da7d2d0291a906c730ef172284ee2.
# Original upstream source and notices remain subject to the Apache-2.0 license.
import asyncio
import json
import logging
import math
from decimal import Decimal
from typing import Dict, List, Optional, Union

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.gateway.gateway_base import GatewayBase
from hummingbot.core.data_type.common import (
    OrderType,
    PositionAction,
    PriceType,
    TradeType,
)
from hummingbot.core.data_type.in_flight_order import InFlightOrder
from hummingbot.core.data_type.order_candidate import (
    OrderCandidate,
    PerpetualOrderCandidate,
)
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)
from hummingbot.logger import HummingbotLogger
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.executors.gateway_utils import (
    is_slippage_failure,
    next_slippage_pct,
)
from hummingbot.strategy_v2.executors.order_executor.data_types import (
    ExecutionStrategy,
    OrderExecutorConfig,
)
from hummingbot.strategy_v2.executors.order_executor.spectra_attempt_guard import (
    AttemptAlreadyClaimed,
    claim_executor,
    find_executor_claim,
    open_executor_evidence,
    read_claim,
)
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder

SPECTRA_EXECUTION_CONTRACT = "spectra-single-attempt-v1"


class OrderExecutor(ExecutorBase):
    SPECTRA_EXECUTION_CONTRACT = "spectra-single-attempt-v1"
    _logger = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(
        self,
        strategy: StrategyV2Base,
        config: OrderExecutorConfig,
        update_interval: float = 1.0,
        max_retries: int = 10,
    ):
        """
        Initialize the OrderExecutor instance.

        :param strategy: The strategy to be used by the OrderExecutor.
        :param config: The configuration for the OrderExecutor.
        :param update_interval: The interval at which the OrderExecutor should be updated, defaults to 1.0.
        :param max_retries: The maximum number of retries for the OrderExecutor, defaults to 10.
        """
        super().__init__(
            strategy=strategy,
            config=config,
            connectors=[config.connector_name],
            update_interval=update_interval,
            max_retries=max_retries,
        )
        self.config: OrderExecutorConfig = config

        # Order tracking
        self._order: Optional[TrackedOrder] = None
        self._failed_orders: list[TrackedOrder] = []
        self._canceled_orders: list[TrackedOrder] = []
        self._partial_filled_orders: list[TrackedOrder] = []
        self._shutdown_ticks: int = 0
        # Fixed LIMIT authorizations are one-shot. A durable claim precedes any
        # transport call and is never released by an order event or executor stop.
        self._spectra_protected = config.execution_strategy in {
            ExecutionStrategy.LIMIT,
            ExecutionStrategy.LIMIT_MAKER,
        }
        self._spectra_authorization = self._spectra_config_scope()
        self._spectra_attempt_claimed = False
        self._spectra_reservation = None
        self._spectra_orders: Dict[str, TrackedOrder] = {}
        self._spectra_quarantine_reason = None
        self._spectra_accounting_unknown = False
        self._spectra_recovered_evidence = None
        self._spectra_recovered_order_ids = []
        self._spectra_recovered_fee_total = Decimal("0")
        self._spectra_tracker_ambiguity = None
        self._spectra_fill_watermarks = {}
        self._spectra_fill_lag_pending = False
        self._spectra_ambiguity_snapshot_payload = None
        # The tolerance the next Gateway swap will carry. Gateway connectors only — a CEX
        # order has a price and a book and nothing to be tolerant about. Widens only on a
        # failure Gateway attributed to slippage, never past max_slippage_pct.
        self._slippage_pct: Decimal = config.slippage_pct

    def _spectra_require_accounting(self):
        self._spectra_observe_tracker_ambiguity()
        if self._spectra_accounting_unknown:
            raise RuntimeError("Durable Spectra accounting requires reconciliation")

    def _spectra_recover_claim(self):
        """Restore accounting evidence only; never restore submission authority."""
        self._spectra_accounting_unknown = True
        try:
            evidence = read_claim(namespace="executor", attempt_id=self.config.id)
            declared_ids = evidence.get("client_order_ids", [])
            if not isinstance(declared_ids, list) or any(
                not isinstance(order_id, str) or not order_id or len(order_id) > 128
                for order_id in declared_ids
            ):
                raise ValueError("Invalid recovered order identities")
            self._spectra_recovered_order_ids = list(dict.fromkeys(declared_ids))
            snapshot = evidence.get("snapshot")
            required_snapshot_fields = {
                "tracked_orders",
                "held_position_orders",
                "executed_amount_base",
                "filled_amount_quote",
                "cum_fees_quote",
                "reconciliation_required",
                "venue_finality_established",
            }
            if (
                not isinstance(snapshot, dict)
                or not required_snapshot_fields <= set(snapshot)
                or not set(snapshot)
                <= required_snapshot_fields
                | {
                    "accounting_available",
                    "accounting_uncertainty_reason",
                    "venue_fill_watermarks",
                }
                or (
                    "accounting_available" in snapshot
                    and type(snapshot["accounting_available"]) is not bool
                )
                or (
                    "accounting_uncertainty_reason" in snapshot
                    and not isinstance(snapshot["accounting_uncertainty_reason"], str)
                )
            ):
                raise ValueError("Missing or invalid durable order snapshot")
            # Bound/validate independently of SQLite JSON decoding. A corrupt
            # snapshot cannot silently become zero exposure or a live order.
            if len(json.dumps(snapshot, allow_nan=False).encode()) > 65536:
                raise ValueError("Recovered snapshot exceeds bounds")
            stack, visited = [(snapshot, 0)], 0
            while stack:
                value, depth = stack.pop()
                visited += 1
                if depth > 24 or visited > 8192:
                    raise ValueError("Recovered snapshot exceeds bounds")
                if isinstance(value, dict):
                    if any(not isinstance(key, str) or len(key) > 128 for key in value):
                        raise ValueError("Invalid recovered snapshot key")
                    stack.extend((item, depth + 1) for item in value.values())
                elif isinstance(value, list):
                    stack.extend((item, depth + 1) for item in value)
                elif isinstance(value, str):
                    if len(value) > 2048:
                        raise ValueError("Invalid recovered snapshot string")
                elif value is None or isinstance(value, bool):
                    pass
                elif isinstance(value, int):
                    if value.bit_length() > 1024:
                        raise ValueError("Invalid recovered snapshot integer")
                elif isinstance(value, float):
                    if not math.isfinite(value):
                        raise ValueError("Invalid recovered snapshot number")
                else:
                    raise ValueError("Unsupported recovered snapshot")
            scope = evidence["scope"]
            if (
                scope["connector_name"] != self.config.connector_name
                or scope["trading_pair"] != self.config.trading_pair
                or scope["side"] != self.config.side.name
                or Decimal(scope["amount"]) != self.config.amount
                or Decimal(scope["price"]) != self.config.price
                or snapshot.get("venue_finality_established") is not False
                or snapshot.get("reconciliation_required") is not True
            ):
                raise ValueError("Recovered evidence does not match authorization")
            serialized = snapshot["tracked_orders"]
            if (
                not isinstance(serialized, list)
                or len(serialized) != 1
                or len(self._spectra_recovered_order_ids) != 1
            ):
                raise ValueError("A single-attempt snapshot must identify its order")
            recovered = {}
            for data in serialized:
                if not isinstance(data, dict):
                    raise ValueError("Invalid recovered order")
                order = InFlightOrder.from_json(data)
                if (
                    order.client_order_id not in self._spectra_recovered_order_ids
                    or order.client_order_id in recovered
                    or order.trading_pair != self.config.trading_pair
                    or order.trade_type != self.config.side
                    or order.amount != self.config.amount
                    or order.order_type not in {OrderType.LIMIT, OrderType.LIMIT_MAKER}
                    or not order.price.is_finite()
                    or order.price <= 0
                    or (
                        self.config.side == TradeType.BUY
                        and order.price > self.config.price
                    )
                    or (
                        self.config.side == TradeType.SELL
                        and order.price < self.config.price
                    )
                    or not order.executed_amount_base.is_finite()
                    or order.executed_amount_base < 0
                    or not order.executed_amount_quote.is_finite()
                    or order.executed_amount_quote < 0
                ):
                    raise ValueError("Recovered order violates authorization")
                for trade_id, trade in order.order_fills.items():
                    if (
                        trade_id != trade.trade_id
                        or trade.client_order_id != order.client_order_id
                        or trade.trading_pair != order.trading_pair
                        or trade.exchange_order_id != order.exchange_order_id
                        or not trade.fill_price.is_finite()
                        or trade.fill_price <= 0
                        or not trade.fill_base_amount.is_finite()
                        or trade.fill_base_amount <= 0
                        or not trade.fill_quote_amount.is_finite()
                        or trade.fill_quote_amount <= 0
                    ):
                        raise ValueError(
                            "Recovered trade identity/quantity is inconsistent"
                        )
                if (
                    sum(
                        (
                            trade.fill_base_amount
                            for trade in order.order_fills.values()
                        ),
                        Decimal("0"),
                    )
                    != order.executed_amount_base
                    or sum(
                        (
                            trade.fill_quote_amount
                            for trade in order.order_fills.values()
                        ),
                        Decimal("0"),
                    )
                    != order.executed_amount_quote
                ):
                    raise ValueError("Recovered fills do not match order totals")
                tracked = TrackedOrder(order_id=order.client_order_id)
                tracked.order = order
                recovered[order.client_order_id] = tracked
            base = sum(
                (tracked.executed_amount_base for tracked in recovered.values()),
                Decimal("0"),
            )
            value = sum(
                (
                    tracked.executed_amount_base * tracked.average_executed_price
                    for tracked in recovered.values()
                ),
                Decimal("0"),
            )
            if base != Decimal(snapshot["executed_amount_base"]) or value != Decimal(
                snapshot["filled_amount_quote"]
            ):
                raise ValueError("Recovered aggregate does not match its trades")
            fee_total = Decimal(snapshot["cum_fees_quote"])
            if not fee_total.is_finite() or fee_total < 0:
                raise ValueError("Invalid recovered fee total")
            held = snapshot["held_position_orders"]
            if not isinstance(held, list) or len(held) != (1 if base > 0 else 0):
                raise ValueError("Recovered held exposure is inconsistent")
            if held and (
                held[0].get("client_order_id") not in recovered
                or Decimal(held[0]["executed_amount_base"]) != base
            ):
                raise ValueError("Recovered held exposure is inconsistent")
            raw_watermarks = snapshot.get("venue_fill_watermarks", {})
            if not isinstance(raw_watermarks, dict) or len(raw_watermarks) > 256:
                raise ValueError("Invalid recovered venue fill watermarks")
            watermarks = {}
            for order_id, raw_value in raw_watermarks.items():
                if order_id not in recovered or not isinstance(raw_value, str):
                    raise ValueError("Recovered watermark does not identify its order")
                watermark = Decimal(raw_value)
                if (
                    not watermark.is_finite()
                    or watermark < 0
                    or format(watermark, "f") != raw_value
                ):
                    raise ValueError("Invalid recovered venue fill watermark")
                watermarks[order_id] = watermark
            pending = any(
                recovered[order_id].executed_amount_base < watermark
                for order_id, watermark in watermarks.items()
            )
            if pending and snapshot.get("accounting_available", True):
                raise ValueError("Lagging recovered trades cannot be available")
            # The reconstructed references are accounting-only: no _order,
            # active tracker registration, resume, or new placement occurs.
            self._spectra_orders.update(recovered)
            self._held_position_orders = held
            self._spectra_recovered_evidence = evidence
            self._spectra_recovered_fee_total = fee_total
            self._spectra_fill_watermarks = watermarks
            self._spectra_fill_lag_pending = pending
            self._spectra_accounting_unknown = not snapshot.get(
                "accounting_available", True
            )
            if self._spectra_accounting_unknown:
                reason = snapshot.get("accounting_uncertainty_reason")
                if reason == "venue_trade_details_pending" and pending:
                    pass  # Reversible lag is separate from sticky identity ambiguity.
                else:
                    self._spectra_tracker_ambiguity = (
                        reason or "durable_accounting_ambiguous"
                    )
            # A verified existing-claim handle can checkpoint accounting only.
            # It cannot create claims, add order identities or restore transport
            # authority, and is opened only after all recovery evidence validates.
            self._spectra_reservation = open_executor_evidence(self.config.id)
        except Exception:
            self._spectra_accounting_unknown = True
            self._spectra_quarantine("durable_accounting_recovery_failed")

    def _spectra_observe_tracker_ambiguity(self):
        if not self._spectra_protected or not self._spectra_attempt_claimed:
            return
        reason = self._spectra_tracker_ambiguity
        tracker = getattr(
            self.connectors.get(self.config.connector_name), "_order_tracker", None
        )
        checker = getattr(tracker, "spectra_order_ambiguity", None)
        if callable(checker):
            for order_id in self._spectra_orders:
                found = checker(order_id)
                if found:
                    reason = found
                    break
        watermark_reader = getattr(tracker, "spectra_order_fill_watermark", None)
        if callable(watermark_reader):
            try:
                for order_id in self._spectra_orders:
                    watermark = watermark_reader(order_id)
                    if watermark is not None:
                        if (
                            not isinstance(watermark, Decimal)
                            or not watermark.is_finite()
                            or watermark < 0
                        ):
                            raise ValueError("Invalid tracked venue fill watermark")
                        self._spectra_fill_watermarks[order_id] = max(
                            watermark,
                            self._spectra_fill_watermarks.get(order_id, Decimal("0")),
                        )
            except Exception:
                reason = "venue_fill_watermark_invalid"
        previous_lag = self._spectra_fill_lag_pending
        tracked_by_id = {
            tracked.order_id: tracked for tracked in self._spectra_tracked_orders()
        }
        self._spectra_fill_lag_pending = any(
            order_id not in tracked_by_id
            or tracked_by_id[order_id].order is None
            or tracked_by_id[order_id].executed_amount_base < watermark
            for order_id, watermark in self._spectra_fill_watermarks.items()
        )
        if not reason and not self._spectra_fill_lag_pending:
            if previous_lag and all(
                tracked.order is not None for tracked in tracked_by_id.values()
            ):
                self._spectra_accounting_unknown = False
                # Availability of actual trade details grants no new execution
                # authority or venue finality, and does not resume RUNNING.
            return
        newly_ambiguous = (
            not self._spectra_accounting_unknown
            or reason != self._spectra_tracker_ambiguity
        )
        if reason:
            self._spectra_tracker_ambiguity = reason
        self._spectra_accounting_unknown = True
        if newly_ambiguous or (self._spectra_fill_lag_pending and not previous_lag):
            self._spectra_quarantine(
                "retained_trade_signature_ambiguous"
                if reason
                else "venue_trade_details_pending"
            )
        unavailable_reason = reason or "venue_trade_details_pending"
        if self._spectra_reservation is not None:
            try:
                orders = self._spectra_tracked_orders()
                base = sum(
                    (tracked.executed_amount_base for tracked in orders), Decimal("0")
                )
                value = sum(
                    (
                        tracked.executed_amount_base * tracked.average_executed_price
                        for tracked in orders
                    ),
                    Decimal("0"),
                )
                fees = sum(
                    (
                        tracked.cum_fees_quote
                        for tracked in orders
                        if tracked.order_id not in self._spectra_recovered_order_ids
                    ),
                    (
                        self._spectra_recovered_fee_total
                        if self._spectra_recovered_order_ids
                        else Decimal("0")
                    ),
                )
                snapshot = {
                    "tracked_orders": [
                        tracked.order.to_json() for tracked in orders if tracked.order
                    ],
                    "held_position_orders": self._collect_held_position_orders(),
                    "executed_amount_base": str(base),
                    "filled_amount_quote": str(value),
                    "cum_fees_quote": str(fees),
                    "reconciliation_required": True,
                    "venue_finality_established": False,
                    "accounting_available": False,
                    "accounting_uncertainty_reason": str(unavailable_reason),
                    "venue_fill_watermarks": {
                        order_id: format(watermark, "f")
                        for order_id, watermark in self._spectra_fill_watermarks.items()
                    },
                }
                payload = json.dumps(
                    snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
                if payload != self._spectra_ambiguity_snapshot_payload:
                    self._spectra_reservation.record_snapshot(snapshot)
                    self._spectra_ambiguity_snapshot_payload = payload
            except Exception:
                self._spectra_quarantine_reason = (
                    "ambiguity_snapshot_persistence_failed"
                )

    def _spectra_check_prior_claim(self) -> bool:
        try:
            existing = find_executor_claim(self.config.id)
        except Exception:
            self._spectra_protected = True
            self._spectra_attempt_claimed = True
            self._spectra_accounting_unknown = True
            self._spectra_quarantine("prior_claim_lookup_unavailable")
            return False
        if existing is not None:
            self._spectra_protected = True
            self._spectra_attempt_claimed = True
            self._spectra_recover_claim()
            self._spectra_quarantine("protected_identity_reuse_requires_reconciliation")
            return False
        return True

    def _spectra_config_scope(self):
        return (
            self.config.id,
            self.config.connector_name,
            self.config.trading_pair,
            self.config.side,
            self.config.amount,
            self.config.price,
            self.config.execution_strategy,
            self.config.position_action,
        )

    def _spectra_tracked_orders(self) -> List[TrackedOrder]:
        unique = {}
        for tracked in (
            list(self._spectra_orders.values())
            + self._partial_filled_orders
            + self._canceled_orders
            + self._failed_orders
            + ([self._order] if self._order else [])
        ):
            if tracked.order_id not in unique or tracked.order is not None:
                unique[tracked.order_id] = tracked
        return list(unique.values())

    def _spectra_refresh_held(self) -> bool:
        try:
            self._held_position_orders = self._collect_held_position_orders()
            return True
        except Exception:
            self._spectra_quarantine_reason = "exposure_snapshot_unavailable"
            self._status = RunnableStatus.SHUTTING_DOWN
            return False

    def _spectra_quarantine(self, reason: str):
        self._spectra_quarantine_reason = reason
        self._status = RunnableStatus.SHUTTING_DOWN
        self._spectra_refresh_held()
        if self._spectra_reservation is not None:
            try:
                self._spectra_reservation.mark_uncertain()
            except Exception:
                # The durable claim remains spent; failure cannot unlock it.
                self._spectra_quarantine_reason = "journal_update_failed"

    def _spectra_record_snapshot(self) -> bool:
        self._spectra_observe_tracker_ambiguity()
        if self._spectra_reservation is None or self._spectra_accounting_unknown:
            return False
        try:
            self._spectra_reservation.record_snapshot(
                {
                    "tracked_orders": [
                        tracked.order.to_json()
                        for tracked in self._spectra_tracked_orders()
                        if tracked.order
                    ],
                    "held_position_orders": self._collect_held_position_orders(),
                    "executed_amount_base": str(self.executed_amount_base),
                    "filled_amount_quote": str(self.filled_amount_quote),
                    "cum_fees_quote": str(self.get_cum_fees_quote()),
                    "reconciliation_required": True,
                    "venue_finality_established": False,
                    "accounting_available": True,
                    "accounting_uncertainty_reason": "",
                    "venue_fill_watermarks": {
                        order_id: format(watermark, "f")
                        for order_id, watermark in self._spectra_fill_watermarks.items()
                    },
                }
            )
            return True
        except Exception:
            self._spectra_quarantine("snapshot_persistence_failed")
            return False

    def _spectra_describe_reservation(self):
        if self._spectra_reservation is None:
            return self._spectra_recovered_evidence
        try:
            return self._spectra_reservation.describe()
        except Exception:
            self._spectra_quarantine("journal_read_failed")
            return None

    def _spectra_place_once(self):
        if self._spectra_attempt_claimed or self.status != RunnableStatus.RUNNING:
            return
        # Latch first, even if claim, storage, config, transport or recording fails.
        self._spectra_attempt_claimed = True
        try:
            if self._spectra_tracked_orders():
                raise ValueError("Preexisting order requires reconciliation")
            if self._spectra_config_scope() != self._spectra_authorization:
                raise ValueError("Fixed LIMIT authorization changed")
            if (
                not isinstance(self.config.amount, Decimal)
                or not self.config.amount.is_finite()
                or self.config.amount <= Decimal("0")
                or not isinstance(self.config.price, Decimal)
                or not self.config.price.is_finite()
                or self.config.price <= Decimal("0")
                or self.config.side not in {TradeType.BUY, TradeType.SELL}
            ):
                raise ValueError("Invalid fixed LIMIT authorization")
            self._spectra_reservation = claim_executor(
                executor_id=self.config.id,
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=self.config.side,
                amount=self.config.amount,
                price=self.config.price,
            )
        except AttemptAlreadyClaimed:
            self._spectra_recover_claim()
            self._spectra_quarantine(
                "claim_already_spent_requires_reconciliation"
                if not self._spectra_accounting_unknown
                else "durable_accounting_recovery_failed"
            )
            return
        except Exception:
            self._spectra_accounting_unknown = True
            self._spectra_quarantine("claim_denied_or_failed")
            return
        try:
            # Gateways perform swaps and can retry internally: they cannot satisfy
            # this CEX fixed-limit contract. No submission is permitted here.
            connector = self.connectors[self.config.connector_name]
            if isinstance(connector, GatewayBase):
                raise ValueError("Fixed LIMIT requires a centralized connector")
            price = self.get_order_price()
            if not price.is_finite() or price <= Decimal("0"):
                raise ValueError("Invalid fixed LIMIT price")
            if (self.config.side == TradeType.BUY and price > self.config.price) or (
                self.config.side == TradeType.SELL and price < self.config.price
            ):
                raise ValueError("Fixed LIMIT price exceeds authorization")
            order_id = self.place_order(
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                order_type=self.get_order_type(),
                amount=self.config.amount,
                price=price,
                side=self.config.side,
                position_action=self.config.position_action,
            )
        except Exception:
            self._spectra_accounting_unknown = True
            self._spectra_quarantine("submission_indeterminate")
            return
        if not isinstance(order_id, str) or not order_id:
            self._spectra_accounting_unknown = True
            self._spectra_quarantine("submission_missing_order_id")
            return
        self._order = TrackedOrder(order_id=order_id)
        self._spectra_orders[order_id] = self._order
        try:
            self._spectra_reservation.record_order(order_id)
            self.update_tracked_order_with_order_id(order_id)
        except Exception:
            if any(tracked.order is None for tracked in self._spectra_tracked_orders()):
                self._spectra_accounting_unknown = True
            self._spectra_quarantine("journal_or_tracking_failed_after_submission")

    # Shutdown ticks (5s apart) tolerated for an order stuck in a state that is
    # neither open nor filled (e.g. the created event never arrived) before the
    # executor force-stops with whatever exposure it knows about.
    MAX_SHUTDOWN_TICKS = 12

    @property
    def current_market_price(self) -> Decimal:
        """
        Get the current market price based on the order side.

        :return: The current market price.
        """
        price_type = (
            PriceType.BestBid
            if self.config.side == TradeType.BUY
            else PriceType.BestAsk
        )
        return self.get_price(
            self.config.connector_name, self.config.trading_pair, price_type=price_type
        )

    @property
    def executed_amount_base(self) -> Decimal:
        """Count each attempted order once, including zero-fill terminal orders."""
        self._spectra_require_accounting()
        return sum(
            (order.executed_amount_base for order in self._spectra_tracked_orders()),
            Decimal("0"),
        )

    @property
    def average_executed_price(self) -> Decimal:
        self._spectra_require_accounting()
        orders = self._spectra_tracked_orders()
        amount = sum((order.executed_amount_base for order in orders), Decimal("0"))
        value = sum(
            (
                order.executed_amount_base * order.average_executed_price
                for order in orders
                if order.executed_amount_base > Decimal("0")
            ),
            Decimal("0"),
        )
        return value / amount if amount > Decimal("0") else Decimal("0")

    @property
    def filled_amount_quote(self) -> Decimal:
        """
        Get the filled amount in quote currency.

        :return: The filled amount in quote currency.
        """
        return self.executed_amount_base * self.average_executed_price

    async def control_task(self):
        """
        Control the order execution process based on the execution strategy.
        """
        self._spectra_observe_tracker_ambiguity()
        if self.status == RunnableStatus.RUNNING:
            self.control_order()
        elif self.status == RunnableStatus.SHUTTING_DOWN:
            await self.control_shutdown_process()

    def control_order(self):
        if self._spectra_protected and self._spectra_attempt_claimed:
            # Losing _order is not authority to submit another venue order.
            return
        if not self._order:
            self.place_open_order()
        elif self.config.execution_strategy == ExecutionStrategy.LIMIT_CHASER:
            self.control_limit_chaser()

    def control_limit_chaser(self):
        """
        Control the limit chaser strategy by updating the order price based on market conditions.
        The distance is treated as a percentage of the current market price.
        """
        if not self._order or not self._order.order or not self._order.order.is_open:
            return
        if self._order.order.is_pending_cancel_confirmation:
            # A renewal is already in flight: wait for the cancel to resolve
            # (the replacement is placed by control_order once _order clears).
            return

        current_price = self.current_market_price
        threshold = self.config.chaser_config.refresh_threshold

        if self.config.side == TradeType.BUY:
            if current_price - self._order.order.price > (current_price * threshold):
                self.renew_order()
        else:  # SELL
            if self._order.order.price - current_price > (current_price * threshold):
                self.renew_order()

    def early_stop(self, keep_position: bool = True):
        """
        This method allows strategy to stop the executor early.

        keep_position=False is NOT supported here: an order executor cannot unwind
        fills (that is a position executor's job), so any filled quantity is always
        terminated as a POSITION_HOLD rather than silently dropped from the store.

        :return: None
        """
        if not keep_position:
            self.logger().warning(
                f"Executor {self.config.id}: keep_position=False is not supported by "
                "OrderExecutor — filled quantity (if any) will be held, not unwound."
            )
        if self._spectra_protected and self._spectra_attempt_claimed:
            self._spectra_quarantine("stop_requires_reconciliation")
            self._spectra_record_snapshot()
        else:
            self._status = RunnableStatus.SHUTTING_DOWN

    def stop(self):
        # Administrative/service shutdown still detaches native callbacks, but
        # checkpoint the last known evidence first. Stop never releases a claim
        # or establishes venue finality; automatic protected completion stays in
        # the accounting-only shutdown state instead of reaching this method.
        if self._spectra_protected and self._spectra_attempt_claimed:
            self._spectra_quarantine("administrative_stop_requires_reconciliation")
            self._spectra_record_snapshot()
        super().stop()

    def _cancel_outstanding_orders(self):
        self.cancel_order()

    def _collect_held_position_orders(self) -> List[Dict]:
        """Refresh known exposure by order ID; a snapshot is not venue finality."""
        held = {
            order.get("client_order_id"): order for order in self._held_position_orders
        }
        for tracked in self._spectra_tracked_orders():
            if tracked.order and tracked.executed_amount_base > Decimal("0"):
                held[tracked.order.client_order_id] = tracked.order.to_json()
        return list(held.values())

    async def control_shutdown_process(self):
        """
        Control the shutdown process of the executor.
        """
        if self._spectra_protected and self._spectra_attempt_claimed:
            # Keep callbacks and original object references alive. A bounded
            # timeout/cancel/completion is not authoritative trades reconciliation.
            self._spectra_refresh_held()
            self._spectra_record_snapshot()
            if (
                self._spectra_tracker_ambiguity is None
                and not self._spectra_fill_lag_pending
                and self.close_type != CloseType.POSITION_HOLD
            ):
                self.cancel_order()
            await self._sleep(5.0)
            return
        if self._order:
            if self._order.is_open:
                self.cancel_order()
            elif self._order.is_filled:
                self.close_type = CloseType.POSITION_HOLD
                self._held_position_orders.append(self._order.order.to_json())
                self._held_position_orders.extend(
                    [order.order.to_json() for order in self._partial_filled_orders]
                )
                self.stop()
            else:
                # Neither open nor filled — usually the created event never arrived
                # (e.g. a stuck placement). Bounded wait, then force-stop with the
                # exposure we know about instead of looping in SHUTTING_DOWN forever.
                self._shutdown_ticks += 1
                if self._shutdown_ticks >= self.MAX_SHUTDOWN_TICKS:
                    self.logger().error(
                        f"Executor {self.config.id}: order {self._order.order_id} stuck in an "
                        "indeterminate state at shutdown; force-stopping with known fills."
                    )
                    self._held_position_orders.extend(
                        [order.order.to_json() for order in self._partial_filled_orders]
                    )
                    self.close_type = (
                        CloseType.POSITION_HOLD
                        if self._held_position_orders
                        else CloseType.FAILED
                    )
                    self.stop()
        else:
            if self._partial_filled_orders:
                self._held_position_orders.extend(
                    [order.order.to_json() for order in self._partial_filled_orders]
                )
                self.close_type = CloseType.POSITION_HOLD
            else:
                self.close_type = CloseType.EARLY_STOP
            self.stop()
        await self._sleep(5.0)

    def place_open_order(self):
        """
        Place the order based on the execution strategy.
        Accounts for previously filled amounts so renewals only order the remaining quantity.
        """
        if self._spectra_protected or self.config.execution_strategy in {
            ExecutionStrategy.LIMIT,
            ExecutionStrategy.LIMIT_MAKER,
        }:
            self._spectra_protected = True
            self._spectra_place_once()
            return
        if not self._spectra_check_prior_claim():
            return
        remaining = self.config.amount - self.executed_amount_base
        if remaining <= Decimal("0"):
            self.logger().info(
                f"Executor {self.config.id}: fully filled ({self.executed_amount_base}/{self.config.amount}). "
                f"No new order needed."
            )
            self._held_position_orders.extend(
                [order.order.to_json() for order in self._partial_filled_orders]
            )
            self.close_type = CloseType.POSITION_HOLD
            self.stop()
            return
        connector = self.connectors[self.config.connector_name]
        if isinstance(connector, GatewayBase):
            # Straight to the connector, because the strategy's buy/sell carries no
            # place for a slippage tolerance and a Gateway order is a swap against a
            # pool. Same call the LP executor's close-out swap makes.
            order_id = connector.place_order(
                is_buy=self.config.side == TradeType.BUY,
                trading_pair=self.config.trading_pair,
                amount=remaining,
                price=self.get_order_price(),
                slippage_pct=float(self._slippage_pct),
                max_retries=self._max_retries,
            )
        else:
            order_id = self.place_order(
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                order_type=self.get_order_type(),
                amount=remaining,
                price=self.get_order_price(),
                side=self.config.side,
                position_action=self.config.position_action,
            )
        self._order = TrackedOrder(order_id=order_id)
        self._spectra_orders[order_id] = self._order
        self.update_tracked_order_with_order_id(order_id)
        self.logger().debug(
            f"Executor ID: {self.config.id} - Placing order {order_id} "
            f"(remaining: {remaining}, filled: {self.executed_amount_base}/{self.config.amount})"
        )

    def get_order_type(self) -> OrderType:
        """
        Get the order type based on the execution strategy.

        :return: The order type.
        """
        if self.config.execution_strategy == ExecutionStrategy.MARKET:
            return OrderType.MARKET
        elif self.config.execution_strategy in [
            ExecutionStrategy.LIMIT_MAKER,
            ExecutionStrategy.LIMIT_CHASER,
        ]:
            return OrderType.LIMIT_MAKER
        else:
            return OrderType.LIMIT

    def get_order_price(self) -> Decimal:
        """
        Get the order price based on the execution strategy.

        :return: The order price.
        """
        if self.config.execution_strategy == ExecutionStrategy.MARKET:
            return Decimal("NaN")
        elif self.config.execution_strategy == ExecutionStrategy.LIMIT_CHASER:
            if self.config.side == TradeType.BUY:
                return self.current_market_price * (
                    Decimal("1") - self.config.chaser_config.distance
                )
            else:
                return self.current_market_price * (
                    Decimal("1") + self.config.chaser_config.distance
                )
        elif self.config.execution_strategy == ExecutionStrategy.LIMIT_MAKER:
            if self.config.side == TradeType.BUY:
                return min(self.config.price, self.current_market_price)
            else:
                return max(self.config.price, self.current_market_price)
        else:
            return self.config.price

    def get_price_for_balance_validation(self) -> Decimal:
        """
        Get the price to use for balance validation.
        For MARKET orders, uses current market price since NaN cannot be used in calculations.

        :return: The price for balance validation.
        """
        if self.config.execution_strategy == ExecutionStrategy.MARKET:
            return self.current_market_price
        return self.get_order_price()

    def renew_order(self):
        """
        Renew the order: request cancellation of the current order and let
        control_order place the replacement once the cancel RESOLVES (the canceled
        event moves the order to the partial/canceled lists and clears self._order).

        Placing the replacement immediately used to reassign self._order before the
        cancel resolved: every subsequent event for the old id failed the identity
        check, so fills-during-cancel vanished from executed_amount_base and the
        replacement was sized without them — re-ordering quantity already bought.
        """
        self.cancel_order()
        self.logger().debug(
            "Renewing order: cancel requested; replacement placed once the cancel resolves"
        )

    def cancel_order(self):
        if self._order and self._order.order and self._order.order.is_open:
            if (
                self._spectra_protected
                and self._order.order.is_pending_cancel_confirmation
            ):
                return
            try:
                self._strategy.cancel(
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    order_id=self._order.order_id,
                )
            except Exception:
                if not self._spectra_protected:
                    raise
                self._spectra_quarantine("cancellation_indeterminate")
                return
            self.logger().debug("Cancelling order")

    def update_tracked_order_with_order_id(self, order_id: str):
        try:
            in_flight_order = self.get_in_flight_order(
                self.config.connector_name, order_id
            )
        except Exception:
            if not self._spectra_protected:
                raise
            if any(tracked.order is None for tracked in self._spectra_tracked_orders()):
                self._spectra_accounting_unknown = True
            self._spectra_quarantine("order_tracking_failed")
            return
        targets = [
            tracked
            for tracked in self._spectra_tracked_orders()
            if tracked.order_id == order_id
        ]
        if (
            self._spectra_protected
            and targets
            and in_flight_order is None
            and any(tracked.order is None for tracked in targets)
        ):
            # Strategy placement returns a client ID before asynchronous tracker
            # registration. An unresolved ID provides no known-zero fill evidence.
            # Keep waiting for its matching callback, with the attempt spent.
            self._spectra_accounting_unknown = True
            if self._spectra_quarantine_reason is None:
                self._spectra_quarantine_reason = "order_reference_unresolved"
            return
        if self._spectra_protected and targets and in_flight_order is not None:
            _, _, pair, side, amount, bound, _, _ = self._spectra_authorization
            if (
                in_flight_order.client_order_id != order_id
                or in_flight_order.trading_pair != pair
                or in_flight_order.trade_type != side
                or in_flight_order.amount != amount
                or in_flight_order.order_type
                not in {OrderType.LIMIT, OrderType.LIMIT_MAKER}
                or not in_flight_order.price.is_finite()
                or in_flight_order.price <= 0
                or (side == TradeType.BUY and in_flight_order.price > bound)
                or (side == TradeType.SELL and in_flight_order.price < bound)
            ):
                self._spectra_accounting_unknown = True
                self._spectra_quarantine("order_reference_scope_mismatch")
                return
        # Never replace a retained object with None after tracker cache expiry.
        if in_flight_order is not None:
            for tracked in self._spectra_tracked_orders():
                if tracked.order_id == order_id:
                    tracked.order = in_flight_order
                    if order_id in self._spectra_recovered_order_ids:
                        self._spectra_recovered_order_ids.remove(order_id)
        if self._spectra_protected:
            if (
                targets
                and in_flight_order is not None
                and self._spectra_reservation is not None
                and all(
                    tracked.order is not None
                    for tracked in self._spectra_tracked_orders()
                )
            ):
                if self._spectra_tracker_ambiguity is None:
                    self._spectra_accounting_unknown = False
                if self._spectra_quarantine_reason == "order_reference_unresolved":
                    self._spectra_quarantine_reason = None
            if self._spectra_quarantine_reason:
                self._spectra_refresh_held()
            self._spectra_record_snapshot()

    def process_order_created_event(
        self, _, market, event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]
    ):
        """
        Process the order created event.
        """
        self.update_tracked_order_with_order_id(event.order_id)

    def process_order_filled_event(self, _, market, event: OrderFilledEvent):
        """
        Process the order filled event.
        """
        self.update_tracked_order_with_order_id(event.order_id)

    def process_order_completed_event(
        self, _, market, event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent]
    ):
        self.update_tracked_order_with_order_id(event.order_id)
        if self._order and self._order.order_id == event.order_id:
            if self._spectra_protected:
                if self._spectra_accounting_unknown:
                    self._spectra_quarantine("completion_requires_reconciliation")
                    return
                if not self._spectra_refresh_held():
                    self._spectra_quarantine("exposure_snapshot_unavailable")
                    return
                known_exact_fill = (
                    self._order.order is not None
                    and self._order.is_filled
                    and self.executed_amount_base == self.config.amount
                    and bool(self._order.order.order_fills)
                )
                if known_exact_fill and self._spectra_record_snapshot():
                    # Known full single-order exposure can be held normally;
                    # journal/custom info still explicitly denies venue finality.
                    self.close_type = CloseType.POSITION_HOLD
                    # The API archives TERMINATED executors once. Keep this
                    # accounting-only until authoritative finality, so later
                    # same-ID fills update both active evidence and the journal.
                    self._spectra_quarantine("full_fill_requires_finality")
                else:
                    self._spectra_quarantine("completion_requires_reconciliation")
            else:
                self._held_position_orders = self._collect_held_position_orders()
                self.close_type = CloseType.POSITION_HOLD
                self.stop()

    def process_order_canceled_event(
        self, _, market: ConnectorBase, event: OrderCancelledEvent
    ):
        self.update_tracked_order_with_order_id(event.order_id)
        if self._order and event.order_id == self._order.order_id:
            if self._order.executed_amount_base > Decimal("0"):
                self._partial_filled_orders.append(self._order)
            else:
                self._canceled_orders.append(self._order)
            self._order = None
            if self._spectra_protected:
                self._spectra_quarantine("cancellation_requires_reconciliation")

    def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
        self.update_tracked_order_with_order_id(event.order_id)
        if self._order and event.order_id == self._order.order_id:
            if self._order.executed_amount_base > Decimal("0"):
                self._partial_filled_orders.append(self._order)
            else:
                self._failed_orders.append(self._order)
            self._order = None
            if self._spectra_protected:
                self._spectra_quarantine("failure_requires_reconciliation")
                return
            self._widen_slippage_if_that_was_it(event.order_id)
            self.logger().error(
                f"Order failed {event.order_id}. Retrying {self._current_retries}/{self._max_retries}"
            )
            self._current_retries += 1

    def _widen_slippage_if_that_was_it(self, order_id: str):
        """Widen the tolerance for the next attempt when slippage is what failed.

        The order tracker records FAILED and no reason, so the reason comes from the
        connector, which keeps what Gateway said. Retrying at a tolerance that has
        already been refused is the behaviour this replaces: ten identical attempts, each
        paying gas if it reached the chain.

        Nothing here applies to a CEX order — a non-Gateway connector remembers no
        reason, so the ramp never moves.
        """
        connector = self.connectors.get(self.config.connector_name)
        reason = getattr(connector, "order_failure_reason", lambda _: None)(order_id)
        if not reason or not is_slippage_failure(Exception(reason)):
            return

        widened = next_slippage_pct(
            self._slippage_pct,
            self.config.slippage_multiplier,
            self.config.max_slippage_pct,
        )
        if widened is None:
            self.logger().warning(
                f"Slippage failure at {self._slippage_pct}%, which is max_slippage_pct "
                f"({self.config.max_slippage_pct}%). Not widening further."
            )
            return

        self.logger().info(
            f"Slippage failure at {self._slippage_pct}%; retrying at {widened}% "
            f"(x{self.config.slippage_multiplier}, ceiling {self.config.max_slippage_pct}%)."
        )
        self._slippage_pct = widened

    def evaluate_max_retries(self):
        """
        Stop after the retry budget, but never discard real exposure: retry
        exhaustion with partial fills terminates POSITION_HOLD carrying them —
        the base FAILED-and-stop would report "nothing left behind" while filled
        quantity sits in the account.
        """
        if self._spectra_protected and self._spectra_attempt_claimed:
            if self._spectra_quarantine_reason:
                self._spectra_refresh_held()
            return
        if self._current_retries > self._max_retries:
            if self._order and self._order.is_filled:
                self._held_position_orders.append(self._order.order.to_json())
            self._held_position_orders.extend(
                [order.order.to_json() for order in self._partial_filled_orders]
            )
            if self._held_position_orders:
                self.close_type = CloseType.POSITION_HOLD
            else:
                self.close_type = CloseType.FAILED
            self.stop()

    def get_custom_info(self) -> Dict:
        """
        Get custom information about the executor.

        :return: A dictionary containing custom information.
        """
        connector = self.connectors.get(self.config.connector_name)
        self._spectra_observe_tracker_ambiguity()
        if self._spectra_protected and self._spectra_attempt_claimed:
            if self._spectra_quarantine_reason:
                self._spectra_refresh_held()
            self._spectra_record_snapshot()
        reservation = self._spectra_describe_reservation()
        return {
            "spectra_execution_contract": self.SPECTRA_EXECUTION_CONTRACT,
            "spectra_single_attempt": self._spectra_protected,
            "spectra_attempt_claimed": self._spectra_attempt_claimed,
            "spectra_attempt_order_ids": list(
                dict.fromkeys(
                    [*self._spectra_orders, *self._spectra_recovered_order_ids]
                )
            ),
            "spectra_accounting_available": not self._spectra_accounting_unknown,
            "spectra_accounting_source": (
                "unavailable"
                if self._spectra_accounting_unknown
                else (
                    "durable_snapshot"
                    if self._spectra_recovered_evidence
                    else "in_process_tracker"
                )
            ),
            "spectra_recovered_attempt_evidence": self._spectra_recovered_evidence,
            "spectra_accounting_uncertainty_reason": (
                self._spectra_tracker_ambiguity
                or (
                    "venue_trade_details_pending"
                    if self._spectra_fill_lag_pending
                    else None
                )
            ),
            "spectra_venue_fill_watermarks": {
                order_id: format(watermark, "f")
                for order_id, watermark in self._spectra_fill_watermarks.items()
            },
            "spectra_reconciliation_required": self._spectra_protected
            and self._spectra_attempt_claimed,
            "spectra_venue_finality_established": False,
            "spectra_quarantine_reason": self._spectra_quarantine_reason,
            "spectra_attempt_reservation": reservation,
            "side": self.config.side,
            "level_id": self.config.level_id,
            "current_retries": self._current_retries,
            "max_retries": self._max_retries,
            "order_id": self._order.order_id if self._order else None,
            "order_last_update": (
                self._order.last_update_timestamp if self._order else None
            ),
            "held_position_orders": self._held_position_orders,
            "executed_amount_base": (
                self.executed_amount_base
                if not self._spectra_accounting_unknown
                else None
            ),
            "average_executed_price": (
                self.average_executed_price
                if not self._spectra_accounting_unknown
                else None
            ),
            "filled_amount_quote": (
                self.filled_amount_quote
                if not self._spectra_accounting_unknown
                else None
            ),
            "cum_fees_quote": (
                self.get_cum_fees_quote()
                if not self._spectra_accounting_unknown
                else None
            ),
            # The LIVE tolerance, not the configured one. A value above
            # config.slippage_pct is the only evidence that attempts already failed and
            # this one is paying to get through — lp_executor reports it for exactly that
            # reason, and without it a widening leaves no trace after the fact.
            "slippage_pct": self._slippage_pct,
            "max_slippage_pct": self.config.max_slippage_pct,
            # On a Gateway swap the connector sets exchange_order_id to the transaction
            # hash. order_id is internal (buy-SOL-USDC-1787271213996599) and appears
            # nowhere on chain, so without this there is no path from an executor record
            # to what it actually did — confirming a swap meant querying the wallet's
            # recent signatures and matching by timestamp.
            "transaction_hash": (
                self._order.order.exchange_order_id
                if self._order and self._order.order
                else None
            ),
            # Who actually executed it, and from which wallet. connector_name names the
            # NETWORK (solana-mainnet-beta); the DEX comes from that network's configured
            # swapProvider and is resolved inside the connector, so this is the only place
            # it can be observed. Both are None off Gateway, where neither applies.
            "swap_provider": getattr(connector, "swap_provider", None),
            "wallet_address": getattr(connector, "address", None),
        }

    def to_format_status(self, scale=1.0):
        """
        Format the status of the executor.

        :param scale: The scale for formatting.
        :return: A list of formatted status lines.
        """
        lines = [f"""
| Trading Pair: {self.config.trading_pair} | Exchange: {self.config.connector_name} | Action: {self.config.position_action}
| Amount: {self.config.amount} | Price: {self._order.order.price if self._order and self._order.order else 'N/A'}
| Execution Strategy: {self.config.execution_strategy} | Retries: {self._current_retries}/{self._max_retries}
"""]
        return lines

    async def validate_sufficient_balance(self):
        # ExecutorBase.on_start invokes this before the first control task. A
        # restart's current budget cannot erase a spent identity's held fills.
        # Verify/recover that identity before native validation can terminate it.
        if self._spectra_attempt_claimed:
            return
        if not self._spectra_check_prior_claim():
            return
        connector = self.connectors[self.config.connector_name]
        # Gateway swap connectors have no order book and are not registered in
        # AllConnectorSettings, so they carry no CEX fee schema. The BudgetChecker /
        # OrderCandidate path raises trying to load that schema, so it cannot be used
        # here. Skip the pre-flight check: Gateway itself rejects an under-funded swap
        # (EVM reverts on gas estimation before submission, Solana fails the quote/sim),
        # and the executor surfaces that failure through its normal retry path. This
        # keeps the OrderExecutor identical across Hummingbot and Hummingbot API without
        # a per-order network round-trip to price the swap.
        if isinstance(connector, GatewayBase):
            return
        price_for_validation = self.get_price_for_balance_validation()
        if self.is_perpetual_connector(self.config.connector_name):
            order_candidate = PerpetualOrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=self.get_order_type().is_limit_type(),
                order_type=self.get_order_type(),
                order_side=self.config.side,
                amount=self.config.amount,
                price=price_for_validation,
                leverage=Decimal(self.config.leverage),
                position_close=self.config.position_action == PositionAction.CLOSE,
            )
        else:
            order_candidate = OrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=self.get_order_type().is_limit_type(),
                order_type=self.get_order_type(),
                order_side=self.config.side,
                amount=self.config.amount,
                price=price_for_validation,
            )
        adjusted_order_candidates = self.adjust_order_candidates(
            self.config.connector_name, [order_candidate]
        )
        if adjusted_order_candidates[0].amount == Decimal("0"):
            self.close_type = CloseType.INSUFFICIENT_BALANCE
            self.logger().error("Not enough budget to open position.")
            self.stop()

    async def _sleep(self, delay: float):
        """
        Sleep for a specified delay.

        :param delay: The delay in seconds.
        """
        await asyncio.sleep(delay)

    def get_net_pnl_pct(self) -> Decimal:
        """
        Get the net profit and loss percentage.

        :return: The net profit and loss percentage.
        """
        return Decimal("0")

    def get_net_pnl_quote(self) -> Decimal:
        """
        Get the net profit and loss in quote currency.

        :return: The net profit and loss in quote currency.
        """
        return Decimal("0")

    def get_cum_fees_quote(self) -> Decimal:
        self._spectra_require_accounting()
        return sum(
            (
                order.cum_fees_quote
                for order in self._spectra_tracked_orders()
                if order.order_id not in self._spectra_recovered_order_ids
            ),
            (
                self._spectra_recovered_fee_total
                if self._spectra_recovered_order_ids
                else Decimal("0")
            ),
        )
