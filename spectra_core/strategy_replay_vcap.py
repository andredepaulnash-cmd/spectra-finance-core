"""Offline adapter to production pure planners; never creates a live governor."""

from dataclasses import asdict, replace
from decimal import Decimal

from .asset_fingerprint import AssetFingerprint, AssetObservation
from .kraken_market_fabric import KrakenTradingRule
from .models import CapitalProposal, IntentKind
from .recovery_fingerprint import (
    RecoveryFingerprintPolicy,
    VCAPPositionState,
    VCAPRecoveryPolicy,
    plan_vcap_recovery,
)
from .strategy_replay import BPS, ONE, ZERO, Order, ReplayConfig, View, number
from .vcap_profit_exit import (
    VCAPExitCostModel,
    VCAPProfitExitAction,
    VCAPProfitExitPolicy,
    plan_vcap_profit_exit,
)
from .vcap_tranches import VCAPTranchePolicy, plan_vcap_tranches


class ProductionPlannerAdapter:
    """One instance per replay, with causal fingerprint and profit-lock state.

    Entry cadence and the drawdown trigger below are explicit research inputs.
    Recovery sizing, fingerprint throttling, tranche geometry and profit exits
    call production code. Simulator cash/caps remain simulation-only authority.
    """

    def __init__(
        self,
        config: ReplayConfig,
        *,
        ticket=Decimal("10"),
        recovery_trigger=Decimal(".03"),
        fingerprint_policy=None,
        recovery_policy=None,
        tranche_policy=None,
        exit_policy=None,
    ):
        self.config = config
        self.ticket = number(ticket, "ticket")
        self.recovery_trigger = number(recovery_trigger, "recovery_trigger")
        if self.ticket <= ZERO or self.recovery_trigger >= ONE:
            raise ValueError("invalid entry ticket or recovery trigger")
        self.fingerprint_policy = fingerprint_policy or RecoveryFingerprintPolicy()
        self.recovery_policy = recovery_policy or VCAPRecoveryPolicy()
        self.tranche_policy = tranche_policy or VCAPTranchePolicy()
        self.exit_policy = exit_policy or VCAPProfitExitPolicy()
        self.costs = VCAPExitCostModel(
            expected_exit_fee_bps=config.fee_bps,
            slippage_allowance_bps=config.slippage_bps,
        )
        self.fingerprints = {}
        self.profit_states = {}
        self.inventory_signatures = {}
        self.decisions = []
        self._seen_frames = 0
        self._last_frame = None

    def parameters(self):
        return {
            "ticket": self.ticket,
            "recovery_trigger": self.recovery_trigger,
            "fingerprint_policy": asdict(self.fingerprint_policy),
            "recovery_policy": asdict(self.recovery_policy),
            "tranche_policy": asdict(self.tranche_policy),
            "exit_policy": asdict(self.exit_policy),
        }

    def __call__(self, view: View):
        if len(view.history) != self._seen_frames + 1 or (
            self._last_frame is not None and view.history[-2] != self._last_frame
        ):
            raise ValueError("fresh adapter required for each sequential replay")
        frame = view.history[-1]
        # Validate the entire frame before mutating adapter state.
        if any(q.timestamp <= 0 or q.quote_volume_24h is None for q in frame):
            raise ValueError(
                "production fingerprints require positive timestamps and recorded 24h volume"
            )
        self._seen_frames += 1
        self._last_frame = frame
        quotes = {q.pair: q for q in frame}
        equity = view.cash + sum(
            (lot.quantity * quotes[lot.pair].bid for lot in view.lots), ZERO
        )
        spendable = max(ZERO, view.cash - equity * self.config.reserve_fraction)
        orders = []
        for q in frame:
            mid = (q.bid + q.ask) / Decimal(2)
            observation = AssetObservation(
                q.pair,
                q.timestamp,
                float(mid),
                float(q.quote_volume_24h),
                spread_bps=float((q.ask - q.bid) / mid * BPS),
                depth_quote=float(q.depth_base * mid),
            )
            prior = self.fingerprints.get(q.pair)
            fp = (
                AssetFingerprint.cold_start(observation)
                if prior is None
                else prior.update(observation)
            )
            self.fingerprints[q.pair] = fp
            lots = tuple(lot for lot in view.lots if lot.pair == q.pair)
            signature = tuple((lot.lot_id, lot.quantity, lot.basis) for lot in lots)
            if signature != self.inventory_signatures.get(q.pair):
                # Profit-state geometry belongs to the current inventory cohort.
                self.profit_states.pop(q.pair, None)
                self.inventory_signatures[q.pair] = signature
            if q.pair in view.pending_pairs:
                continue
            pair_basis = sum((lot.basis for lot in lots), ZERO)
            available = min(
                spendable,
                max(ZERO, equity * self.config.pair_cap_fraction - pair_basis),
            )
            if not lots:
                amount = min(self.ticket, available)
                if amount > ZERO:
                    orders.append(Order(q.pair, "BUY", amount))
                    spendable -= amount
                continue
            quantity = sum((lot.quantity for lot in lots), ZERO)
            # Replay basis already includes buy fees: accrued_fees stays zero.
            position = VCAPPositionState(q.pair, quantity, pair_basis, q.bid)
            exit_plan = plan_vcap_profit_exit(
                position,
                costs=self.costs,
                now=q.timestamp,
                fingerprint=fp,
                previous_state=self.profit_states.get(q.pair),
                policy=self.exit_policy,
            )
            self.profit_states[q.pair] = exit_plan.state
            record = {
                "timestamp": q.timestamp,
                "pair": q.pair,
                "exit": asdict(exit_plan),
            }
            self.decisions.append(record)
            if exit_plan.action == VCAPProfitExitAction.EXIT:
                orders.append(
                    Order(q.pair, "SELL", quantity, tuple(lot.lot_id for lot in lots))
                )
                continue
            if (
                q.ask >= pair_basis / quantity * (ONE - self.recovery_trigger)
                or available <= ZERO
            ):
                continue
            proposal = CapitalProposal(
                f"replay:{q.timestamp}:{q.pair}",
                q.pair,
                IntentKind.RECOVERY,
                self.ticket,
            )
            recovery = plan_vcap_recovery(
                proposal,
                fp,
                replace(position, current_price=q.ask),
                now=q.timestamp,
                fingerprint_policy=self.fingerprint_policy,
                vcap_policy=self.recovery_policy,
            )
            record["recovery"] = asdict(recovery)
            # Pure planner uses quote notional; replay Order BUY is fee-inclusive.
            authorization = min(
                recovery.planned_amount_quote,
                available / (ONE + self.config.fee_bps / BPS),
            )
            if authorization <= ZERO:
                continue
            rule = KrakenTradingRule(
                q.pair,
                q.min_base,
                q.min_quote,
                ZERO,
                ZERO,
                q.step_base,
                ZERO,
                True,
                True,
            )
            entry_price = q.ask * (ONE + self.config.slippage_bps / BPS)
            tranche_plan = plan_vcap_tranches(
                recovery,
                authorized_quote=authorization,
                current_price=entry_price,
                trading_rule=rule,
                policy=self.tranche_policy,
            )
            record["tranches"] = asdict(tranche_plan)
            if tranche_plan.tranches:
                amount = tranche_plan.tranches[0].quote_amount * (
                    ONE + self.config.fee_bps / BPS
                )
                orders.append(Order(q.pair, "BUY", amount, recovery=True))
                spendable -= amount
        return tuple(orders)
