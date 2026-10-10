"""Real persistent runtime composition; only venue evidence is simulated."""

import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

import mcp_servers.hummingbot_api.tools.accounting_health as health_tool
import mcp_servers.hummingbot_api.tools.economic_exit as exit_tool
import mcp_servers.hummingbot_api.tools.execution_book as book_tool
import mcp_servers.hummingbot_api.tools.executor_create as create_tool
import mcp_servers.hummingbot_api.tools.partial_recycle as partial_tool
import mcp_servers.hummingbot_api.tools.vcap_runtime as runtime_tool
from mcp_servers.hummingbot_api.tools.cash_accounting import prepare_sale_cash_fill
from mcp_servers.hummingbot_api.tools.runtime_scheduler import run_vcap_scheduler_tick
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_exit import (
    EconomicExitManifestStore,
    create_or_load_position_exit_manifest,
)
from spectra_core.economic_position import (
    EconomicPositionLedger,
    EconomicPositionStatus,
)
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
)
from spectra_core.fill_reconciliation import TradeFillEvidence
from spectra_core.models import (
    BreakerMode,
    CapitalProposal,
    CapitalSnapshot,
    IntentKind,
)
from spectra_core.partial_recycle import PartialRecycleStore, plan_partial_recycle
from spectra_core.runtime_scheduler import RuntimeSchedulerPolicy, RuntimeSchedulerStore
from spectra_core.vcap_lifecycle import VCAPLifecycleAction
from spectra_core.vcap_runtime import VCAPRuntimePhase, VCAPRuntimeStore
from spectra_core.vcap_state import VCAPStateStore
from tests.test_residual_position_lifecycle import fixture
from tests.test_vcap_runtime_supervisor import fingerprint, lifecycle_state

D = Decimal


def pending_cycle(store, state, manifest, *, sale_kind="PARTIAL"):
    cycle = store.begin_or_resume(
        position_id=manifest.position_id,
        envelope_proposal_id=state.envelope_proposal_id,
        now=time.time(),
    )
    cycle = store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.PLANNED,
        expected_phases=(cycle.phase,),
        now=time.time(),
        planned_action=(
            VCAPLifecycleAction.PARTIAL_RECYCLE.value
            if sale_kind == "PARTIAL"
            else VCAPLifecycleAction.EXIT.value
        ),
        mutation_key=(
            "PARTIAL_RECYCLE:" + manifest.manifest_id
            if sale_kind == "PARTIAL"
            else "EXIT:" + state.envelope_proposal_id
        ),
    )
    return store.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.MUTATION_DISPATCHING,
        expected_phases=(cycle.phase,),
        now=time.time(),
    )


def integrated_case(
    tmp_path, monkeypatch, sold, *, remaining="10.04", sale_kind="PARTIAL"
):
    g, book, inputs = fixture(
        tmp_path, intent=IntentKind.RECOVERY, remaining=remaining, guarded_recovery=True
    )
    original = book.snapshot("position")
    rule = inputs["trading_rule"]
    now = time.time()
    assert g.activate_cash_authority(
        account_name="account",
        connector_name="kraken",
        quote_asset="USD",
        opening_productive_quote=D("100"),
        activation_id="verified-opening",
        activated_at=now - 3,
    )
    if sale_kind == "PARTIAL":
        planned = plan_partial_recycle(
            ledger=book,
            governor=g,
            position_id="position",
            trading_rule=rule,
            current_price=D("1"),
            expected_exit_fee_bps=D("0"),
            slippage_allowance_bps=D("0"),
            now=now,
        )
        manifest = planned.manifest
        store = PartialRecycleStore(book.path)
        assert store.claim_dispatch(
            manifest,
            ledger=book,
            governor=g,
            trading_rule=rule,
            current_price=D("1"),
            now=time.time(),
            market_observed_at=now,
        ).allowed
    else:
        store = EconomicExitManifestStore(book.path)
        manifest = create_or_load_position_exit_manifest(
            ledger=book,
            governor=g,
            position_id="position",
            trading_rule=rule,
            current_price=D("1"),
            now=now,
            store=store,
            target_net_profit_bps=D("174"),
            expected_exit_cost_bps=D("0"),
        )
        assert store.claim_dispatch(manifest_id=manifest.manifest_id, now=time.time())
    event_time = float(int(time.time()))
    status = "FILLED" if sold == D("10") else "CANCELED"
    order = ExecutorOrderEvidence(
        manifest.sell_executor_id,
        "bounded-sale",
        "LIMIT",
        "SELL",
        D("10"),
        manifest.protected_limit_price,
        status,
        sold,
        None if sold == 0 else D("1"),
        event_time,
        event_time,
    )
    trades = (
        ()
        if sold == 0
        else (
            TradeFillEvidence(
                "bounded-sale-fill",
                order.order_id,
                "account",
                "kraken",
                "XRP-USD",
                "SELL",
                sold,
                D("1"),
                D("0"),
                "USD",
                event_time,
            ),
        )
    )
    source = {e.order_snapshot.executor_id: e for e in inputs["executor_evidence"]}
    calls = {"create": 0, "market": 0}

    async def orders(client, *, executor_id):
        if executor_id == manifest.sell_executor_id:
            return ExecutorOrderSnapshot(executor_id, (order,))
        return source[executor_id].order_snapshot

    async def fills(client, *, executor_id):
        if executor_id == manifest.sell_executor_id:
            return ExecutorTradeSnapshot(executor_id, trades)
        return source[executor_id].trade_snapshot

    async def get_executor(executor_id):
        if executor_id == manifest.sell_executor_id:
            return {
                "id": executor_id,
                "status": "TERMINATED",
                "executor_type": "order_executor",
                "close_type": "COMPLETED",
                "filled_amount_quote": str(sold),
                "cum_fees_quote": "0",
                "net_pnl_quote": "0",
                "custom_info": {"realized_pnl_quote": "0"},
                "config": {
                    "id": executor_id,
                    "type": "order_executor",
                    "side": 2,
                    "connector_name": "kraken",
                    "trading_pair": "XRP-USD",
                    "execution_strategy": "LIMIT",
                    "price": str(manifest.protected_limit_price),
                    "amount": "10",
                },
            }
        assert executor_id in source
        empty = executor_id == "entry-executor"
        return {
            "id": executor_id,
            "status": "TERMINATED",
            "executor_type": "dca_executor",
            "close_type": "FAILED" if empty else "POSITION_HOLD",
            "filled_amount_quote": "0" if empty else "149.99",
            "cum_fees_quote": "0" if empty else "0.3",
            "custom_info": {"position_size_quote": "0" if empty else "0.005"},
        }

    async def no_create(**kwargs):
        calls["create"] += 1
        pytest.fail("restarting an existing immutable sale must not create an executor")

    async def market(*args, **kwargs):
        calls["market"] += 1
        value = (fingerprint(time.time()), rule, D("1"))
        return (*value, D("0.99")) if kwargs.get("include_best_bid") else value

    async def trusted_book(rule):
        return SimpleNamespace(
            best_bid=D(".99"), best_ask=D("1"), market_observed_at=time.time()
        )

    for module in (health_tool, exit_tool, partial_tool):
        monkeypatch.setattr(module, "fetch_executor_order_snapshot", orders)
        monkeypatch.setattr(module, "fetch_executor_trade_snapshot", fills)
    monkeypatch.setattr(runtime_tool, "_refresh_market", market)
    monkeypatch.setattr(runtime_tool, "fetch_protected_kraken_book", trusted_book)
    client = SimpleNamespace(
        executors=SimpleNamespace(get_executor=get_executor, create_executor=no_create)
    )
    lifecycle = VCAPStateStore(tmp_path / "lifecycle.db")
    state = replace(
        lifecycle_state(),
        recovery_proposal_ids=("owner",),
        position_base=original.active_base,
        cost_basis_quote=original.active_cost_basis_quote,
        last_known_price=D("1"),
        updated_at=now,
    )
    lifecycle.save(state)
    runtime = VCAPRuntimeStore(tmp_path / "runtime.db")
    cycle = pending_cycle(runtime, state, manifest, sale_kind=sale_kind)
    return SimpleNamespace(
        governor=g,
        ledger=book,
        manifest=manifest,
        partials=store,
        original=original,
        rule=rule,
        trades=trades,
        lifecycle=lifecycle,
        state=state,
        runtime=runtime,
        cycle=cycle,
        client=client,
        calls=calls,
    )


def restart(case):
    case.governor = CapitalGovernor(case.governor.db_path, case.governor.policy)
    case.ledger = EconomicPositionLedger(case.ledger.path)
    case.runtime = VCAPRuntimeStore(case.runtime.path)
    case.lifecycle = VCAPStateStore(case.lifecycle.path)
    return case


def run(case):
    return asyncio.run(
        runtime_tool.run_vcap_runtime_cycle(
            case.client,
            position_id="position",
            envelope_proposal_id=case.state.envelope_proposal_id,
            governor=case.governor,
            ledger=case.ledger,
            lifecycle_store=case.lifecycle,
            runtime_store=case.runtime,
            accounting_supervisor=AccountingHealthSupervisor(case.ledger.path),
            seed_quote=D("10"),
            expected_exit_fee_bps=D("0"),
            slippage_allowance_bps=D("0"),
            now=time.time(),
            account_name="account",
        )
    )


@pytest.mark.parametrize(
    "sold,cash_stage",
    [
        ("0", "before_inventory"),
        ("5", "before_inventory"),
        ("5", "after_inventory"),
        ("10", "before_inventory"),
        ("10", "after_inventory"),
    ],
)
def test_cash_and_exact_partial_manifest_resume_without_dispatch_result(
    tmp_path, monkeypatch, sold, cash_stage
):
    sold = D(sold)
    case = integrated_case(tmp_path, monkeypatch, sold)
    if sold:
        fill = case.trades[0]
        if cash_stage == "before_inventory":
            assert prepare_sale_cash_fill(
                governor=case.governor,
                manifest=case.manifest,
                fill=fill,
                sale_purpose="RECOVERY_PARTIAL",
                geometry=partial_tool._sell_geometry(fill),
            )
        else:
            original_finalize = partial_tool.finalize_sale_cash_fill

            def crash(**kwargs):
                raise RuntimeError("crash after inventory before cash")

            monkeypatch.setattr(partial_tool, "finalize_sale_cash_fill", crash)
            with pytest.raises(RuntimeError, match="crash after inventory"):
                asyncio.run(
                    partial_tool.synchronize_partial_recycle_executor(
                        case.client,
                        manifest=case.manifest,
                        governor=case.governor,
                        ledger=case.ledger,
                        store=case.partials,
                        now=time.time(),
                    )
                )
            monkeypatch.setattr(
                partial_tool, "finalize_sale_cash_fill", original_finalize
            )
            assert case.ledger.snapshot("position").active_base == D("10.04") - sold
        assert case.governor.cash_authority_snapshot().blocked
    assert case.runtime.load(case.cycle.cycle_id).execution_result is None
    result = run(restart(case))
    assert result["phase"] == "COMPLETED", result
    assert result["cycle_id"] == case.cycle.cycle_id
    assert case.calls["create"] == 0
    cash = case.governor.cash_authority_snapshot()
    assert not cash.blocked and cash.held_quote == 0
    assert cash.productive_quote == D("100") and cash.parked_quote == sold
    after = case.ledger.snapshot("position")
    assert after.active_base == D("10.04") - sold
    assert (
        case.governor.current_recovery_exposure_quote() >= after.active_cost_basis_quote
    )
    if sold == D("10"):
        assert after.status == EconomicPositionStatus.RESIDUAL
        certificate = case.ledger.get_retirement_certificate("position")
        assert certificate["capital_released_quote"] == "0"
        assert (
            after.active_entry_fees_quote
            == case.original.active_entry_fees_quote * D(".04") / D("10.04")
        )
        case.ledger.open_position(
            position_id="replacement", pair="XRP-USD", created_at=time.time()
        )
        entry = CapitalProposal(
            "replacement-entry",
            "XRP-USD",
            IntentKind.ENTRY,
            D("1"),
            account_name="account",
            connector_name="kraken",
            quote_asset="USD",
        )
        case.governor.reserve(
            entry,
            CapitalSnapshot(
                free_quote=D("100"),
                account_name="account",
                connector_name="kraken",
                quote_asset="USD",
            ),
        )
        submitted = []

        async def create_executor(*, executor_config, account_name, controller_id):
            submitted.append((executor_config, account_name, controller_id))
            return {"executor_id": "replacement-limit"}

        async def rules(**kwargs):
            return {
                "XRP-USD": {
                    "min_order_size": "1",
                    "min_notional_size": "1",
                    "min_base_amount_increment": ".1",
                    "min_price_increment": ".01",
                    "min_quote_amount_increment": ".01",
                    "supports_limit_orders": True,
                    "supports_market_orders": True,
                }
            }

        async def book(rule):
            now = time.time()
            return book_tool.FreshVenueBook(
                "XRP-USD",
                "XRPUSD",
                D(".99"),
                D("1"),
                ((D(".99"), D("100")),),
                ((D("1"), D("100")),),
                now,
                now,
                "kraken_public_rest_depth",
                "a" * 64,
                venue_min_order_size=D("1"),
                venue_min_cost_quote=D("1"),
                venue_base_asset="XRP",
                venue_quote_asset="USD",
                venue_rules_observed_at=now,
                venue_rules_sha256="e" * 64,
            )

        monkeypatch.setattr(create_tool.settings, "agent_slug", "spectra")
        monkeypatch.setattr(create_tool, "get_capital_governor", lambda: case.governor)
        monkeypatch.setattr(
            create_tool, "get_economic_position_ledger", lambda: case.ledger
        )
        monkeypatch.setattr(create_tool, "get_breaker_mode", lambda: BreakerMode.NORMAL)
        monkeypatch.setattr(book_tool, "fetch_protected_kraken_book", book)
        create_tool.trading_rules_cache.clear()
        case.client.connectors = SimpleNamespace(get_trading_rules=rules)
        case.client.executors.create_executor = create_executor
        created = asyncio.run(
            create_tool.create_dca_executor(
                case.client,
                connector_name="kraken",
                trading_pair="XRP-USD",
                side=1,
                amounts_quote=[1],
                prices=[1],
                leverage=1,
                account_name="account",
                controller_id="spectra",
                spectra_proposal_id=entry.proposal_id,
                spectra_intent="ENTRY",
                spectra_position_id="replacement",
                spectra_max_price_slippage_bps=0,
                spectra_fee_allowance_bps=0,
            )
        )
        assert created.get("executor_id") == "replacement-limit", created
        config, account, controller = submitted[0]
        assert len(submitted) == 1 and account == "account" and controller == "spectra"
        assert (
            config["type"] == "order_executor"
            and config["execution_strategy"] == "LIMIT"
        )
        assert D(str(config["price"])) == 1 and D(str(config["amount"])) == 1
        assert (
            case.governor.get_protected_buy_submission(entry.proposal_id)[
                "authorized_quote_budget"
            ]
            == "1"
        )
        assert case.ledger.snapshot("position").active_base == D(".04")
        assert case.governor.cash_authority_snapshot().parked_quote == sold
        assert case.governor.cash_authority_snapshot().productive_quote == D("99")
    else:
        assert after.status == EconomicPositionStatus.ACTIVE
        next_decision = plan_partial_recycle(
            ledger=case.ledger,
            governor=case.governor,
            position_id="position",
            trading_rule=case.rule,
            current_price=D("1"),
            expected_exit_fee_bps=D("0"),
            slippage_allowance_bps=D("0"),
            now=time.time(),
        )
        assert next_decision.manifest.manifest_id != case.manifest.manifest_id
        assert (
            next_decision.manifest.predecessor_manifest_id == case.manifest.manifest_id
        )


@pytest.mark.parametrize("kind", ["RESIDUAL", "CLOSED"])
def test_halted_scheduler_resumes_crash_after_certificate_before_runtime_complete(
    tmp_path, monkeypatch, kind
):
    case = integrated_case(
        tmp_path, monkeypatch, D("10"), remaining="10" if kind == "CLOSED" else "10.04"
    )
    original_transition = case.runtime.transition

    def crash_complete(*args, **kwargs):
        if kwargs.get("phase") == VCAPRuntimePhase.COMPLETED:
            raise RuntimeError("crash after terminal certificate")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(case.runtime, "transition", crash_complete)
    with pytest.raises(RuntimeError, match="crash after terminal certificate"):
        run(case)
    assert case.ledger.get_retirement_certificate("position")
    assert (
        case.runtime.load(case.cycle.cycle_id).phase
        == VCAPRuntimePhase.MUTATION_DISPATCHING
    )
    assert not case.governor.cash_authority_snapshot().blocked
    assert case.ledger.snapshot("position").status.value == kind
    restart(case)
    scheduler = RuntimeSchedulerStore(tmp_path / "scheduler.db")
    result = asyncio.run(
        run_vcap_scheduler_tick(
            case.client,
            governor=case.governor,
            ledger=case.ledger,
            lifecycle_store=case.lifecycle,
            runtime_store=case.runtime,
            scheduler_store=scheduler,
            accounting_supervisor=AccountingHealthSupervisor(case.ledger.path),
            seed_quote=D("10"),
            expected_exit_fee_bps=D("0"),
            slippage_allowance_bps=D("0"),
            now=time.time(),
            breaker_mode=BreakerMode.HALTED,
            scheduler_policy=RuntimeSchedulerPolicy(max_positions_per_tick=1),
            account_name="account",
        )
    )
    assert result["selected_position_ids"] == ["position"]
    resumed = result["results"][0]["runtime"]
    assert resumed["phase"] == "COMPLETED", resumed["last_error"]
    assert case.calls["create"] == 0
    assert case.runtime.list_pending_reconciliation_position_ids() == ()


def test_pending_discovery_is_bounded_and_ignores_superseded_history(tmp_path):
    store = VCAPRuntimeStore(tmp_path / "runtime.db")
    state = lifecycle_state()
    for index in range(3):
        manifest = SimpleNamespace(
            position_id=f"position-{index}", manifest_id=f"manifest-{index}"
        )
        cycle = pending_cycle(store, state, manifest)
        if index == 0:
            store.transition(
                cycle.cycle_id,
                phase=VCAPRuntimePhase.COMPLETED,
                expected_phases=(cycle.phase,),
                now=time.time(),
            )
            store.begin_or_resume(
                position_id=manifest.position_id,
                envelope_proposal_id=state.envelope_proposal_id,
                now=time.time(),
            )
    assert store.list_pending_reconciliation_position_ids(limit=1) == ("position-1",)
    assert store.list_pending_reconciliation_position_ids() == (
        "position-1",
        "position-2",
    )
    with pytest.raises(ValueError):
        store.list_pending_reconciliation_position_ids(limit=True)


@pytest.mark.parametrize("cash_stage", ["before_inventory", "after_inventory"])
def test_whole_sale_cash_finishes_after_inventory_close_without_dispatch_result(
    tmp_path, monkeypatch, cash_stage
):
    case = integrated_case(
        tmp_path, monkeypatch, D("10"), remaining="10", sale_kind="WHOLE"
    )
    fill = case.trades[0]
    if cash_stage == "before_inventory":
        assert prepare_sale_cash_fill(
            governor=case.governor,
            manifest=case.manifest,
            fill=fill,
            sale_purpose="WHOLE_EXIT",
            geometry=exit_tool._sell_geometry(fill),
        )
    else:
        original_finalize = exit_tool.finalize_sale_cash_fill

        def crash(**kwargs):
            raise RuntimeError("crash after whole inventory before cash")

        monkeypatch.setattr(exit_tool, "finalize_sale_cash_fill", crash)
        with pytest.raises(RuntimeError, match="crash after whole inventory"):
            asyncio.run(
                exit_tool.synchronize_economic_position_exit(
                    case.client,
                    manifest=case.manifest,
                    governor=case.governor,
                    ledger=case.ledger,
                    store=case.partials,
                    now=time.time(),
                )
            )
        monkeypatch.setattr(exit_tool, "finalize_sale_cash_fill", original_finalize)
        assert case.ledger.snapshot("position").status == EconomicPositionStatus.CLOSED
    assert case.governor.cash_authority_snapshot().blocked
    assert case.runtime.load(case.cycle.cycle_id).execution_result is None
    result = run(restart(case))
    assert result["phase"] == "COMPLETED", result["last_error"]
    assert case.ledger.get_retirement_certificate("position")["kind"] == "FLAT_TERMINAL"
    cash = case.governor.cash_authority_snapshot()
    assert not cash.blocked and cash.held_quote == 0 and cash.parked_quote == 0
    assert cash.productive_quote == D("110")
    # Only the exact five units of sold basis release their recovery budget.
    # The original unproven unused authorization remains conservatively owned.
    assert case.governor.current_recovery_exposure_quote() == D("95")
    assert (
        case.ledger.get_retirement_certificate("position")["capital_released_quote"]
        == "0"
    )
    assert case.calls["create"] == 0
    market_calls = case.calls["market"]
    blocked = run(case)
    assert blocked["phase"] == "HOLD" and blocked["executed"] is False
    assert blocked["reasons"] == ["inactive_position_new_mutation_blocked"]
    assert case.calls["market"] == market_calls


def test_flat_selected_inventory_does_not_complete_a_running_sell(
    tmp_path, monkeypatch
):
    case = integrated_case(tmp_path, monkeypatch, D("10"), remaining="10")
    original_get = case.client.executors.get_executor
    running = [True]

    async def executor(executor_id):
        raw = await original_get(executor_id)
        if executor_id == case.manifest.sell_executor_id and running[0]:
            return {**raw, "status": "RUNNING", "close_type": None}
        return raw

    case.client.executors.get_executor = executor
    first = run(case)
    assert first["phase"] == "AWAITING_RECONCILIATION"
    assert case.ledger.snapshot("position").active_base == 0
    assert case.partials.terminal_proof(case.manifest.manifest_id) is None
    assert case.ledger.get_retirement_certificate("position") is None
    assert not case.governor.cash_authority_snapshot().blocked
    assert case.governor.cash_authority_snapshot().parked_quote == D("10")
    running[0] = False
    second = run(restart(case))
    assert second["phase"] == "COMPLETED", second["last_error"]
    assert case.ledger.get_retirement_certificate("position")["kind"] == "FLAT_TERMINAL"
    assert case.calls["create"] == 0


def add_protected_buy_policy_breach(case, monkeypatch, *, lagging=False, breached=True):
    """Create a real bounded RECOVERY claim, then observe a venue order breach."""
    proposal = CapitalProposal(
        "policy-source",
        "XRP-USD",
        IntentKind.RECOVERY,
        D("1"),
        account_name="account",
        connector_name="kraken",
        quote_asset="USD",
    )
    assert case.governor.reserve(
        proposal,
        CapitalSnapshot(
            free_quote=D("100"),
            account_name="account",
            connector_name="kraken",
            quote_asset="USD",
        ),
    ).approved

    async def rules(**kwargs):
        return {
            "XRP-USD": {
                "min_order_size": "1",
                "min_notional_size": "1",
                "min_base_amount_increment": ".1",
                "min_price_increment": ".01",
                "min_quote_amount_increment": ".01",
                "supports_limit_orders": True,
            }
        }

    async def fresh(rule):
        observed = time.time()
        return book_tool.FreshVenueBook(
            "XRP-USD",
            "XRPUSD",
            D(".99"),
            D("1"),
            ((D(".99"), D("100")),),
            ((D("1"), D("100")),),
            observed,
            observed,
            "test-fresh-Kraken-Depth",
            "d" * 64,
            venue_min_order_size=D("1"),
            venue_min_cost_quote=D("1"),
            venue_base_asset="XRP",
            venue_quote_asset="USD",
            venue_rules_observed_at=observed,
            venue_rules_sha256="f" * 64,
        )

    original_create = case.client.executors.create_executor

    async def create(**kwargs):
        assert kwargs["executor_config"]["execution_strategy"] == "LIMIT"
        return {"executor_id": "policy-source-executor"}

    monkeypatch.setattr(create_tool.settings, "agent_slug", "spectra")
    monkeypatch.setattr(create_tool, "get_capital_governor", lambda: case.governor)
    monkeypatch.setattr(
        create_tool, "get_economic_position_ledger", lambda: case.ledger
    )
    monkeypatch.setattr(create_tool, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    monkeypatch.setattr(book_tool, "fetch_protected_kraken_book", fresh)
    create_tool.trading_rules_cache.clear()
    case.client.connectors = SimpleNamespace(get_trading_rules=rules)
    case.client.executors.create_executor = create
    created = asyncio.run(
        create_tool.create_dca_executor(
            case.client,
            connector_name="kraken",
            trading_pair="XRP-USD",
            side=1,
            amounts_quote=[1],
            prices=[1],
            leverage=1,
            account_name="account",
            spectra_proposal_id=proposal.proposal_id,
            spectra_intent="RECOVERY",
            spectra_position_id="position",
            spectra_max_price_slippage_bps=0,
            spectra_fee_allowance_bps=0,
        )
    )
    assert created.get("executor_id") == "policy-source-executor", created
    case.state = replace(
        case.state,
        recovery_proposal_ids=(*case.state.recovery_proposal_ids, proposal.proposal_id),
        updated_at=time.time(),
    )
    case.lifecycle.save(case.state)
    case.client.executors.create_executor = original_create
    assert case.governor.cash_authority_snapshot().productive_quote == D("99")
    occurred = time.time() - 0.01
    order = ExecutorOrderEvidence(
        "policy-source-executor",
        "policy-source-order",
        "LIMIT",
        "BUY",
        D("1"),
        D("1.1") if breached else D("1"),
        "FILLED",
        D("2") if lagging else D("1"),
        D(".9"),
        occurred,
        occurred,
    )
    trade = TradeFillEvidence(
        "policy-source-fill",
        order.order_id,
        "account",
        "kraken",
        "XRP-USD",
        "BUY",
        D("1"),
        D(".9"),
        D("0"),
        "USD",
        occurred,
    )
    original_orders = health_tool.fetch_executor_order_snapshot
    original_trades = health_tool.fetch_executor_trade_snapshot

    async def orders(client, *, executor_id):
        if executor_id == "policy-source-executor":
            return ExecutorOrderSnapshot(executor_id, (order,))
        return await original_orders(client, executor_id=executor_id)

    async def trades(client, *, executor_id):
        if executor_id == "policy-source-executor":
            return ExecutorTradeSnapshot(executor_id, (trade,))
        return await original_trades(client, executor_id=executor_id)

    monkeypatch.setattr(health_tool, "fetch_executor_order_snapshot", orders)
    monkeypatch.setattr(health_tool, "fetch_executor_trade_snapshot", trades)
    return proposal


@pytest.mark.parametrize("sale_kind", ["PARTIAL", "WHOLE"])
def test_policy_hold_restart_records_existing_sell_truth_without_cash_finality(
    tmp_path,
    monkeypatch,
    sale_kind,
):
    case = integrated_case(
        tmp_path,
        monkeypatch,
        D("5"),
        sale_kind=sale_kind,
        remaining="10" if sale_kind == "WHOLE" else "10.04",
    )
    source = add_protected_buy_policy_breach(case, monkeypatch)
    result = run(restart(case))
    assert result["phase"] == "AWAITING_RECONCILIATION", result
    assert result["accounting_state"] == "HOLD"
    assert "protected_buy_hold_existing_sell_reconciliation_only" in result["reasons"]
    assert case.governor.get_protected_buy_conflicts(source.proposal_id)
    assert case.ledger.snapshot(
        "position"
    ).active_base == case.original.active_base - D("4")
    assert case.partials.fill_recorded("bounded-sale-fill")
    cash = case.governor.cash_authority_snapshot()
    assert cash.productive_quote == D("99") and cash.parked_quote == 0
    assert cash.held_quote == D("5") and cash.blocked
    assert case.ledger.get_retirement_certificate("position") is None
    assert case.calls["create"] == 0 and case.calls["market"] == 0
    if sale_kind == "PARTIAL":
        assert case.partials.terminal_proof(case.manifest.manifest_id) is None
    else:
        assert not case.partials.finality_confirmed(case.manifest.manifest_id)
    again = run(restart(case))
    assert again["phase"] == "AWAITING_RECONCILIATION"
    assert case.ledger.snapshot(
        "position"
    ).active_base == case.original.active_base - D("4")
    assert case.governor.cash_authority_snapshot().held_quote == D("5")


@pytest.mark.parametrize("sale_kind", ["PARTIAL", "WHOLE"])
def test_policy_conflict_with_lagging_buy_coverage_does_not_bypass_accounting_hold(
    tmp_path,
    monkeypatch,
    sale_kind,
):
    case = integrated_case(
        tmp_path,
        monkeypatch,
        D("5"),
        sale_kind=sale_kind,
        remaining="10" if sale_kind == "WHOLE" else "10.04",
    )
    add_protected_buy_policy_breach(case, monkeypatch, lagging=True)
    result = run(restart(case))
    assert result["phase"] == "AWAITING_RECONCILIATION", result
    assert (
        "protected_buy_hold_existing_sell_reconciliation_only" not in result["reasons"]
    )
    assert case.ledger.snapshot(
        "position"
    ).active_base == case.original.active_base + D("1")
    assert not case.partials.fill_recorded("bounded-sale-fill")
    assert case.governor.cash_authority_snapshot().held_quote == 0
    assert case.calls["create"] == 0


@pytest.mark.parametrize("sale_kind", ["PARTIAL", "WHOLE"])
def test_structural_source_conflict_still_blocks_policy_hold_sell_exception(
    tmp_path,
    monkeypatch,
    sale_kind,
):
    case = integrated_case(
        tmp_path,
        monkeypatch,
        D("5"),
        sale_kind=sale_kind,
        remaining="10" if sale_kind == "WHOLE" else "10.04",
    )
    source = add_protected_buy_policy_breach(case, monkeypatch)
    health = asyncio.run(
        health_tool.synchronize_position_accounting_health(
            case.client,
            position_id="position",
            proposal_ids=("owner", "entry-owner", source.proposal_id),
            governor=case.governor,
            ledger=case.ledger,
            supervisor=AccountingHealthSupervisor(case.ledger.path),
            now=time.time(),
        )
    )
    assert "protected_buy_requires_reconciliation" in health.reasons
    original = health_tool.fetch_executor_order_snapshot

    async def cross_proposal_order(client, *, executor_id):
        snapshot = await original(client, executor_id=executor_id)
        if executor_id == "policy-source-executor":
            return replace(
                snapshot, orders=(replace(snapshot.orders[0], order_id="buy"),)
            )
        return snapshot

    monkeypatch.setattr(
        health_tool, "fetch_executor_order_snapshot", cross_proposal_order
    )
    result = run(restart(case))
    assert result["phase"] == "CONFLICT" and result["accounting_state"] == "CONFLICT"
    assert case.ledger.snapshot(
        "position"
    ).active_base == case.original.active_base + D("1")
    assert not case.partials.fill_recorded("bounded-sale-fill")
    assert case.governor.cash_authority_snapshot().held_quote == 0
    assert case.calls["create"] == 0


def test_healthy_scoped_buy_growth_restart_preserves_existing_whole_sell_truth(
    tmp_path,
    monkeypatch,
):
    case = integrated_case(
        tmp_path,
        monkeypatch,
        D("5"),
        sale_kind="WHOLE",
        remaining="10",
    )
    source = add_protected_buy_policy_breach(case, monkeypatch, breached=False)
    result = run(restart(case))
    assert result["phase"] == "AWAITING_RECONCILIATION", result
    assert result["accounting_state"] == "HEALTHY"
    assert not case.governor.get_protected_buy_conflicts(source.proposal_id)
    assert case.ledger.snapshot("position").active_base == D("6")
    assert case.ledger.get_lot("trade-fill:policy-source-fill").base_remaining == D("1")
    assert case.partials.fill_recorded("bounded-sale-fill")
    cash = case.governor.cash_authority_snapshot()
    assert cash.productive_quote == D("99") and cash.parked_quote == 0
    assert cash.held_quote == D("5") and cash.blocked
    assert case.ledger.get_retirement_certificate("position") is None
    assert not case.partials.finality_confirmed(case.manifest.manifest_id)
    assert case.calls["create"] == 0 and case.calls["market"] == 0
    again = run(restart(case))
    assert again["phase"] == "AWAITING_RECONCILIATION"
    assert case.ledger.snapshot("position").active_base == D("6")
    assert case.governor.cash_authority_snapshot().held_quote == D("5")
