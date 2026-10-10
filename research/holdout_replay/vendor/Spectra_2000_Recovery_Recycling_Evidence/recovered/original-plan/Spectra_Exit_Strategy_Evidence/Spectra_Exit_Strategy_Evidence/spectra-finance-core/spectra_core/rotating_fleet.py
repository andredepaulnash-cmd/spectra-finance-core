"""Causal shared-account replay for a continuously rotating altcoin fleet.

The engine consumes recorded one-minute OHLCV bars.  Orders decided after one
bar participate in the following bar at recorded VWAP plus an explicit slippage
allowance, subject to a conservative fraction of that bar's recorded volume.
That is an estimated execution model, not a bid/ask or order-book replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from pathlib import Path

from .asset_fingerprint import (
    AssetFingerprint,
    AssetObservation,
    FingerprintHorizon,
)
from .binance_kline_import import KlineDataset, MinuteBar, load_archives
from .kraken_market_fabric import KrakenTradingRule
from .models import CapitalProposal, IntentKind
from .recovery_fingerprint import (
    RecoveryFingerprintPolicy,
    VCAPPositionState,
    VCAPRecoveryPolicy,
    plan_vcap_recovery,
)
from .strategy_lab import base_asset
from .vcap_profit_exit import (
    VCAPExitCostModel,
    VCAPProfitExitAction,
    VCAPProfitExitPolicy,
    plan_vcap_profit_exit,
)
from .vcap_tranches import VCAPTranchePolicy, plan_vcap_tranches


D = Decimal
ZERO = D(0)
ONE = D(1)
BPS = D(10000)


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value: object, field: str, *, positive: bool = False) -> D:
    result = D(str(value))
    if not result.is_finite() or result < ZERO or (positive and result <= ZERO):
        raise ValueError(f"{field} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


@dataclass(frozen=True)
class FleetConfig:
    initial_cash: D = D("2000")
    target_positions: int = 50
    entry_budget: D = D("10.25")
    minimum_position_basis: D = D("10")
    # The production fingerprint may throttle a cold/stale request to 25%.
    # Forty quote units therefore yields a $10 executable recovery floor while
    # the pair cap still owns the hard maximum.
    recovery_seed_quote: D = D("40")
    recovery_trigger: D = D("0.03")
    reserve_fraction: D = D("0.25")
    pair_cap_fraction: D = D("0.05")
    fee_bps: D = D("40")
    slippage_bps: D = D("5")
    volume_participation: D = D("0.01")
    simulated_min_order_quote: D = D("5")
    simulated_base_step: D = D("0.000000000001")
    replacement_candidates_per_exit: int = 5

    def __post_init__(self) -> None:
        for field in (
            "initial_cash",
            "entry_budget",
            "minimum_position_basis",
            "recovery_seed_quote",
            "recovery_trigger",
            "reserve_fraction",
            "pair_cap_fraction",
            "fee_bps",
            "slippage_bps",
            "volume_participation",
            "simulated_min_order_quote",
            "simulated_base_step",
        ):
            object.__setattr__(
                self,
                field,
                _number(
                    getattr(self, field),
                    field,
                    positive=field
                    in {
                        "initial_cash",
                        "entry_budget",
                        "minimum_position_basis",
                        "recovery_seed_quote",
                        "volume_participation",
                        "simulated_min_order_quote",
                        "simulated_base_step",
                    },
                ),
            )
        if type(self.target_positions) is not int or self.target_positions < 1:
            raise ValueError("target_positions must be a positive integer")
        if (
            type(self.replacement_candidates_per_exit) is not int
            or self.replacement_candidates_per_exit < 1
        ):
            raise ValueError("replacement_candidates_per_exit must be positive")
        if self.entry_budget < self.minimum_position_basis:
            raise ValueError("entry_budget must create at least the minimum position basis")
        if self.minimum_position_basis < self.simulated_min_order_quote:
            raise ValueError("minimum position must meet the simulated order minimum")
        if self.fee_bps + self.slippage_bps >= BPS:
            raise ValueError("execution costs must be below 10000 bps")
        for field in (
            "recovery_trigger",
            "reserve_fraction",
            "pair_cap_fraction",
            "volume_participation",
        ):
            if getattr(self, field) > ONE:
                raise ValueError(f"{field} must be <= 1")
        required = self.entry_budget * self.target_positions
        spendable = self.initial_cash * (ONE - self.reserve_fraction)
        if required > spendable:
            raise ValueError("cash outside reserve cannot seed the requested fleet")


class FleetExitMode(StrEnum):
    """Mutually exclusive profit-harvest rules for controlled comparisons."""

    DTTP = "dttp"
    HARD_TARGET = "hard_target"
    EARNED_DTTP = "earned_dttp"


@dataclass(frozen=True)
class FleetExitStrategy:
    """Causal gate that decides whether a reached profit floor may trail."""

    mode: FleetExitMode = FleetExitMode.DTTP
    qualifying_win_bps: D = D("124")
    wins_required: int = 3
    win_window_seconds: int = 12 * 60 * 60
    activation_seconds: int = 6 * 60 * 60
    extension_continuation_threshold: float = 0.60
    continuation_exit_threshold: float = 0.50

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", FleetExitMode(self.mode))
        object.__setattr__(
            self,
            "qualifying_win_bps",
            _number(self.qualifying_win_bps, "qualifying_win_bps", positive=True),
        )
        for field in ("wins_required", "win_window_seconds", "activation_seconds"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        for field in (
            "extension_continuation_threshold",
            "continuation_exit_threshold",
        ):
            value = getattr(self, field)
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{field} must be in [0, 1]")
        if (
            self.continuation_exit_threshold
            > self.extension_continuation_threshold
        ):
            raise ValueError(
                "continuation exit threshold cannot exceed extension threshold"
            )


@dataclass(frozen=True)
class Position:
    position_id: int
    pair: str
    quantity: D
    basis: D
    opened_at: int
    recovery_count: int = 0


@dataclass(frozen=True)
class FleetOrder:
    order_id: int
    submitted_at: int
    pair: str
    kind: str
    budget: D = ZERO
    position_id: int | None = None
    depends_on_order_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"ENTRY", "RECOVERY", "EXIT"}:
            raise ValueError("unknown fleet order kind")
        if self.kind == "ENTRY" and self.position_id is not None:
            raise ValueError("entry cannot name an existing position")
        if self.kind != "ENTRY" and self.position_id is None:
            raise ValueError("recovery/exit must name an exact position identity")
        if self.kind == "EXIT" and self.budget != ZERO:
            raise ValueError("exit cannot carry a quote budget")
        if self.kind != "EXIT" and self.budget <= ZERO:
            raise ValueError("buy order requires a positive all-in budget")


@dataclass(frozen=True)
class FleetFill:
    timestamp: int
    order_id: int
    pair: str
    kind: str
    position_id: int
    quantity: D
    price: D
    gross_quote: D
    fee_quote: D
    basis_released: D
    realized_pnl: D
    dependency_order_id: int | None


@dataclass(frozen=True)
class FleetSample:
    timestamp: int
    cash: D
    committed_basis: D
    marked_value: D
    equity: D
    active_positions: int
    unique_base_assets: int


@dataclass(frozen=True)
class FleetResult:
    config: FleetConfig
    exit_strategy: FleetExitStrategy
    exit_policy: VCAPProfitExitPolicy
    dataset_manifest: dict
    cash: D
    positions: tuple[Position, ...]
    fills: tuple[FleetFill, ...]
    samples: tuple[FleetSample, ...]
    rejections: dict[str, int]
    rejection_examples: tuple[dict, ...]
    metrics: dict
    fingerprint: str


class RotatingFleetSimulator:
    """One shared cash ledger and at most one economic position per base asset."""

    def __init__(
        self,
        config: FleetConfig | None = None,
        *,
        fingerprint_policy: RecoveryFingerprintPolicy | None = None,
        recovery_policy: VCAPRecoveryPolicy | None = None,
        tranche_policy: VCAPTranchePolicy | None = None,
        exit_policy: VCAPProfitExitPolicy | None = None,
        exit_strategy: FleetExitStrategy | None = None,
    ) -> None:
        self.config = config or FleetConfig()
        self.fingerprint_policy = fingerprint_policy or RecoveryFingerprintPolicy()
        self.recovery_policy = recovery_policy or VCAPRecoveryPolicy()
        self.tranche_policy = tranche_policy or VCAPTranchePolicy()
        self.exit_policy = exit_policy or VCAPProfitExitPolicy()
        self.exit_strategy = exit_strategy or FleetExitStrategy()
        if (
            self.exit_strategy.mode == FleetExitMode.EARNED_DTTP
            and self.exit_policy.target_net_profit_bps
            != self.exit_strategy.qualifying_win_bps
        ):
            raise ValueError(
                "earned-DTTP target and qualifying-win threshold must match"
            )

    def run(self, dataset: KlineDataset) -> FleetResult:
        cfg = self.config
        pairs = dataset.pairs
        if len(pairs) <= cfg.target_positions:
            raise ValueError("universe must contain at least one inactive replacement asset")
        if dataset.frames < 3:
            raise ValueError("at least three synchronized frames required")
        if not dataset.manifest.get("complete_synchronized_grid"):
            raise ValueError("fleet replay requires a complete synchronized grid")
        bases = tuple(base_asset(pair) for pair in pairs)
        if len(set(bases)) != len(bases):
            raise ValueError("duplicate normalized base asset in universe")
        if any(pair.split("-")[1] != "USDT" for pair in pairs):
            raise ValueError("one explicit quote ledger is required")

        cash = cfg.initial_cash
        positions: dict[str, Position] = {}
        pending: list[FleetOrder] = []
        fills: list[FleetFill] = []
        samples: list[FleetSample] = []
        rejection_counts: Counter[str] = Counter()
        rejection_examples: list[dict] = []
        fingerprints: dict[str, AssetFingerprint] = {}
        profit_states = {}
        qualifying_wins: dict[str, list[int]] = {pair: [] for pair in pairs}
        earned_dttp_until: dict[str, int] = {}
        dttp_extended_positions: set[int] = set()
        ever_dttp_extended_positions: set[int] = set()
        earned_dttp_assets: set[str] = set()
        exit_signal_reasons: Counter[str] = Counter()
        volume_24h = {pair: ZERO for pair in pairs}
        next_order_id = 0
        next_position_id = 0
        realized = ZERO
        fees = ZERO
        turnover = ZERO
        peak_committed = ZERO
        initialized_index: int | None = None
        successful_dependencies = 0
        replacement_recycle_fills = 0
        planner_exit_signals = 0
        planner_recovery_signals = 0
        hard_target_exit_signals = 0
        earned_dttp_activation_events = 0
        earned_dttp_deactivation_events = 0
        earned_dttp_extension_decisions = 0
        qualifying_hard_wins = 0
        entry_basis_observed: list[D] = []
        decision_digest = hashlib.sha256()
        fee_rate = cfg.fee_bps / BPS
        slip = cfg.slippage_bps / BPS
        exit_costs = VCAPExitCostModel(
            expected_exit_fee_bps=cfg.fee_bps,
            slippage_allowance_bps=cfg.slippage_bps,
        )

        def reject(index: int, order: FleetOrder, reason: str) -> None:
            rejection_counts[reason] += 1
            if len(rejection_examples) < 100:
                rejection_examples.append(
                    {
                        "timestamp": dataset.series[order.pair][index].timestamp + 60,
                        "order_id": order.order_id,
                        "pair": order.pair,
                        "kind": order.kind,
                        "reason": reason,
                    }
                )

        def next_order(
            timestamp: int,
            pair: str,
            kind: str,
            *,
            budget: D = ZERO,
            position_id: int | None = None,
            depends_on: int | None = None,
        ) -> FleetOrder:
            nonlocal next_order_id
            next_order_id += 1
            return FleetOrder(
                next_order_id,
                timestamp,
                pair,
                kind,
                budget,
                position_id,
                depends_on,
            )

        for index in range(dataset.frames):
            bars = {pair: dataset.series[pair][index] for pair in pairs}
            timestamp = next(iter(bars.values())).timestamp + 60
            closed_this_frame: set[str] = set()
            successful_orders: set[int] = set()
            replaced_dependencies: set[int] = set()
            released_by_exit: dict[int, D] = {}

            # Orders were submitted after the prior bar and participate through
            # this complete minute.  Sells settle before dependent replacements.
            due = sorted(pending, key=lambda order: order.kind != "EXIT")
            pending = []
            for order in due:
                bar = bars[order.pair]
                if (
                    order.depends_on_order_id is not None
                    and order.depends_on_order_id not in successful_orders
                ):
                    reject(index, order, "dependency_not_filled")
                    continue
                if (
                    order.depends_on_order_id is not None
                    and order.depends_on_order_id in replaced_dependencies
                ):
                    reject(index, order, "replacement_fallback_not_needed")
                    continue
                vwap = bar.vwap
                if vwap is None or bar.quote_volume <= ZERO:
                    reject(index, order, "zero_recorded_volume")
                    continue
                capacity = bar.quote_volume * cfg.volume_participation
                position = positions.get(order.pair)
                if order.kind == "EXIT":
                    if position is None or position.position_id != order.position_id:
                        reject(index, order, "stale_position_identity")
                        continue
                    price = vwap * (ONE - slip)
                    gross = position.quantity * price
                    if gross > capacity:
                        reject(index, order, "volume_participation_fok")
                        continue
                    fee = gross * fee_rate
                    proceeds = gross - fee
                    pnl = proceeds - position.basis
                    cash += proceeds
                    realized += pnl
                    fees += fee
                    turnover += gross
                    fills.append(
                        FleetFill(
                            timestamp,
                            order.order_id,
                            order.pair,
                            order.kind,
                            position.position_id,
                            position.quantity,
                            price,
                            gross,
                            fee,
                            position.basis,
                            pnl,
                            None,
                        )
                    )
                    if self.exit_strategy.mode == FleetExitMode.EARNED_DTTP:
                        was_extended = position.position_id in dttp_extended_positions
                        dttp_extended_positions.discard(position.position_id)
                        active = earned_dttp_until.get(order.pair, 0) > timestamp
                        realized_bps = (
                            pnl / position.basis * BPS
                            if position.basis > ZERO
                            else ZERO
                        )
                        # Only completed hard-target wins earn DTTP.  A trade
                        # already extended by DTTP cannot recursively re-arm it.
                        if (
                            not was_extended
                            and not active
                            and realized_bps
                            >= self.exit_strategy.qualifying_win_bps
                        ):
                            qualifying_hard_wins += 1
                            cutoff = (
                                timestamp
                                - self.exit_strategy.win_window_seconds
                            )
                            history = [
                                won_at
                                for won_at in qualifying_wins[order.pair]
                                if won_at >= cutoff
                            ]
                            history.append(timestamp)
                            if len(history) >= self.exit_strategy.wins_required:
                                earned_dttp_until[order.pair] = (
                                    timestamp
                                    + self.exit_strategy.activation_seconds
                                )
                                history = []
                                earned_dttp_activation_events += 1
                                earned_dttp_assets.add(order.pair)
                            qualifying_wins[order.pair] = history
                    del positions[order.pair]
                    profit_states.pop(order.pair, None)
                    closed_this_frame.add(order.pair)
                    successful_orders.add(order.order_id)
                    released_by_exit[order.order_id] = proceeds
                    continue

                if order.kind == "ENTRY":
                    if position is not None:
                        reject(index, order, "duplicate_active_asset")
                        continue
                elif position is None or position.position_id != order.position_id:
                    reject(index, order, "stale_position_identity")
                    continue
                gross = order.budget / (ONE + fee_rate)
                if gross < cfg.simulated_min_order_quote:
                    reject(index, order, "simulated_order_minimum")
                    continue
                if gross > capacity:
                    reject(index, order, "volume_participation_fok")
                    continue
                price = vwap * (ONE + slip)
                quantity = gross / price
                fee = gross * fee_rate
                total = gross + fee
                if total > cash:
                    reject(index, order, "insufficient_shared_cash")
                    continue
                equity_before = cash + sum(
                    current.quantity * bars[pair].close
                    for pair, current in positions.items()
                )
                reserve_breached = cash - total < equity_before * cfg.reserve_fraction
                recycled_replacement = (
                    order.depends_on_order_id is not None
                    and total
                    <= released_by_exit.get(order.depends_on_order_id, ZERO)
                )
                if reserve_breached and not recycled_replacement:
                    reject(index, order, "shared_reserve")
                    continue
                if order.kind == "ENTRY":
                    if total < cfg.minimum_position_basis:
                        reject(index, order, "undersized_position_identity")
                        continue
                    if len(positions) >= cfg.target_positions:
                        reject(index, order, "fleet_target_already_full")
                        continue
                    next_position_id += 1
                    position = Position(
                        next_position_id,
                        order.pair,
                        quantity,
                        total,
                        timestamp,
                    )
                    positions[order.pair] = position
                    entry_basis_observed.append(total)
                else:
                    pair_cap = equity_before * cfg.pair_cap_fraction
                    if position.basis + total > pair_cap:
                        reject(index, order, "pair_basis_cap")
                        continue
                    position = replace(
                        position,
                        quantity=position.quantity + quantity,
                        basis=position.basis + total,
                        recovery_count=position.recovery_count + 1,
                    )
                    positions[order.pair] = position
                    profit_states.pop(order.pair, None)
                cash -= total
                fees += fee
                turnover += gross
                fills.append(
                    FleetFill(
                        timestamp,
                        order.order_id,
                        order.pair,
                        order.kind,
                        position.position_id,
                        quantity,
                        price,
                        gross,
                        fee,
                        ZERO,
                        ZERO,
                        order.depends_on_order_id,
                    )
                )
                successful_orders.add(order.order_id)
                if order.depends_on_order_id is not None:
                    replaced_dependencies.add(order.depends_on_order_id)
                    successful_dependencies += 1
                    if reserve_breached:
                        replacement_recycle_fills += 1

            # Update only with the just-completed bar.  Rolling volume subtracts
            # exactly the minute that left the causal 24-hour window.
            for pair, bar in bars.items():
                volume_24h[pair] += bar.quote_volume
                if index >= 1440:
                    volume_24h[pair] -= dataset.series[pair][index - 1440].quote_volume
                observation = AssetObservation(
                    pair,
                    timestamp,
                    float(bar.close),
                    float(volume_24h[pair]),
                )
                prior = fingerprints.get(pair)
                fingerprints[pair] = (
                    AssetFingerprint.cold_start(observation)
                    if prior is None
                    else prior.update(observation)
                )

            committed = sum((position.basis for position in positions.values()), ZERO)
            marked = sum(
                (position.quantity * bars[pair].close for pair, position in positions.items()),
                ZERO,
            )
            equity = cash + marked
            if cash < ZERO or abs(cash + committed - cfg.initial_cash - realized) > D("1e-18"):
                raise ArithmeticError("shared fleet accounting invariant failed")
            if len(positions) > cfg.target_positions:
                raise ArithmeticError("fleet exceeded requested simultaneous positions")
            active_bases = {base_asset(pair) for pair in positions}
            if len(active_bases) != len(positions):
                raise ArithmeticError("duplicate simultaneous base-asset exposure")
            if any(position.basis < cfg.minimum_position_basis for position in positions.values()):
                raise ArithmeticError("active position fell below identity minimum")
            peak_committed = max(peak_committed, committed)
            samples.append(
                FleetSample(
                    timestamp,
                    cash,
                    committed,
                    marked,
                    equity,
                    len(positions),
                    len(active_bases),
                )
            )
            if initialized_index is None and len(positions) == cfg.target_positions:
                initialized_index = index

            spendable = max(ZERO, cash - equity * cfg.reserve_fraction)
            pending_pairs: set[str] = set()
            exit_orders: list[FleetOrder] = []
            recovery_orders: list[FleetOrder] = []
            for pair, position in sorted(positions.items()):
                bar = bars[pair]
                state = VCAPPositionState(
                    pair,
                    position.quantity,
                    position.basis,
                    bar.close,
                )
                previous_profit_state = profit_states.get(pair)
                exit_plan = plan_vcap_profit_exit(
                    state,
                    costs=exit_costs,
                    now=timestamp,
                    fingerprint=fingerprints[pair],
                    previous_state=previous_profit_state,
                    policy=self.exit_policy,
                )
                profit_states[pair] = exit_plan.state
                effective_action = exit_plan.action
                effective_reason = exit_plan.reasons[-1]
                if (
                    self.exit_strategy.mode == FleetExitMode.HARD_TARGET
                    and exit_plan.action == VCAPProfitExitAction.EXTEND
                ):
                    effective_action = VCAPProfitExitAction.EXIT
                    effective_reason = "hard_net_target_reached"
                    hard_target_exit_signals += 1
                elif self.exit_strategy.mode == FleetExitMode.EARNED_DTTP:
                    active_until = earned_dttp_until.get(pair, 0)
                    earned = active_until > timestamp
                    if exit_plan.action == VCAPProfitExitAction.EXTEND:
                        first_floor = (
                            previous_profit_state is None
                            or previous_profit_state.profit_floor_reached_at is None
                        )
                        supported = (
                            exit_plan.continuation_support
                            >= self.exit_strategy.extension_continuation_threshold
                            if first_floor
                            else exit_plan.continuation_support
                            >= self.exit_strategy.continuation_exit_threshold
                        )
                        if earned and supported:
                            dttp_extended_positions.add(position.position_id)
                            ever_dttp_extended_positions.add(position.position_id)
                            earned_dttp_extension_decisions += 1
                            effective_reason = "earned_dttp_extension_supported"
                        else:
                            effective_action = VCAPProfitExitAction.EXIT
                            hard_target_exit_signals += 1
                            effective_reason = (
                                "earned_dttp_continuation_failed"
                                if earned
                                else "hard_net_target_reached"
                            )
                            if earned:
                                earned_dttp_until[pair] = timestamp
                                qualifying_wins[pair] = []
                                earned_dttp_deactivation_events += 1
                    elif earned and exit_plan.action == VCAPProfitExitAction.EXIT:
                        # Giveback, stagnation, or a floor breach means the
                        # temporary momentum privilege has ended.
                        earned_dttp_until[pair] = timestamp
                        qualifying_wins[pair] = []
                        earned_dttp_deactivation_events += 1
                        effective_reason = "earned_dttp_protection_exit"
                decision_digest.update(
                    (
                        f"{timestamp}|{pair}|{self.exit_strategy.mode}|"
                        f"{exit_plan.action}|{effective_action}|{effective_reason}|"
                        f"{exit_plan.estimated_net_profit_bps}|"
                        f"{exit_plan.continuation_support}\n"
                    ).encode()
                )
                if effective_action == VCAPProfitExitAction.EXIT:
                    planner_exit_signals += 1
                    exit_signal_reasons[effective_reason] += 1
                    order = next_order(
                        timestamp,
                        pair,
                        "EXIT",
                        position_id=position.position_id,
                    )
                    exit_orders.append(order)
                    pending_pairs.add(pair)
                    continue
                average = position.basis / position.quantity
                if bar.close >= average * (ONE - cfg.recovery_trigger) or spendable <= ZERO:
                    continue
                proposal = CapitalProposal(
                    f"fleet:{timestamp}:{position.position_id}",
                    pair,
                    IntentKind.RECOVERY,
                    cfg.recovery_seed_quote,
                )
                recovery = plan_vcap_recovery(
                    proposal,
                    fingerprints[pair],
                    state,
                    now=timestamp,
                    fingerprint_policy=self.fingerprint_policy,
                    vcap_policy=self.recovery_policy,
                )
                if recovery.planned_amount_quote <= ZERO:
                    continue
                pair_available = max(ZERO, equity * cfg.pair_cap_fraction - position.basis)
                authorization = min(
                    recovery.planned_amount_quote,
                    pair_available,
                    spendable / (ONE + fee_rate),
                )
                if authorization < cfg.simulated_min_order_quote:
                    continue
                rule = KrakenTradingRule(
                    pair,
                    cfg.simulated_base_step,
                    cfg.simulated_min_order_quote,
                    ZERO,
                    ZERO,
                    cfg.simulated_base_step,
                    ZERO,
                    True,
                    True,
                )
                entry_price = bar.close * (ONE + slip)
                tranche = plan_vcap_tranches(
                    recovery,
                    authorized_quote=authorization,
                    current_price=entry_price,
                    trading_rule=rule,
                    policy=self.tranche_policy,
                )
                if not tranche.tranches:
                    continue
                budget = tranche.tranches[0].quote_amount * (ONE + fee_rate)
                recovery_orders.append(
                    next_order(
                        timestamp,
                        pair,
                        "RECOVERY",
                        budget=budget,
                        position_id=position.position_id,
                    )
                )
                planner_recovery_signals += 1
                pending_pairs.add(pair)
                spendable -= budget

            # Rank only from observations available at this close.  The first
            # bar uses its realized range; later bars use the FAST fingerprint's
            # causal absolute-return memory.  Volume is only a tie-breaker.
            def movement_rank(pair: str) -> tuple[float, float, str]:
                window = fingerprints[pair].windows[FingerprintHorizon.FAST]
                movement = window.ema_abs_return_bps
                if movement is None:
                    bar = bars[pair]
                    movement = float((bar.high - bar.low) / bar.open * BPS)
                return (-movement, -float(volume_24h[pair]), pair)

            entry_gross = cfg.entry_budget / (ONE + fee_rate)
            candidates = [
                pair
                for pair in pairs
                if pair not in positions
                and pair not in pending_pairs
                and pair not in closed_this_frame
                # The current completed minute is a causal eligibility check,
                # not a guarantee about the following minute's volume.
                and bars[pair].quote_volume * cfg.volume_participation
                >= entry_gross
            ]
            candidates.sort(key=movement_rank)
            entry_orders: list[FleetOrder] = []
            # First repair any already-visible shortfall using current cash.
            shortfall = cfg.target_positions - len(positions)
            while shortfall > 0 and candidates and spendable >= cfg.entry_budget:
                pair = candidates.pop(0)
                entry_orders.append(
                    next_order(timestamp, pair, "ENTRY", budget=cfg.entry_budget)
                )
                pending_pairs.add(pair)
                spendable -= cfg.entry_budget
                shortfall -= 1
            if shortfall > 0:
                reason = (
                    "insufficient_shared_cash_for_fleet"
                    if spendable < cfg.entry_budget
                    else "insufficient_liquidity_eligible_assets"
                )
                rejection_counts[reason] += shortfall
            # Pre-authorize a distinct inactive replacement for every pending
            # exit.  It may fill only if that exact exit fills first next bar.
            for exit_order in exit_orders:
                assigned = 0
                while candidates and assigned < cfg.replacement_candidates_per_exit:
                    pair = candidates.pop(0)
                    entry_orders.append(
                        next_order(
                            timestamp,
                            pair,
                            "ENTRY",
                            budget=cfg.entry_budget,
                            depends_on=exit_order.order_id,
                        )
                    )
                    pending_pairs.add(pair)
                    assigned += 1
                if assigned == 0:
                    rejection_counts["no_distinct_replacement_candidate"] += 1

            pending = exit_orders + recovery_orders + entry_orders

        final_bar = {pair: dataset.series[pair][-1] for pair in pairs}
        committed = sum((position.basis for position in positions.values()), ZERO)
        marked = sum(
            (position.quantity * final_bar[pair].close for pair, position in positions.items()),
            ZERO,
        )
        equity = cash + marked
        unrealized = marked - committed
        drawdown = ZERO
        peak = samples[0].equity
        for sample in samples:
            peak = max(peak, sample.equity)
            if peak > ZERO:
                drawdown = max(drawdown, (peak - sample.equity) / peak)
        if initialized_index is None:
            post_init = samples
        else:
            post_init = samples[initialized_index:]
        below_target = sum(
            sample.active_positions < cfg.target_positions for sample in post_init
        )
        active_minutes = sum(sample.active_positions for sample in samples)
        position_minutes_capacity = cfg.target_positions * len(samples)
        entry_fills = [fill for fill in fills if fill.kind == "ENTRY"]
        exit_fills = [fill for fill in fills if fill.kind == "EXIT"]
        recovery_fills = [fill for fill in fills if fill.kind == "RECOVERY"]
        metrics = {
            "starting_cash": cfg.initial_cash,
            "ending_cash": cash,
            "ending_equity": equity,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": equity - cfg.initial_cash,
            "fees": fees,
            "turnover_quote": turnover,
            "max_drawdown_fraction": drawdown,
            "open_basis": committed,
            "peak_committed_basis": peak_committed,
            "ending_positions": len(positions),
            "target_positions": cfg.target_positions,
            "fleet_initialized": initialized_index is not None,
            "first_target_timestamp": (
                None if initialized_index is None else samples[initialized_index].timestamp
            ),
            "minimum_positions_after_initialization": min(
                sample.active_positions for sample in post_init
            ),
            "minutes_below_target_after_initialization": below_target,
            "fleet_position_minute_fill_ratio": (
                D(active_minutes) / D(position_minutes_capacity)
                if position_minutes_capacity
                else ZERO
            ),
            "entry_fills": len(entry_fills),
            "rotations": max(0, len(entry_fills) - cfg.target_positions),
            "completed_positions": len(exit_fills),
            "recovery_fills": len(recovery_fills),
            "linked_replacements_filled": successful_dependencies,
            "linked_replacements_funded_from_released_capital": replacement_recycle_fills,
            "unique_position_ids": len({fill.position_id for fill in entry_fills}),
            "unique_assets_ever_entered": len({fill.pair for fill in entry_fills}),
            "minimum_entry_basis": min(entry_basis_observed) if entry_basis_observed else None,
            "maximum_simultaneous_positions": max(
                sample.active_positions for sample in samples
            ),
            "duplicate_asset_violations": 0,
            "expired_orders": len(pending),
            "planner_exit_signals": planner_exit_signals,
            "planner_recovery_signals": planner_recovery_signals,
            "exit_mode": self.exit_strategy.mode,
            "target_net_profit_bps": self.exit_policy.target_net_profit_bps,
            "hard_target_exit_signals": hard_target_exit_signals,
            "exit_signal_reasons": dict(sorted(exit_signal_reasons.items())),
            "qualifying_hard_wins": qualifying_hard_wins,
            "earned_dttp_activation_events": earned_dttp_activation_events,
            "earned_dttp_assets_activated": len(earned_dttp_assets),
            "earned_dttp_deactivation_events": earned_dttp_deactivation_events,
            "earned_dttp_extension_decisions": earned_dttp_extension_decisions,
            "earned_dttp_extended_positions": len(ever_dttp_extended_positions),
            "execution_model": "next-minute recorded VWAP plus slippage; FOK at recorded quote-volume participation cap",
            "evidence_class": "recorded OHLCV; estimated execution; not top-of-book",
        }
        fingerprint = hashlib.sha256(
            (
                _source_hash(Path(__file__))
                + _source_hash(Path(__file__).with_name("binance_kline_import.py"))
                + dataset.manifest["source_fingerprint"]
                + json.dumps(asdict(cfg), default=str, sort_keys=True)
                + json.dumps(asdict(self.exit_strategy), default=str, sort_keys=True)
                + json.dumps(asdict(self.exit_policy), default=str, sort_keys=True)
                + decision_digest.hexdigest()
            ).encode()
        ).hexdigest()
        return FleetResult(
            cfg,
            self.exit_strategy,
            self.exit_policy,
            dataset.manifest,
            cash,
            tuple(sorted(positions.values(), key=lambda position: position.position_id)),
            tuple(fills),
            tuple(samples),
            dict(sorted(rejection_counts.items())),
            tuple(rejection_examples),
            metrics,
            fingerprint,
        )


def report(result: FleetResult) -> dict:
    return {
        "schema": 2,
        "engine": "spectra-rotating-fleet-v2-exit-comparison",
        "warning": (
            "Recorded OHLCV with estimated next-minute VWAP/participation execution. "
            "This is not recorded bid/ask depth and is not evidence of live profitability."
        ),
        "engine_source_sha256": _source_hash(Path(__file__)),
        "importer_source_sha256": _source_hash(
            Path(__file__).with_name("binance_kline_import.py")
        ),
        "config": asdict(result.config),
        "exit_strategy": asdict(result.exit_strategy),
        "exit_policy": asdict(result.exit_policy),
        "dataset": result.dataset_manifest,
        "metrics": result.metrics,
        "rejections": result.rejections,
        "rejection_examples": result.rejection_examples,
        "fills": tuple(asdict(fill) for fill in result.fills),
        "ending_positions": tuple(asdict(position) for position in result.positions),
        "fingerprint": result.fingerprint,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fee-bps", default="40")
    parser.add_argument("--slippage-bps", default="5")
    parser.add_argument(
        "--exit-mode",
        choices=tuple(mode.value for mode in FleetExitMode),
        default=FleetExitMode.DTTP.value,
    )
    parser.add_argument("--target-net-profit-bps", default="174")
    args = parser.parse_args()
    dataset = load_archives(args.archives)
    exit_strategy = FleetExitStrategy(
        mode=FleetExitMode(args.exit_mode),
        qualifying_win_bps=D(args.target_net_profit_bps),
    )
    result = RotatingFleetSimulator(
        replace(
            FleetConfig(),
            fee_bps=D(args.fee_bps),
            slippage_bps=D(args.slippage_bps),
        ),
        exit_policy=replace(
            VCAPProfitExitPolicy(),
            target_net_profit_bps=D(args.target_net_profit_bps),
        ),
        exit_strategy=exit_strategy,
    ).run(dataset)
    args.output.write_text(json.dumps(report(result), default=str, indent=2) + "\n")
    print(json.dumps(result.metrics, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
