from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from mcp_servers.hummingbot_api.tools.accounting_health import (
    synchronize_position_accounting_health,
)
from mcp_servers.hummingbot_api.tools.economic_exit import (
    dispatch_protected_economic_exit,
    reconcile_economic_exit_owner_accounting,
)
from mcp_servers.hummingbot_api.tools.partial_recycle import (
    synchronize_partial_recycle_executor,
)
from mcp_servers.hummingbot_api.tools.vcap_lifecycle import (
    execute_vcap_lifecycle_decision,
)
from spectra_core.accounting_health import (
    AccountingHealthState,
    AccountingHealthSupervisor,
)
from spectra_core.asset_fingerprint import (
    AssetFingerprint,
    observation_from_kraken_universe,
)
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_exit import (
    EconomicExitManifestStore,
    EconomicExitProgressState,
    assess_position_exit_manifest,
)
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.kraken_market_fabric import (
    KrakenTradingRule,
    collect_kraken_universe,
)
from spectra_core.partial_recycle import (
    PartialRecycleStore,
    assess_partial_recycle_manifest,
)
from spectra_core.recovery_fingerprint import (
    RecoveryFingerprintPolicy,
    VCAPRecoveryPolicy,
)
from spectra_core.vcap_lifecycle import (
    VCAPLifecycleAction,
    VCAPLifecycleDecision,
    checkpoint_vcap_exit_result,
    plan_vcap_lifecycle_from_ledger,
)
from spectra_core.vcap_profit_exit import VCAPProfitExitPolicy
from spectra_core.vcap_runtime import (
    VCAPRuntimeCycle,
    VCAPRuntimePhase,
    VCAPRuntimePolicy,
    VCAPRuntimeStore,
)
from spectra_core.vcap_state import VCAPStateStore
from spectra_core.vcap_tranches import VCAPTranchePolicy

_PREPLAN_PHASES = (
    VCAPRuntimePhase.STARTED,
    VCAPRuntimePhase.ACCOUNTING_PRECHECKED,
    VCAPRuntimePhase.MARKET_REFRESHED,
    VCAPRuntimePhase.PLANNED,
)


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("proposal ids must be non-empty strings")
        value = raw.strip()
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _runtime_result(
    cycle: VCAPRuntimeCycle,
    *,
    accounting_state: str | None = None,
    action: str | None = None,
    executed: bool | None = None,
    reasons: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "cycle_id": cycle.cycle_id,
        "cycle_sequence": cycle.sequence,
        "phase": cycle.phase.value,
        "position_id": cycle.position_id,
        "envelope_proposal_id": cycle.envelope_proposal_id,
        "accounting_state": accounting_state,
        "action": action or cycle.planned_action,
        "executed": executed,
        "mutation_key": cycle.mutation_key,
        "mutation_proposal_id": cycle.mutation_proposal_id,
        "heartbeat_at": cycle.heartbeat_at,
        "terminal": cycle.terminal,
        "reasons": list(reasons or cycle.decision_reasons),
        "last_error": cycle.last_error,
    }


async def _refresh_market(
    client: Any,
    *,
    pair: str,
    runtime_store: VCAPRuntimeStore,
    policy: VCAPRuntimePolicy,
    now: float,
) -> tuple[AssetFingerprint, KrakenTradingRule, Decimal]:
    quote = pair.rsplit("-", 1)[1]
    universe = await collect_kraken_universe(
        client,
        quote_assets=(quote,),
        order_book_pairs=(pair,),
        order_book_depth=policy.order_book_depth,
    )
    if pair not in universe.eligible_pairs:
        reason = universe.rejected.get(pair, "pair not eligible")
        raise RuntimeError(f"Kraken market refresh rejected {pair}: {reason}")

    observation = observation_from_kraken_universe(universe, pair)
    if observation.timestamp > float(now) + 5.0:
        raise RuntimeError("market observation timestamp is ahead of runtime clock")
    age = float(now) - observation.timestamp
    if age > policy.market_max_age_seconds:
        raise RuntimeError(
            f"market observation is stale by {age:.3f}s"
        )

    prior = runtime_store.load_fingerprint(pair)
    if prior is None:
        fingerprint = AssetFingerprint.cold_start(observation)
    elif observation.timestamp > prior.last_seen_at:
        fingerprint = prior.update(observation)
    elif observation.timestamp == prior.last_seen_at:
        # Exact retry/restart of the same market snapshot is idempotent.
        fingerprint = prior
    else:
        raise RuntimeError(
            "market observation predates persisted asset fingerprint"
        )

    runtime_store.save_fingerprint(fingerprint, now=now)
    return (
        fingerprint,
        universe.trading_rules[pair],
        universe.tickers[pair].price,
    )


def _proposal_scope(
    *,
    position_id: str,
    ledger: EconomicPositionLedger,
    recovery_proposal_ids: Iterable[str],
    mutation_proposal_id: str | None = None,
) -> tuple[str, ...]:
    extras = list(recovery_proposal_ids)
    if mutation_proposal_id:
        extras.append(mutation_proposal_id)
    return _dedupe(
        (
            *ledger.active_proposal_ids(position_id),
            *extras,
        )
    )


async def _refresh_accounting(
    client: Any,
    *,
    position_id: str,
    proposal_ids: Iterable[str],
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    supervisor: AccountingHealthSupervisor,
    now: float,
):
    return await synchronize_position_accounting_health(
        client,
        position_id=position_id,
        proposal_ids=proposal_ids,
        governor=governor,
        ledger=ledger,
        supervisor=supervisor,
        now=now,
    )


def _mutation_key(decision: VCAPLifecycleDecision) -> tuple[str, str | None]:
    if decision.action == VCAPLifecycleAction.EXECUTE_TRANCHE:
        execution = decision.execution_decision
        if execution is None or execution.next_proposal is None:
            raise RuntimeError("tranche decision has no exact proposal")
        proposal_id = execution.next_proposal.proposal_id
        return f"TRANCHE:{proposal_id}", proposal_id

    if decision.action == VCAPLifecycleAction.PARTIAL_RECYCLE:
        partial = decision.partial_recycle_decision
        if partial is None or partial.manifest is None:
            raise RuntimeError("partial recycle decision has no exact manifest")
        return f"PARTIAL_RECYCLE:{partial.manifest.manifest_id}", None

    if decision.action == VCAPLifecycleAction.EXIT:
        return f"EXIT:{decision.state.envelope_proposal_id}", None

    raise ValueError("lifecycle decision is not capital-mutating")


def _tranche_fill_confirmed(
    *,
    position_id: str,
    proposal_id: str,
    ledger: EconomicPositionLedger,
) -> bool:
    return proposal_id in ledger.active_proposal_ids(position_id)


async def _resume_reconciliation(
    client: Any,
    *,
    cycle: VCAPRuntimeCycle,
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    lifecycle_store: VCAPStateStore,
    runtime_store: VCAPRuntimeStore,
    accounting_supervisor: AccountingHealthSupervisor,
    now: float,
    policy: VCAPRuntimePolicy | None = None,
    account_name: str = "master_account",
    controller_id: str = "spectra",
) -> dict[str, Any]:
    state = lifecycle_store.load(cycle.envelope_proposal_id)
    if state is None:
        conflict = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.CONFLICT,
            now=now,
            expected_phases=(
                VCAPRuntimePhase.MUTATION_DISPATCHING,
                VCAPRuntimePhase.MUTATION_DISPATCHED,
                VCAPRuntimePhase.AWAITING_RECONCILIATION,
            ),
            last_error="VCAP lifecycle state disappeared during runtime cycle",
        )
        return _runtime_result(
            conflict,
            reasons=("vcap_lifecycle_state_missing",),
        )

    exit_manifest = None
    exit_manifest_store = None
    partial_manifest = None
    partial_store = None
    if cycle.planned_action == VCAPLifecycleAction.EXIT.value:
        exit_manifest_store = EconomicExitManifestStore(ledger.path)
        manifest_id = None
        if isinstance(cycle.execution_result, dict):
            raw_manifest_id = cycle.execution_result.get("manifest_id")
            if isinstance(raw_manifest_id, str) and raw_manifest_id.strip():
                manifest_id = raw_manifest_id.strip()
        exit_manifest = (
            None if manifest_id is None else exit_manifest_store.load(manifest_id)
        )
        if exit_manifest is None:
            exit_manifest = exit_manifest_store.open_for_position(cycle.position_id)
        if exit_manifest is None:
            exit_manifest = exit_manifest_store.latest_for_position(cycle.position_id)
        if exit_manifest is None:
            conflict = runtime_store.transition(
                cycle.cycle_id,
                phase=VCAPRuntimePhase.CONFLICT,
                now=now,
                expected_phases=(
                    VCAPRuntimePhase.MUTATION_DISPATCHING,
                    VCAPRuntimePhase.MUTATION_DISPATCHED,
                    VCAPRuntimePhase.AWAITING_RECONCILIATION,
                ),
                last_error="economic exit manifest is missing after dispatch",
                decision_reasons=("economic_exit_manifest_missing",),
            )
            return _runtime_result(
                conflict,
                reasons=conflict.decision_reasons,
            )
        scoped_ids = exit_manifest.proposal_ids
    elif cycle.planned_action == VCAPLifecycleAction.PARTIAL_RECYCLE.value:
        partial_store = PartialRecycleStore(ledger.path)
        manifest_id = None
        if isinstance(cycle.execution_result, dict):
            raw_manifest_id = cycle.execution_result.get("manifest_id")
            if isinstance(raw_manifest_id, str) and raw_manifest_id.strip():
                manifest_id = raw_manifest_id.strip()
        partial_manifest = (
            None if manifest_id is None else partial_store.load(manifest_id)
        )
        if partial_manifest is None:
            partial_manifest = partial_store.open_for_position(cycle.position_id)
        if partial_manifest is None:
            conflict = runtime_store.transition(
                cycle.cycle_id,
                phase=VCAPRuntimePhase.CONFLICT,
                now=now,
                expected_phases=(
                    VCAPRuntimePhase.MUTATION_DISPATCHING,
                    VCAPRuntimePhase.MUTATION_DISPATCHED,
                    VCAPRuntimePhase.AWAITING_RECONCILIATION,
                ),
                last_error="partial recycle manifest is missing after dispatch",
                decision_reasons=("partial_recycle_manifest_missing",),
            )
            return _runtime_result(
                conflict,
                reasons=conflict.decision_reasons,
            )
        scoped_ids = _dedupe(
            (*state.recovery_proposal_ids, *partial_manifest.proposal_ids)
        )
    else:
        scoped_ids = state.recovery_proposal_ids

    scope = _proposal_scope(
        position_id=cycle.position_id,
        ledger=ledger,
        recovery_proposal_ids=scoped_ids,
        mutation_proposal_id=cycle.mutation_proposal_id,
    )
    try:
        health = await _refresh_accounting(
            client,
            position_id=cycle.position_id,
            proposal_ids=scope,
            governor=governor,
            ledger=ledger,
            supervisor=accounting_supervisor,
            now=now,
        )
    except Exception as exc:
        waiting = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
            now=now,
            expected_phases=(
                VCAPRuntimePhase.MUTATION_DISPATCHING,
                VCAPRuntimePhase.MUTATION_DISPATCHED,
                VCAPRuntimePhase.AWAITING_RECONCILIATION,
            ),
            decision_reasons=(
                *cycle.decision_reasons,
                "post_mutation_accounting_refresh_failed",
            ),
            last_error=str(exc),
        )
        return _runtime_result(
            waiting,
            executed=True,
            reasons=waiting.decision_reasons,
        )

    if health.state == AccountingHealthState.CONFLICT:
        conflict = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.CONFLICT,
            now=now,
            expected_phases=(
                VCAPRuntimePhase.MUTATION_DISPATCHING,
                VCAPRuntimePhase.MUTATION_DISPATCHED,
                VCAPRuntimePhase.AWAITING_RECONCILIATION,
            ),
            decision_reasons=health.reasons,
            last_error="post-mutation accounting conflict",
        )
        return _runtime_result(
            conflict,
            accounting_state=health.state.value,
            reasons=health.reasons,
        )

    reconciled = False
    if health.state == AccountingHealthState.HEALTHY:
        if cycle.planned_action == VCAPLifecycleAction.EXECUTE_TRANCHE.value:
            proposal_id = cycle.mutation_proposal_id
            reconciled = proposal_id is not None and _tranche_fill_confirmed(
                position_id=cycle.position_id,
                proposal_id=proposal_id,
                ledger=ledger,
            )
        elif cycle.planned_action == VCAPLifecycleAction.EXIT.value:
            if exit_manifest is None or exit_manifest_store is None:
                raise RuntimeError("economic exit manifest was not loaded")

            try:
                if exit_manifest.sell_executor_id is not None:
                    result = await dispatch_protected_economic_exit(
                        client,
                        manifest=exit_manifest,
                        governor=governor,
                        ledger=ledger,
                        now=now,
                        account_name=account_name,
                        controller_id=controller_id,
                    )
                    if result.get("requires_fresh_trading_rule"):
                        _, fresh_rule, _ = await _refresh_market(
                            client,
                            pair=exit_manifest.pair,
                            runtime_store=runtime_store,
                            policy=policy or VCAPRuntimePolicy(),
                            now=now,
                        )
                        result = await dispatch_protected_economic_exit(
                            client,
                            manifest=exit_manifest,
                            governor=governor,
                            ledger=ledger,
                            now=now,
                            trading_rule=fresh_rule,
                            account_name=account_name,
                            controller_id=controller_id,
                        )
                    owner_accounting = result
                else:
                    owner_accounting = await reconcile_economic_exit_owner_accounting(
                        client,
                        manifest=exit_manifest,
                        governor=governor,
                    )
                progress = assess_position_exit_manifest(
                    exit_manifest,
                    ledger=ledger,
                    store=exit_manifest_store,
                    now=now,
                )
            except Exception as exc:
                waiting = runtime_store.transition(
                    cycle.cycle_id,
                    phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
                    now=now,
                    expected_phases=(
                        VCAPRuntimePhase.MUTATION_DISPATCHING,
                        VCAPRuntimePhase.MUTATION_DISPATCHED,
                        VCAPRuntimePhase.AWAITING_RECONCILIATION,
                    ),
                    decision_reasons=(
                        *health.reasons,
                        "economic_exit_evidence_or_finality_pending",
                    ),
                    last_error=str(exc),
                )
                return _runtime_result(
                    waiting,
                    accounting_state=health.state.value,
                    executed=True,
                    reasons=waiting.decision_reasons,
                )

            if progress.state == EconomicExitProgressState.DUST_BLOCKED:
                hold = runtime_store.transition(
                    cycle.cycle_id,
                    phase=VCAPRuntimePhase.HOLD,
                    now=now,
                    expected_phases=(
                        VCAPRuntimePhase.MUTATION_DISPATCHING,
                        VCAPRuntimePhase.MUTATION_DISPATCHED,
                        VCAPRuntimePhase.AWAITING_RECONCILIATION,
                    ),
                    decision_reasons=(
                        *health.reasons,
                        *progress.reasons,
                    ),
                    last_error="economic exit is blocked by exchange dust",
                )
                return _runtime_result(
                    hold,
                    accounting_state=health.state.value,
                    executed=True,
                    reasons=hold.decision_reasons,
                )

            if progress.state == EconomicExitProgressState.COMPLETED and bool(
                owner_accounting.get("all_recovery_budget_released")
            ):
                checkpoint_vcap_exit_result(
                    state=state,
                    store=lifecycle_store,
                    recovery_budget_released=True,
                    now=now,
                )
                reconciled = True
        elif cycle.planned_action == VCAPLifecycleAction.PARTIAL_RECYCLE.value:
            if partial_manifest is None or partial_store is None:
                raise RuntimeError("partial recycle manifest was not loaded")
            try:
                await synchronize_partial_recycle_executor(
                    client,
                    manifest=partial_manifest,
                    governor=governor,
                    ledger=ledger,
                    store=partial_store,
                    now=now,
                )
            except Exception as exc:
                waiting = runtime_store.transition(
                    cycle.cycle_id,
                    phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
                    now=now,
                    expected_phases=(
                        VCAPRuntimePhase.MUTATION_DISPATCHING,
                        VCAPRuntimePhase.MUTATION_DISPATCHED,
                        VCAPRuntimePhase.AWAITING_RECONCILIATION,
                    ),
                    decision_reasons=(
                        *health.reasons,
                        "partial_recycle_evidence_not_yet_available",
                    ),
                    last_error=str(exc),
                )
                return _runtime_result(
                    waiting,
                    accounting_state=health.state.value,
                    executed=True,
                    reasons=waiting.decision_reasons,
                )

            progress = assess_partial_recycle_manifest(
                partial_manifest,
                ledger=ledger,
                governor=governor,
                store=partial_store,
                now=now,
            )
            reconciled = progress.completed

    if reconciled:
        completed = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.COMPLETED,
            now=now,
            expected_phases=(
                VCAPRuntimePhase.MUTATION_DISPATCHING,
                VCAPRuntimePhase.MUTATION_DISPATCHED,
                VCAPRuntimePhase.AWAITING_RECONCILIATION,
            ),
            decision_reasons=(
                *health.reasons,
                "post_mutation_accounting_confirmed",
            ),
        )
        return _runtime_result(
            completed,
            accounting_state=health.state.value,
            executed=True,
            reasons=completed.decision_reasons,
        )

    waiting = runtime_store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
        now=now,
        expected_phases=(
            VCAPRuntimePhase.MUTATION_DISPATCHING,
            VCAPRuntimePhase.MUTATION_DISPATCHED,
            VCAPRuntimePhase.AWAITING_RECONCILIATION,
        ),
        decision_reasons=(
            *health.reasons,
            "mutation_evidence_not_yet_settled",
        ),
        last_error=cycle.last_error,
    )
    return _runtime_result(
        waiting,
        accounting_state=health.state.value,
        executed=True,
        reasons=waiting.decision_reasons,
    )


async def run_vcap_runtime_cycle(
    client: Any,
    *,
    position_id: str,
    envelope_proposal_id: str,
    governor: CapitalGovernor,
    ledger: EconomicPositionLedger,
    lifecycle_store: VCAPStateStore,
    runtime_store: VCAPRuntimeStore,
    accounting_supervisor: AccountingHealthSupervisor,
    seed_quote: Decimal,
    expected_exit_fee_bps: Decimal,
    slippage_allowance_bps: Decimal,
    now: float,
    leverage: Decimal = Decimal("1"),
    fingerprint_policy: RecoveryFingerprintPolicy | None = None,
    recovery_policy: VCAPRecoveryPolicy | None = None,
    tranche_policy: VCAPTranchePolicy | None = None,
    profit_policy: VCAPProfitExitPolicy | None = None,
    runtime_policy: VCAPRuntimePolicy | None = None,
    account_name: str = "master_account",
    controller_id: str = "spectra",
) -> dict[str, Any]:
    """Run or resume exactly one closed-loop VCAP runtime cycle.

    One cycle may authorize at most one capital-changing lifecycle action.
    A process crash after dispatch never starts a fresh cycle: the same durable
    cycle resumes in reconciliation until exact fill ownership proves what
    happened.
    """

    policy = runtime_policy or VCAPRuntimePolicy()
    cycle = runtime_store.begin_or_resume(
        position_id=position_id,
        envelope_proposal_id=envelope_proposal_id,
        now=now,
    )

    if cycle.phase in {
        VCAPRuntimePhase.MUTATION_DISPATCHING,
        VCAPRuntimePhase.MUTATION_DISPATCHED,
        VCAPRuntimePhase.AWAITING_RECONCILIATION,
    }:
        # Never blindly dispatch a second capital-changing action after restart.
        # For a tranche, a Governor receipt proves the original dispatch reached
        # the executor path. Without one, reconciliation remains conservative.
        if (
            cycle.phase == VCAPRuntimePhase.MUTATION_DISPATCHING
            and cycle.mutation_proposal_id is not None
        ):
            receipt = governor.get_execution_receipt(cycle.mutation_proposal_id)
            if receipt is not None:
                cycle = runtime_store.transition(
                    cycle.cycle_id,
                    phase=VCAPRuntimePhase.MUTATION_DISPATCHED,
                    now=now,
                    expected_phases=(VCAPRuntimePhase.MUTATION_DISPATCHING,),
                    execution_result={
                        "recovered_after_restart": True,
                        "executor_id": receipt,
                    },
                )
        return await _resume_reconciliation(
            client,
            cycle=cycle,
            governor=governor,
            ledger=ledger,
            lifecycle_store=lifecycle_store,
            runtime_store=runtime_store,
            accounting_supervisor=accounting_supervisor,
            now=now,
            policy=policy,
            account_name=account_name,
            controller_id=controller_id,
        )

    state = lifecycle_store.load(envelope_proposal_id)
    if state is None:
        conflict = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.CONFLICT,
            now=now,
            expected_phases=_PREPLAN_PHASES,
            last_error="unknown VCAP lifecycle envelope",
        )
        return _runtime_result(
            conflict,
            reasons=("vcap_lifecycle_state_missing",),
        )

    proposal_scope = _proposal_scope(
        position_id=position_id,
        ledger=ledger,
        recovery_proposal_ids=state.recovery_proposal_ids,
    )
    try:
        health = await _refresh_accounting(
            client,
            position_id=position_id,
            proposal_ids=proposal_scope,
            governor=governor,
            ledger=ledger,
            supervisor=accounting_supervisor,
            now=now,
        )
    except Exception as exc:
        hold = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.HOLD,
            now=now,
            expected_phases=_PREPLAN_PHASES,
            decision_reasons=("accounting_precheck_failed",),
            last_error=str(exc),
        )
        return _runtime_result(
            hold,
            executed=False,
            reasons=hold.decision_reasons,
        )

    if health.state != AccountingHealthState.HEALTHY:
        phase = (
            VCAPRuntimePhase.CONFLICT
            if health.state == AccountingHealthState.CONFLICT
            else VCAPRuntimePhase.HOLD
        )
        stopped = runtime_store.transition(
            cycle.cycle_id,
            phase=phase,
            now=now,
            expected_phases=_PREPLAN_PHASES,
            decision_reasons=health.reasons,
            last_error=(
                "accounting conflict"
                if phase == VCAPRuntimePhase.CONFLICT
                else "accounting precheck not healthy"
            ),
        )
        return _runtime_result(
            stopped,
            accounting_state=health.state.value,
            reasons=health.reasons,
        )

    cycle = runtime_store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.ACCOUNTING_PRECHECKED,
        now=now,
        expected_phases=_PREPLAN_PHASES,
        decision_reasons=health.reasons,
    )

    try:
        fingerprint, trading_rule, current_price = await _refresh_market(
            client,
            pair=state.pair,
            runtime_store=runtime_store,
            policy=policy,
            now=now,
        )
    except Exception as exc:
        hold = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.HOLD,
            now=now,
            expected_phases=(VCAPRuntimePhase.ACCOUNTING_PRECHECKED,),
            last_error=str(exc),
            decision_reasons=("market_refresh_failed", str(exc)),
        )
        return _runtime_result(
            hold,
            accounting_state=health.state.value,
            reasons=hold.decision_reasons,
        )

    cycle = runtime_store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.MARKET_REFRESHED,
        now=now,
        expected_phases=(VCAPRuntimePhase.ACCOUNTING_PRECHECKED,),
    )

    decision = plan_vcap_lifecycle_from_ledger(
        position_id=position_id,
        ledger=ledger,
        state=state,
        store=lifecycle_store,
        fingerprint=fingerprint,
        trading_rule=trading_rule,
        current_price=current_price,
        seed_quote=seed_quote,
        expected_exit_fee_bps=expected_exit_fee_bps,
        slippage_allowance_bps=slippage_allowance_bps,
        now=now,
        leverage=leverage,
        fingerprint_policy=fingerprint_policy,
        recovery_policy=recovery_policy,
        tranche_policy=tranche_policy,
        profit_policy=profit_policy,
        governor=governor,
        accounting_health=health,
    )

    mutation_key = None
    mutation_proposal_id = None
    if decision.action in {
        VCAPLifecycleAction.EXECUTE_TRANCHE,
        VCAPLifecycleAction.PARTIAL_RECYCLE,
        VCAPLifecycleAction.EXIT,
    }:
        mutation_key, mutation_proposal_id = _mutation_key(decision)

    cycle = runtime_store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.PLANNED,
        now=now,
        expected_phases=(VCAPRuntimePhase.MARKET_REFRESHED,),
        planned_action=decision.action.value,
        mutation_key=mutation_key,
        mutation_proposal_id=mutation_proposal_id,
        decision_reasons=decision.reasons,
    )

    if decision.action not in {
        VCAPLifecycleAction.EXECUTE_TRANCHE,
        VCAPLifecycleAction.PARTIAL_RECYCLE,
        VCAPLifecycleAction.EXIT,
    }:
        terminal_phase = (
            VCAPRuntimePhase.HOLD
            if decision.action == VCAPLifecycleAction.HOLD
            else VCAPRuntimePhase.COMPLETED
        )
        final = runtime_store.transition(
            cycle.cycle_id,
            phase=terminal_phase,
            now=now,
            expected_phases=(VCAPRuntimePhase.PLANNED,),
            decision_reasons=decision.reasons,
        )
        return _runtime_result(
            final,
            accounting_state=health.state.value,
            action=decision.action.value,
            executed=False,
            reasons=decision.reasons,
        )

    # Reconcile again immediately before mutation rather than relying only on
    # the prior freshness timestamp.
    current_state = lifecycle_store.load(envelope_proposal_id)
    if current_state is None:
        conflict = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.CONFLICT,
            now=now,
            expected_phases=(VCAPRuntimePhase.PLANNED,),
            last_error="VCAP lifecycle state disappeared before execution",
        )
        return _runtime_result(
            conflict,
            accounting_state=health.state.value,
            reasons=("vcap_lifecycle_state_missing_before_execution",),
        )

    preexec_recovery_ids = tuple(
        proposal_id
        for proposal_id in current_state.recovery_proposal_ids
        if proposal_id != mutation_proposal_id
    )
    preexec_scope = _proposal_scope(
        position_id=position_id,
        ledger=ledger,
        recovery_proposal_ids=preexec_recovery_ids,
    )
    try:
        preexec_health = await _refresh_accounting(
            client,
            position_id=position_id,
            proposal_ids=preexec_scope,
            governor=governor,
            ledger=ledger,
            supervisor=accounting_supervisor,
            now=now,
        )
    except Exception as exc:
        hold = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.HOLD,
            now=now,
            expected_phases=(VCAPRuntimePhase.PLANNED,),
            decision_reasons=("preexecution_accounting_refresh_failed",),
            last_error=str(exc),
        )
        return _runtime_result(
            hold,
            action=decision.action.value,
            executed=False,
            reasons=hold.decision_reasons,
        )
    if preexec_health.state != AccountingHealthState.HEALTHY:
        phase = (
            VCAPRuntimePhase.CONFLICT
            if preexec_health.state == AccountingHealthState.CONFLICT
            else VCAPRuntimePhase.HOLD
        )
        blocked = runtime_store.transition(
            cycle.cycle_id,
            phase=phase,
            now=now,
            expected_phases=(VCAPRuntimePhase.PLANNED,),
            decision_reasons=(
                *preexec_health.reasons,
                "preexecution_accounting_gate_blocked",
            ),
            last_error="preexecution accounting proof is not healthy",
        )
        return _runtime_result(
            blocked,
            accounting_state=preexec_health.state.value,
            action=decision.action.value,
            executed=False,
            reasons=blocked.decision_reasons,
        )

    cycle = runtime_store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.MUTATION_DISPATCHING,
        now=now,
        expected_phases=(VCAPRuntimePhase.PLANNED,),
    )

    try:
        dispatch = await execute_vcap_lifecycle_decision(
            client,
            decision=decision,
            governor=governor,
            store=lifecycle_store,
            now=now,
            position_id=position_id,
            ledger=ledger,
            trading_rule=trading_rule,
            current_price=current_price,
            accounting_health=preexec_health,
            account_name=account_name,
            controller_id=controller_id,
        )
    except Exception as exc:
        waiting = runtime_store.transition(
            cycle.cycle_id,
            phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
            now=now,
            expected_phases=(VCAPRuntimePhase.MUTATION_DISPATCHING,),
            last_error=f"dispatch outcome ambiguous: {exc}",
            decision_reasons=(
                *decision.reasons,
                "dispatch_outcome_ambiguous_reconcile_before_retry",
            ),
        )
        return _runtime_result(
            waiting,
            accounting_state=preexec_health.state.value,
            action=decision.action.value,
            executed=None,
            reasons=waiting.decision_reasons,
        )

    cycle = runtime_store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.MUTATION_DISPATCHED,
        now=now,
        expected_phases=(VCAPRuntimePhase.MUTATION_DISPATCHING,),
        execution_result=dispatch,
    )

    if decision.action == VCAPLifecycleAction.EXECUTE_TRANCHE:
        inner = dispatch.get("result")
        if isinstance(inner, dict) and inner.get("executed") is False:
            hold = runtime_store.transition(
                cycle.cycle_id,
                phase=VCAPRuntimePhase.HOLD,
                now=now,
                expected_phases=(VCAPRuntimePhase.MUTATION_DISPATCHED,),
                last_error=str(inner.get("error") or "tranche was not executed"),
                decision_reasons=(
                    *decision.reasons,
                    "capital_mutation_not_executed",
                ),
            )
            return _runtime_result(
                hold,
                accounting_state=preexec_health.state.value,
                action=decision.action.value,
                executed=False,
                reasons=hold.decision_reasons,
            )
    elif decision.action == VCAPLifecycleAction.PARTIAL_RECYCLE:
        if dispatch.get("executed") is False:
            hold = runtime_store.transition(
                cycle.cycle_id,
                phase=VCAPRuntimePhase.HOLD,
                now=now,
                expected_phases=(VCAPRuntimePhase.MUTATION_DISPATCHED,),
                last_error=str(
                    dispatch.get("error") or "partial recycle was not dispatched"
                ),
                decision_reasons=(
                    *decision.reasons,
                    "partial_recycle_not_executed",
                ),
            )
            return _runtime_result(
                hold,
                accounting_state=preexec_health.state.value,
                action=decision.action.value,
                executed=False,
                reasons=hold.decision_reasons,
            )
    elif decision.action == VCAPLifecycleAction.EXIT:
        if dispatch.get("exit_requested") is False:
            hold = runtime_store.transition(
                cycle.cycle_id,
                phase=VCAPRuntimePhase.HOLD,
                now=now,
                expected_phases=(VCAPRuntimePhase.MUTATION_DISPATCHED,),
                last_error=str(
                    dispatch.get("error") or "profit exit was not requested"
                ),
                decision_reasons=(
                    *decision.reasons,
                    "capital_mutation_not_executed",
                ),
            )
            return _runtime_result(
                hold,
                accounting_state=preexec_health.state.value,
                action=decision.action.value,
                executed=False,
                reasons=hold.decision_reasons,
            )

    return await _resume_reconciliation(
        client,
        cycle=cycle,
        governor=governor,
        ledger=ledger,
        lifecycle_store=lifecycle_store,
        runtime_store=runtime_store,
        accounting_supervisor=accounting_supervisor,
        now=now,
        policy=policy,
        account_name=account_name,
        controller_id=controller_id,
    )
