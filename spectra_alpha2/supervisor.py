"""Single deterministic portfolio admission and paper execution loop."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from decimal import Decimal

from .condor import CondorAnalyzer
from .execution import ExecutionUncertain, HummingbotPaperExecutionClient
from .governor import CapitalDenied, CapitalGovernor
from .market_data import ResearchScanner
from .order_builder import build_buy_market_order
from .policy import MAX_OPEN_POSITIONS
from .safety import PaperSafetySettings


@dataclass(frozen=True, slots=True)
class CycleReport:
    discovered_markets: int
    eligible_markets: int
    existing_positions: int
    available_slots: int
    admitted_positions: int
    pending_orders: int
    skipped_existing_assets: int
    skipped_capital_or_rule_limits: int
    active_positions_after_cycle: int
    reconciled_orders: int


class PortfolioSupervisor:
    """Coordinates scanner -> Condor -> Governor -> Hummingbot in that order."""

    def __init__(
        self,
        governor: CapitalGovernor,
        scanner: ResearchScanner,
        condor: CondorAnalyzer,
        execution: HummingbotPaperExecutionClient,
        *,
        target_positions: int = MAX_OPEN_POSITIONS,
        max_new_positions_per_cycle: int = 10,
        max_slippage_fraction: Decimal = Decimal("0.001"),
        order_timeout_seconds: float = 60,
    ):
        if isinstance(target_positions,bool) or not isinstance(target_positions,int) or not 1<=target_positions<=MAX_OPEN_POSITIONS:
            raise ValueError(f"target_positions must be between 1 and {MAX_OPEN_POSITIONS}")
        if isinstance(max_new_positions_per_cycle,bool) or not isinstance(max_new_positions_per_cycle,int) or max_new_positions_per_cycle<1:
            raise ValueError("max_new_positions_per_cycle must be positive")
        if not isinstance(max_slippage_fraction,Decimal) or not max_slippage_fraction.is_finite() or not Decimal("0")<=max_slippage_fraction<=Decimal("0.02"):
            raise ValueError("max_slippage_fraction must be between 0% and 2%")
        self.safety=governor.safety
        self.safety.assert_paper_mode()
        self.governor=governor
        self.scanner=scanner
        self.condor=condor
        self.execution=execution
        self.target_positions=target_positions
        self.max_new_positions_per_cycle=max_new_positions_per_cycle
        self.max_slippage_fraction=max_slippage_fraction
        self.order_timeout_seconds=order_timeout_seconds

    async def run_once(self) -> CycleReport:
        self.safety.assert_paper_mode()
        reconciled=await self.execution.reconcile_pending(self.governor,timeout_seconds=0.1)
        candidates=await self.scanner.scan()
        recommendations=self.condor.recommend(candidates,limit=self.target_positions)
        candidate_by_pair={candidate.trading_pair:candidate for candidate in candidates}
        positions=self.governor.list_positions()
        existing_assets={position["asset_id"] for position in positions}
        slots=max(0,self.target_positions-len(positions))
        admission_budget=min(slots,self.max_new_positions_per_cycle)
        admitted=0
        pending_orders=sum(1 for result in reconciled if result.status=="PENDING")
        skipped_existing=0
        skipped_limits=0

        for recommendation in recommendations:
            if admitted>=admission_budget:
                break
            candidate=candidate_by_pair.get(recommendation.trading_pair)
            if candidate is None:
                continue
            if candidate.trading_pair in existing_assets:
                skipped_existing+=1
                continue
            position_id=str(uuid.uuid4())
            idempotency_key=f"alpha2-initial-{position_id}"
            authorization=None
            try:
                authorization=self.governor.reserve_initial_position(
                    position_id=position_id,
                    asset_id=candidate.trading_pair,
                    reference_price=candidate.last_price,
                    idempotency_key=idempotency_key,
                )
                draft=build_buy_market_order(
                    candidate,
                    gross_quote_budget=authorization.gross_notional,
                    max_slippage_fraction=self.max_slippage_fraction,
                )
            except (CapitalDenied,ValueError):
                if authorization is not None:
                    self.governor.release_unfilled_reservation(
                        authorization.authorization_id,confirmed_no_fill=True
                    )
                skipped_limits+=1
                continue
            try:
                result=await self.execution.submit_authorized_buy(
                    self.governor,
                    authorization,
                    draft.request_body,
                    timeout_seconds=self.order_timeout_seconds,
                )
            except ExecutionUncertain:
                # A submitted order may exist. Stop admission until reconciliation proves its state.
                raise
            if result.filled_quantity>0:
                admitted+=1
                existing_assets.add(candidate.trading_pair)
            elif result.status=="PENDING":
                pending_orders+=1
                existing_assets.add(candidate.trading_pair)
            else:
                skipped_limits+=1

        active_after=len(self.governor.list_positions())
        return CycleReport(
            discovered_markets=len(candidates),
            eligible_markets=len(recommendations),
            existing_positions=len(positions),
            available_slots=slots,
            admitted_positions=admitted,
            pending_orders=pending_orders,
            skipped_existing_assets=skipped_existing,
            skipped_capital_or_rule_limits=skipped_limits,
            active_positions_after_cycle=active_after,
            reconciled_orders=len(reconciled),
        )

    async def run_forever(self, *, interval_seconds: float = 30, stop_event: asyncio.Event | None = None) -> None:
        if interval_seconds<=0:
            raise ValueError("interval_seconds must be positive")
        while stop_event is None or not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event is not None else asyncio.sleep(interval_seconds),
                    timeout=interval_seconds if stop_event is not None else interval_seconds+1,
                )
            except asyncio.TimeoutError:
                pass
