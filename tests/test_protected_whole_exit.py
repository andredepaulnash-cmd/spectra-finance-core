"""Independent adversarial tests for exact protected whole-position exits.

The Governor, economic ledger and manifest persistence are real. Only venue
executor/order/trade observations are controlled by these fixtures.
"""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import mcp_servers.hummingbot_api.tools.economic_exit as exit_tool
import mcp_servers.hummingbot_api.tools.vcap_runtime as runtime_tool
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.economic_exit import (
    EconomicExitManifestStatus,
    EconomicExitManifestStore,
    EconomicExitProgressState,
    assess_position_exit_manifest,
    create_or_load_position_exit_manifest,
    validate_manifest_against_ledger,
)
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
)
from spectra_core.fill_reconciliation import (
    EconomicFillRole,
    LiveFillLedgerReconciler,
    TradeFillEvidence,
)
from spectra_core.models import BreakerMode, IntentKind
from spectra_core.partial_recycle import (
    PartialRecyclePolicy,
    PartialRecycleStore,
    plan_partial_recycle,
)
from spectra_core.vcap_lifecycle import VCAPLifecycleAction
from spectra_core.vcap_runtime import VCAPRuntimePhase, VCAPRuntimeStore
from spectra_core.vcap_state import (
    VCAPLifecycleState,
    VCAPLifecycleStatus,
    VCAPStateStore,
)
from tests.test_unified_economic_exit import (
    book,
    build_owned_position,
    exit_decision,
    governor,
    prepare_execution,
    trading_rule,
)
from tests.test_vcap_runtime_supervisor import healthy

D = Decimal


class LookupStatusError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"Executor lookup HTTP {status}")
        self.status = status
        self.response = SimpleNamespace(status_code=status)


def protected_decision(**changes):
    values = dict(target_net_profit_bps=D("174"), expected_exit_cost_bps=D("45"))
    values.update(changes)
    return replace(exit_decision(), **values)


def create_manifest(g, ledger, *, rule=None, **changes):
    values = dict(
        target_net_profit_bps=D("174"),
        expected_exit_cost_bps=D("45"),
        protected_floor_price=D("0.49"),
    )
    values.update(changes)
    return create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=trading_rule() if rule is None else rule,
        current_price=D("0.50"),
        now=2000,
        **values,
    )


@pytest.fixture
def whole_case(tmp_path, monkeypatch):
    g, ledger = build_owned_position(tmp_path)
    manifest = create_manifest(g, ledger)
    monkeypatch.setattr(exit_tool, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    return g, ledger, manifest


class EvidenceExecutors:
    def __init__(self, manifest):
        self.manifest = manifest
        self.created = []
        self.stopped = []
        self.known = False
        self.lookup_failure = None
        self.create_failure = None
        self.stop_failure = None
        self.source_status = "TERMINATED"
        self.source_close_type = "POSITION_HOLD"
        self.complete_stop = False
        self.stopped_sources = set()
        self.sell_status = "RUNNING"
        self.sell_close_type = None

    async def get_executor(self, executor_id):
        if executor_id in self.manifest.executor_ids:
            owner = next(
                owner
                for owner in self.manifest.owners
                if owner.executor_id == executor_id
            )
            return dict(
                id=executor_id,
                executor_type="dca_executor",
                status=(
                    "TERMINATED"
                    if executor_id in self.stopped_sources
                    else self.source_status
                ),
                close_type=(
                    "POSITION_HOLD"
                    if executor_id in self.stopped_sources
                    else self.source_close_type
                ),
                filled_amount_quote=str(owner.target_cost_basis_quote),
                cum_fees_quote=str(owner.target_entry_fees_quote),
                position_size_quote=str(owner.target_cost_basis_quote),
                net_pnl_quote="0",
                custom_info={"realized_pnl_quote": "0"},
            )
        assert executor_id == self.manifest.sell_executor_id
        if self.lookup_failure is not None:
            if isinstance(self.lookup_failure, Exception):
                raise self.lookup_failure
            return self.lookup_failure
        if not self.known:
            raise LookupStatusError(404)
        return dict(
            id=executor_id,
            executor_type="order_executor",
            status=self.sell_status,
            close_type=self.sell_close_type,
            filled_amount_quote="0",
            cum_fees_quote="0",
            net_pnl_quote="0",
            custom_info={"realized_pnl_quote": "0"},
            config=dict(
                id=executor_id,
                type="order_executor",
                connector_name="kraken",
                trading_pair="XRP-USD",
                side=2,
                amount=str(self.manifest.target_active_base),
                price=str(self.manifest.protected_limit_price),
                execution_strategy="LIMIT",
            ),
        )

    async def stop_executor(self, *, executor_id, keep_position):
        assert executor_id in self.manifest.executor_ids
        self.stopped.append((executor_id, keep_position))
        if self.stop_failure is not None:
            raise self.stop_failure
        if self.complete_stop:
            self.stopped_sources.add(executor_id)
        return {"status": "stopped", "executor_id": executor_id}

    async def create_executor(self, *, executor_config, account_name, controller_id):
        self.created.append((executor_config, account_name, controller_id))
        self.known = True
        if self.create_failure is not None:
            raise self.create_failure
        return {"executor_id": executor_config["id"], "status": "RUNNING"}


class EvidenceClient:
    def __init__(self, manifest):
        self.executors = EvidenceExecutors(manifest)


def evidence_order(manifest, *, order_id="whole-sell-order", **changes):
    values = dict(
        executor_id=manifest.sell_executor_id,
        order_id=order_id,
        order_type="LIMIT",
        side="SELL",
        amount=manifest.target_active_base,
        price=manifest.protected_limit_price,
        status="OPEN",
        filled_amount=D("0"),
        average_fill_price=None,
        created_at=2001,
        updated_at=2002,
    )
    values.update(changes)
    return ExecutorOrderEvidence(**values)


def install_evidence(
    monkeypatch,
    manifest,
    fills=(),
    *,
    orders=None,
    status="OPEN",
    source_orders=(),
    source_trades=(),
    source_snapshot_failure=None,
):
    if orders is None:
        orders = (
            evidence_order(
                manifest,
                status=status,
                filled_amount=sum((fill.amount_base for fill in fills), D("0")),
                average_fill_price=None if not fills else fills[0].price,
            ),
        )

    async def order_snapshot(client, *, executor_id):
        if executor_id in manifest.executor_ids:
            if source_snapshot_failure is not None:
                raise source_snapshot_failure
            rows = tuple(row for row in source_orders if row.executor_id == executor_id)
        else:
            assert executor_id == manifest.sell_executor_id
            rows = tuple(orders)
        return ExecutorOrderSnapshot(executor_id=executor_id, orders=rows)

    async def trade_snapshot(client, *, executor_id):
        if executor_id in manifest.executor_ids:
            if source_snapshot_failure is not None:
                raise source_snapshot_failure
            rows = tuple(source_trades)
        else:
            assert executor_id == manifest.sell_executor_id
            rows = tuple(fills)
        return ExecutorTradeSnapshot(executor_id=executor_id, trades=rows)

    monkeypatch.setattr(exit_tool, "fetch_executor_order_snapshot", order_snapshot)
    monkeypatch.setattr(exit_tool, "fetch_executor_trade_snapshot", trade_snapshot)
    original_lookup = EvidenceExecutors.get_executor

    async def exact_lookup(self, executor_id):
        raw = await original_lookup(self, executor_id)
        if executor_id == manifest.sell_executor_id and isinstance(raw, dict):
            raw = dict(
                raw,
                filled_amount_quote=str(
                    sum((trade.quote_amount for trade in fills), D("0"))
                ),
            )
        return raw

    monkeypatch.setattr(EvidenceExecutors, "get_executor", exact_lookup)


def fill(manifest, trade_id, amount, *, price=None, fee=None, **changes):
    sale_price = manifest.protected_limit_price if price is None else price
    amount = D(amount)
    values = dict(
        trade_id=trade_id,
        order_id="whole-sell-order",
        account_name="master_account",
        connector_name="kraken",
        pair="XRP-USD",
        side="SELL",
        amount_base=amount,
        price=sale_price,
        fee_paid=amount * sale_price * D("0.004") if fee is None else fee,
        fee_currency="USD",
        occurred_at=2002 if trade_id.endswith("1") else 2003,
    )
    values.update(changes)
    return TradeFillEvidence(**values)


def execute(case, client, *, decision=None, rule=None):
    g, ledger, manifest = case
    with patch.object(exit_tool.time, "time", return_value=2010):
        return asyncio.run(
            exit_tool.execute_economic_position_exit(
                client,
                decision=protected_decision() if decision is None else decision,
                governor=g,
                ledger=ledger,
                position_id=manifest.position_id,
                trading_rule=trading_rule() if rule is None else rule,
                now=2010,
                best_bid=manifest.protected_limit_price or D("0.50"),
                market_observed_at=2010,
            )
        )


def synchronize(case, client, *, ledger=None, manifest=None):
    g, original_ledger, original_manifest = case
    ledger = original_ledger if ledger is None else ledger
    manifest = original_manifest if manifest is None else manifest
    # These cases supply confirmed venue order/trade snapshots for an existing
    # synthetic seller. First-submission/404 behavior is exercised via execute.
    client.executors.known = True
    return asyncio.run(
        exit_tool.synchronize_economic_position_exit(
            client,
            manifest=manifest,
            governor=g,
            ledger=ledger,
            store=EconomicExitManifestStore(ledger.path),
            now=2020,
        )
    )


def test_price_floor_covers_joint_basis_fees_and_rounds_up(tmp_path):
    g, ledger = build_owned_position(tmp_path)
    manifest = create_manifest(g, ledger, protected_floor_price=D("0.001"))
    price = manifest.protected_limit_price
    required = D("18") * D("1.0174")
    assert price == D("0.4599")
    assert D("40") * price * D("0.9955") >= required
    assert D("40") * (price - D("0.0001")) * D("0.9955") < required


def test_entry_and_recovery_fees_are_included_once_in_joint_price_floor(tmp_path):
    g = governor(tmp_path)
    ledger = book(tmp_path)
    for proposal_id, intent, base, basis, fee in (
        ("entry-fee", IntentKind.ENTRY, "20", "10", "0.20"),
        ("recovery-fee", IntentKind.RECOVERY, "10", "4", "0.04"),
    ):
        prepare_execution(
            g,
            proposal_id=proposal_id,
            executor_id=proposal_id + "-executor",
            intent=intent,
            amount=basis,
        )
        record = (
            ledger.record_entry_fill
            if intent == IntentKind.ENTRY
            else ledger.record_vcap_fill
        )
        record(
            governor=g,
            position_id="xrp-position-1",
            lot_id=proposal_id + "-lot",
            proposal_id=proposal_id,
            base_amount=D(base),
            cost_basis_quote=D(basis),
            entry_fees_quote=D(fee),
            occurred_at=1010 if intent == IntentKind.ENTRY else 1020,
        )
    manifest = create_manifest(g, ledger, protected_floor_price=D("0.001"))
    price = manifest.protected_limit_price
    required = D("14.24") * D("1.0174")
    assert manifest.target_entry_fees_quote == D("0.24")
    assert D("30") * price * D("0.9955") >= required
    assert D("30") * (price - D("0.0001")) * D("0.9955") < required


def test_decision_floor_and_exact_active_inventory_are_preserved(whole_case):
    g, ledger, manifest = whole_case
    assert manifest.protected_limit_price == D("0.49")
    assert manifest.target_active_base == D("40")
    assert manifest.target_cost_basis_quote == D("18")
    assert set(manifest.lot_ids) == {"entry-lot", "recovery-lot-1", "recovery-lot-2"}
    assert set(manifest.proposal_ids) == {"entry-1", "recovery-1", "recovery-2"}
    assert manifest.sell_executor_id not in manifest.executor_ids
    assert "retained-43" not in manifest.lot_ids
    assert "unattributed-20" not in manifest.lot_ids


def test_changed_policy_and_price_cannot_lower_a_saved_target(whole_case):
    g, ledger, manifest = whole_case
    resumed = create_or_load_position_exit_manifest(
        ledger=ledger,
        governor=g,
        position_id=manifest.position_id,
        trading_rule=trading_rule(),
        current_price=D("0.01"),
        now=2010,
        target_net_profit_bps=D("100"),
        expected_exit_cost_bps=D("0"),
        protected_floor_price=D("0.01"),
    )
    assert resumed == manifest
    assert EconomicExitManifestStore(ledger.path).load(manifest.manifest_id) == manifest


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_net_profit_bps", D("-1")),
        ("target_net_profit_bps", D("NaN")),
        ("expected_exit_cost_bps", D("-1")),
        ("expected_exit_cost_bps", D("10000")),
        ("expected_exit_cost_bps", D("Infinity")),
        ("protected_floor_price", D("0")),
        ("protected_floor_price", D("NaN")),
    ],
)
def test_invalid_profit_geometry_cannot_create_protected_manifest(
    tmp_path, field, value
):
    g, ledger = build_owned_position(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        create_manifest(g, ledger, **{field: value})
    assert (
        EconomicExitManifestStore(ledger.path).open_for_position("xrp-position-1")
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("supports_limit_orders", False),
        ("min_price_increment", D("0")),
        ("min_price_increment", D("NaN")),
        ("min_base_amount_increment", D("0")),
        ("min_base_amount_increment", D("3")),
    ],
)
def test_invalid_exchange_protection_geometry_is_rejected(tmp_path, field, value):
    g, ledger = build_owned_position(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        create_manifest(g, ledger, rule=replace(trading_rule(), **{field: value}))


def test_dispatch_is_one_exact_joint_limit_after_keep_position_true(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.source_status = "RUNNING"
    client.executors.source_close_type = None
    client.executors.complete_stop = True
    install_evidence(monkeypatch, manifest)
    result = execute(whole_case, client)
    assert result["exit_requested"] is True
    assert len(client.executors.created) == 1
    config, account, controller = client.executors.created[0]
    assert config["id"] == manifest.sell_executor_id
    assert config["execution_strategy"] == "LIMIT"
    assert D(config["price"]) == D("0.49")
    assert D(config["amount"]) == D("40")
    assert account == "master_account" and controller == "spectra"
    assert set(client.executors.stopped) == {
        (owner.executor_id, True) for owner in manifest.owners
    }
    assert ledger.snapshot(manifest.position_id).active_base == D("40")
    assert g.current_recovery_exposure_quote() == D("8")
    assert (
        EconomicExitManifestStore(ledger.path).load(manifest.manifest_id).status
        == EconomicExitManifestStatus.OPEN
    )
    execute(whole_case, client)
    assert len(client.executors.created) == 1


@pytest.mark.parametrize(
    "status,close_type",
    [
        ("RUNNING", None),
        ("TERMINATED", "TAKE_PROFIT"),
        ("SHUTTING_DOWN", "POSITION_HOLD"),
    ],
)
def test_stop_ack_is_not_permission_to_sell_before_passivity(
    whole_case, monkeypatch, status, close_type
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.source_status = status
    client.executors.source_close_type = close_type
    install_evidence(monkeypatch, manifest)
    result = execute(whole_case, client)
    assert result["executed"] is False
    assert result["economic_exit_state"] != EconomicExitProgressState.COMPLETED.value
    assert client.executors.created == []
    assert ledger.snapshot(manifest.position_id).active_base == D("40")


def test_terminal_source_with_open_order_cannot_dispatch_second_seller(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    source_order = evidence_order(
        manifest,
        executor_id="exec-entry-1",
        order_id="old-source-sell",
        amount=D("20"),
        status="OPEN",
    )
    install_evidence(monkeypatch, manifest, source_orders=(source_order,))
    result = execute(whole_case, client)
    assert result["executed"] is False
    assert result["economic_exit_state"] != EconomicExitProgressState.COMPLETED.value
    assert client.executors.created == []
    assert ledger.snapshot(manifest.position_id).active_base == D("40")


@pytest.mark.parametrize(
    "failure", [TimeoutError("source snapshot timeout"), LookupStatusError(500)]
)
def test_unavailable_source_order_finality_holds_inventory(
    whole_case, monkeypatch, failure
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    install_evidence(monkeypatch, manifest, source_snapshot_failure=failure)
    try:
        result = execute(whole_case, client)
        assert result["exit_requested"] is False
    except (RuntimeError, TimeoutError):
        pass
    assert client.executors.created == []
    assert ledger.snapshot(manifest.position_id).active_base == D("40")


def test_terminal_source_sell_with_missing_trades_blocks_new_seller(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    source_order = evidence_order(
        manifest,
        executor_id="exec-entry-1",
        order_id="terminal-source-sell",
        amount=D("2"),
        status="FILLED",
        filled_amount=D("2"),
        average_fill_price=D("0.49"),
    )
    install_evidence(monkeypatch, manifest, source_orders=(source_order,))
    try:
        result = execute(whole_case, client)
        assert result["executed"] is False
    except RuntimeError:
        pass
    assert client.executors.created == []
    assert ledger.snapshot(manifest.position_id).active_base == D("40")
    assert g.current_recovery_exposure_quote() == D("8")


def test_synthetic_sell_cannot_reuse_an_order_bound_to_a_source_owner(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=g)
    reconciler.bind_executor_order(
        order_id="whole-sell-order",
        position_id=manifest.position_id,
        role=EconomicFillRole.POSITION_EXIT,
        proposal_id="entry-1",
        account_name="master_account",
        connector_name="kraken",
        pair="XRP-USD",
        created_at=2001,
    )
    client = EvidenceClient(manifest)
    trade = fill(manifest, "new-synthetic-trade-1", "10")
    install_evidence(monkeypatch, manifest, (trade,), status="PARTIALLY_FILLED")
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client)
    assert ledger.snapshot(manifest.position_id).active_base == D("40")
    assert g.current_recovery_exposure_quote() == D("8")
    assert ledger.get_lot("retained-43").base_remaining == D("43")


@pytest.mark.parametrize(
    "failure", [TimeoutError("lookup timeout"), LookupStatusError(500), None]
)
def test_ambiguous_sell_lookup_never_authorizes_a_create(
    whole_case, monkeypatch, failure
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    if failure is None:
        original = client.executors.get_executor

        async def lookup(executor_id):
            if executor_id == manifest.sell_executor_id:
                return None
            return await original(executor_id)

        monkeypatch.setattr(client.executors, "get_executor", lookup)
    else:
        client.executors.lookup_failure = failure
    install_evidence(monkeypatch, manifest)
    with pytest.raises((RuntimeError, TimeoutError, ValueError)):
        execute(whole_case, client)
    assert client.executors.created == []
    assert ledger.snapshot(manifest.position_id).active_base == D("40")


def test_uncertain_create_is_durable_and_never_retried_after_not_found(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.create_failure = TimeoutError("create acknowledgement lost")
    install_evidence(monkeypatch, manifest)
    with pytest.raises((RuntimeError, TimeoutError)):
        execute(whole_case, client)
    assert len(client.executors.created) == 1
    client.executors.known = False
    client.executors.create_failure = None
    restarted_ledger = EconomicPositionLedger(ledger.path)
    restarted_manifest = EconomicExitManifestStore(ledger.path).load(
        manifest.manifest_id
    )
    with pytest.raises(RuntimeError):
        execute((g, restarted_ledger, restarted_manifest), client)
    assert len(client.executors.created) == 1
    assert restarted_ledger.snapshot(manifest.position_id).active_base == D("40")


def test_concurrent_dispatch_claims_allow_only_one_durable_creator(whole_case):
    g, ledger, manifest = whole_case
    stores = [
        EconomicExitManifestStore(ledger.path),
        EconomicExitManifestStore(ledger.path),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda store: store.claim_dispatch(
                    manifest_id=manifest.manifest_id, now=2010
                ),
                stores,
            )
        )
    assert sorted(claims) == [False, True]
    assert EconomicExitManifestStore(ledger.path).dispatch_claimed(manifest.manifest_id)


def test_joint_parent_can_profit_while_entry_owner_does_not(whole_case, monkeypatch):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.known = True
    client.executors.sell_status = "TERMINATED"
    client.executors.sell_close_type = "COMPLETED"
    trade = fill(manifest, "whole-trade-1", "40")
    install_evidence(monkeypatch, manifest, (trade,), status="FILLED")
    result = synchronize(whole_case, client)
    assert result["completed"] is True
    assert D("20") * manifest.protected_limit_price * D("0.996") < D("10")
    assert ledger.snapshot(manifest.position_id).realized_pnl_quote == D("1.52160")
    assert g.current_recovery_exposure_quote() == D("0")
    assert ledger.get_lot("retained-43").base_remaining == D("43")
    assert ledger.get_lot("unattributed-20").base_remaining == D("20")


def test_partial_fill_restart_and_final_fill_never_consume_retained_inventory(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.known = True
    first = fill(manifest, "whole-trade-1", "10")
    install_evidence(monkeypatch, manifest, (first,), status="PARTIALLY_FILLED")
    pending = synchronize(whole_case, client)
    assert pending["completed"] is False
    assert D(pending["remaining_base"]) == D("30")
    assert ledger.get_lot("entry-lot").base_remaining == D("15")
    assert ledger.get_lot("recovery-lot-1").base_remaining == D("7.5")
    assert g.current_recovery_exposure_quote() == D("6")
    restarted_ledger = EconomicPositionLedger(ledger.path)
    restarted_manifest = EconomicExitManifestStore(ledger.path).load(
        manifest.manifest_id
    )
    replay = synchronize(
        whole_case, client, ledger=restarted_ledger, manifest=restarted_manifest
    )
    assert replay["completed"] is False
    assert restarted_ledger.snapshot(manifest.position_id).active_base == D("30")
    assert g.current_recovery_exposure_quote() == D("6")
    second = fill(manifest, "whole-trade-2", "30")
    client.executors.sell_status = "TERMINATED"
    client.executors.sell_close_type = "COMPLETED"
    install_evidence(monkeypatch, manifest, (first, second), status="FILLED")
    done = synchronize(
        whole_case, client, ledger=restarted_ledger, manifest=restarted_manifest
    )
    assert done["completed"] is True
    assert restarted_ledger.snapshot(manifest.position_id).active_base == D("0")
    assert g.current_recovery_exposure_quote() == D("0")
    assert restarted_ledger.get_lot("retained-43").base_remaining == D("43")
    assert restarted_ledger.get_lot("unattributed-20").base_remaining == D("20")
    saved = EconomicExitManifestStore(ledger.path).load(manifest.manifest_id)
    assert saved.status == EconomicExitManifestStatus.COMPLETED
    pnl = restarted_ledger.snapshot(manifest.position_id).realized_pnl_quote
    synchronize(whole_case, client, ledger=restarted_ledger, manifest=saved)
    assert restarted_ledger.snapshot(manifest.position_id).realized_pnl_quote == pnl
    assert client.executors.created == []


def test_known_sell_reconciles_after_price_collapse_and_breaker_halt(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.known = True
    trade = fill(manifest, "whole-trade-1", "10")
    install_evidence(monkeypatch, manifest, (trade,), status="PARTIALLY_FILLED")
    monkeypatch.setattr(exit_tool, "get_breaker_mode", lambda: BreakerMode.HALTED)
    execute(whole_case, client, decision=protected_decision(current_price=D("0.001")))
    assert ledger.snapshot(manifest.position_id).active_base == D("30")
    assert g.current_recovery_exposure_quote() == D("6")
    assert client.executors.created == []


@pytest.mark.parametrize(
    "order_type,price", [("MARKET", D("0.49")), ("LIMIT", D("0.48"))]
)
def test_unprotected_observed_order_is_durably_held_without_fake_disposal(
    whole_case, monkeypatch, order_type, price
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    bad_order = evidence_order(manifest, order_type=order_type, price=price)
    install_evidence(monkeypatch, manifest, orders=(bad_order,))
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client)
    assert ledger.snapshot(manifest.position_id).active_base == D("40")
    assert EconomicExitManifestStore(ledger.path).protection_conflicts(
        manifest.manifest_id
    )


@pytest.mark.parametrize("bad_price,excess_fee", [(True, False), (False, True)])
def test_real_breach_is_booked_once_and_never_claimed_complete(
    whole_case, monkeypatch, bad_price, excess_fee
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.known = True
    price = D("0.30") if bad_price else manifest.protected_limit_price
    fee = D("2") if excess_fee else None
    trade = fill(manifest, "whole-trade-1", "40", price=price, fee=fee)
    install_evidence(monkeypatch, manifest, (trade,), status="FILLED")
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client)
    assert ledger.snapshot(manifest.position_id).active_base == D("0")
    realized = D("40") * price - trade.fee_paid - D("18")
    assert ledger.snapshot(manifest.position_id).realized_pnl_quote == realized
    assert (
        EconomicExitManifestStore(ledger.path).load(manifest.manifest_id).status
        == EconomicExitManifestStatus.OPEN
    )
    assert g.current_recovery_exposure_quote() == D("0")
    restarted_ledger = EconomicPositionLedger(ledger.path)
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client, ledger=restarted_ledger)
    assert (
        restarted_ledger.snapshot(manifest.position_id).realized_pnl_quote == realized
    )
    assert restarted_ledger.get_lot("retained-43").base_remaining == D("43")
    assert restarted_ledger.get_lot("unattributed-20").base_remaining == D("20")


def test_multiple_live_orders_cannot_reserve_more_than_immutable_active_base(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    rows = (
        evidence_order(manifest, order_id="sell-a", amount=D("25")),
        evidence_order(manifest, order_id="sell-b", amount=D("25")),
    )
    install_evidence(monkeypatch, manifest, orders=rows)
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client)
    assert EconomicExitManifestStore(ledger.path).protection_conflicts(
        manifest.manifest_id
    )
    assert ledger.snapshot(manifest.position_id).active_base == D("40")


def test_outstanding_orders_are_rechecked_after_truthful_partial_fill(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    trade = fill(manifest, "whole-trade-1", "20")
    rows = (
        evidence_order(
            manifest,
            status="PARTIALLY_FILLED",
            filled_amount=D("20"),
        ),
        evidence_order(manifest, order_id="additional-seller", amount=D("20")),
    )
    install_evidence(monkeypatch, manifest, (trade,), orders=rows)
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client)
    assert ledger.snapshot(manifest.position_id).active_base == D("20")
    assert ledger.snapshot(manifest.position_id).realized_pnl_quote == D("0.76080")
    assert g.current_recovery_exposure_quote() == D("4")
    assert EconomicExitManifestStore(ledger.path).protection_conflicts(
        manifest.manifest_id
    )
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client, ledger=EconomicPositionLedger(ledger.path))
    assert ledger.snapshot(manifest.position_id).active_base == D("20")
    assert ledger.snapshot(manifest.position_id).realized_pnl_quote == D("0.76080")


def test_fractional_mixed_owner_dispositions_release_actual_basis_once(
    tmp_path, monkeypatch
):
    g = governor(tmp_path)
    ledger = book(tmp_path)
    for proposal_id, intent, base, basis, occurred_at in (
        ("mixed-entry", IntentKind.ENTRY, "3", "3", 1010),
        ("mixed-recovery-a", IntentKind.RECOVERY, "7", "4.1", 1020),
        ("mixed-recovery-b", IntentKind.RECOVERY, "11", "5.9", 1030),
    ):
        prepare_execution(
            g,
            proposal_id=proposal_id,
            executor_id=proposal_id + "-executor",
            intent=intent,
            amount=basis,
        )
        record = (
            ledger.record_entry_fill
            if intent == IntentKind.ENTRY
            else ledger.record_vcap_fill
        )
        record(
            governor=g,
            position_id="xrp-position-1",
            lot_id=proposal_id + "-lot",
            proposal_id=proposal_id,
            base_amount=D(base),
            cost_basis_quote=D(basis),
            occurred_at=occurred_at,
        )
    ledger.record_strategic_retained(
        lot_id="retained-mixed",
        pair="XRP-USD",
        base_amount=D("43"),
        cost_basis_quote=D("17"),
        occurred_at=1040,
    )
    manifest = create_manifest(g, ledger, protected_floor_price=D("0.70"))
    case = (g, ledger, manifest)
    client = EvidenceClient(manifest)
    trades = []
    for number, amount in enumerate(("0.1", "0.2", "3.7", "17"), 1):
        trades.append(fill(manifest, f"fractional-trade-{number}", amount))
        final = number == 4
        if final:
            client.executors.sell_status = "TERMINATED"
            client.executors.sell_close_type = "COMPLETED"
        install_evidence(
            monkeypatch,
            manifest,
            tuple(trades),
            status="FILLED" if final else "PARTIALLY_FILLED",
        )
        result = synchronize(case, client, ledger=EconomicPositionLedger(ledger.path))
        assert result["completed"] is final
        if final:
            assert g.current_recovery_exposure_quote() == D("0")
        else:
            expected = sum(
                (
                    Fraction(ledger.get_lot(lot_id).cost_basis_remaining_quote)
                    for lot_id in (
                        "mixed-recovery-a-lot",
                        "mixed-recovery-b-lot",
                    )
                ),
                Fraction(0),
            )
            assert Fraction(g.current_recovery_exposure_quote()) == expected
        pnl = ledger.snapshot(manifest.position_id).realized_pnl_quote
        synchronize(case, client, ledger=EconomicPositionLedger(ledger.path))
        assert ledger.snapshot(manifest.position_id).realized_pnl_quote == pnl
    with sqlite3.connect(g.db_path) as conn:
        rows = conn.execute(
            "SELECT proposal_id, amount_quote FROM recovery_recycle_releases"
        ).fetchall()
    assert sum(
        (
            Fraction(D(amount))
            for proposal_id, amount in rows
            if proposal_id == "mixed-recovery-a"
        ),
        Fraction(0),
    ) == Fraction(D("4.1"))
    assert sum(
        (
            Fraction(D(amount))
            for proposal_id, amount in rows
            if proposal_id == "mixed-recovery-b"
        ),
        Fraction(0),
    ) == Fraction(D("5.9"))
    assert ledger.get_lot("retained-mixed").base_remaining == D("43")
    assert client.executors.created == []


def test_tiny_final_lot_cannot_invent_dust_or_break_partial_exit_conservation(
    tmp_path,
):
    ledger = book(tmp_path)
    amounts = (D("1"), D("1"), D("1"), D("1e-50"))
    lot_ids = tuple(f"tiny-tail-lot-{index}" for index in range(4))
    for index, (lot_id, amount) in enumerate(zip(lot_ids, amounts, strict=True)):
        ledger.record_base_fill(
            position_id="xrp-position-1",
            lot_id=lot_id,
            pair="XRP-USD",
            base_amount=amount,
            cost_basis_quote=amount,
            occurred_at=1010 + index,
        )
    initial = sum((Fraction(amount) for amount in amounts), Fraction(0))
    assert Fraction(ledger.snapshot("xrp-position-1").active_base) == initial
    first = ledger.record_partial_exit(
        event_id="tiny-tail-first-sale",
        position_id="xrp-position-1",
        base_sold=D("1"),
        gross_proceeds_quote=D("1"),
        occurred_at=2000,
        eligible_lot_ids=lot_ids,
    )
    remaining = ledger.snapshot("xrp-position-1").active_base
    assert Fraction(remaining) == initial - Fraction(1)
    assert Fraction(first.base_sold) == Fraction(1)
    assert sum(
        (Fraction(ledger.get_lot(lot_id).base_remaining) for lot_id in lot_ids),
        Fraction(0),
    ) == initial - Fraction(1)
    assert sum(
        (
            Fraction(ledger.get_lot(lot_id).cost_basis_remaining_quote)
            for lot_id in lot_ids
        ),
        Fraction(0),
    ) == initial - Fraction(first.allocated_cost_basis_quote)
    for lot_id in lot_ids:
        lot = ledger.get_lot(lot_id)
        assert lot.base_remaining >= D("0")
        assert lot.cost_basis_remaining_quote >= D("0")
        assert lot.entry_fees_remaining_quote >= D("0")
    live_lot_ids = tuple(
        lot_id for lot_id in lot_ids if ledger.get_lot(lot_id).base_remaining > D("0")
    )
    final = ledger.record_partial_exit(
        event_id="tiny-tail-final-sale",
        position_id="xrp-position-1",
        base_sold=remaining,
        gross_proceeds_quote=remaining,
        occurred_at=2010,
        eligible_lot_ids=live_lot_ids,
    )
    assert Fraction(first.base_sold) + Fraction(final.base_sold) == initial
    assert (
        Fraction(first.allocated_cost_basis_quote)
        + Fraction(final.allocated_cost_basis_quote)
    ) == initial
    assert ledger.snapshot("xrp-position-1").active_base == D("0")
    assert ledger.snapshot("xrp-position-1").active_cost_basis_quote == D("0")
    for lot_id in lot_ids:
        lot = ledger.get_lot(lot_id)
        assert lot.base_remaining == D("0")
        assert lot.cost_basis_remaining_quote == D("0")
        assert lot.entry_fees_remaining_quote == D("0")


@pytest.mark.parametrize("change", ["floor", "target", "sell_executor", "owner", "lot"])
def test_saved_manifest_rejects_target_floor_and_inventory_tampering(
    whole_case, change
):
    g, ledger, manifest = whole_case
    if change == "floor":
        tampered = replace(manifest, protected_limit_price=D("0.10"))
    elif change == "target":
        tampered = replace(manifest, target_net_profit_bps=D("0"))
    elif change == "sell_executor":
        tampered = replace(manifest, sell_executor_id="unowned-seller")
    elif change == "owner":
        tampered = replace(
            manifest,
            owners=(
                replace(manifest.owners[0], proposal_id="foreign"),
                *manifest.owners[1:],
            ),
        )
    else:
        tampered = replace(
            manifest,
            owners=(
                replace(manifest.owners[0], lot_ids=("retained-43",)),
                *manifest.owners[1:],
            ),
        )
    with pytest.raises(RuntimeError):
        validate_manifest_against_ledger(tampered, ledger=ledger)
    assert ledger.snapshot(manifest.position_id).active_base == D("40")


def test_legacy_schema_migrates_without_inventing_protection_or_sending_orders(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    with sqlite3.connect(ledger.path) as conn:
        for field in (
            "target_net_profit_bps",
            "expected_exit_cost_bps",
            "protected_limit_price",
            "sell_executor_id",
        ):
            conn.execute(f"ALTER TABLE economic_exit_manifests DROP COLUMN {field}")
    store = EconomicExitManifestStore(ledger.path)
    legacy = store.load(manifest.manifest_id)
    assert legacy.target_net_profit_bps is None
    assert legacy.expected_exit_cost_bps is None
    assert legacy.protected_limit_price is None
    assert legacy.sell_executor_id is None
    client = EvidenceClient(legacy)
    install_evidence(monkeypatch, legacy, orders=())
    result = execute((g, ledger, legacy), client)
    assert result["exit_requested"] is False
    assert client.executors.created == []
    assert client.executors.stopped == []
    assert ledger.snapshot(manifest.position_id).active_base == D("40")


def partial_manifest(g, ledger):
    decision = plan_partial_recycle(
        ledger=ledger,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=trading_rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("40"),
        slippage_allowance_bps=D("5"),
        now=2000,
        policy=PartialRecyclePolicy(target_net_profit_bps=D("174")),
    )
    assert decision.manifest is not None
    return decision.manifest


def test_open_whole_exit_blocks_new_partial_recycling(whole_case):
    g, ledger, manifest = whole_case
    decision = plan_partial_recycle(
        ledger=ledger,
        governor=g,
        position_id=manifest.position_id,
        trading_rule=trading_rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("40"),
        slippage_allowance_bps=D("5"),
        now=2000,
    )
    assert decision.manifest is None
    assert "open_whole_exit_blocks_partial_manifest_creation" in decision.reasons
    assert ledger.snapshot(manifest.position_id).active_base == D("40")
    assert (
        PartialRecycleStore(ledger.path).open_for_position(manifest.position_id) is None
    )


def test_open_partial_recycle_blocks_new_whole_exit(tmp_path):
    g, ledger = build_owned_position(tmp_path)
    partial = partial_manifest(g, ledger)
    with pytest.raises((RuntimeError, ValueError), match="partial"):
        create_manifest(g, ledger)
    assert PartialRecycleStore(ledger.path).load(partial.manifest_id) == partial
    assert (
        EconomicExitManifestStore(ledger.path).open_for_position(partial.position_id)
        is None
    )
    assert ledger.snapshot(partial.position_id).active_base == D("40")


def test_atomic_whole_and_partial_manifest_creations_cannot_both_win(tmp_path):
    g, ledger = build_owned_position(tmp_path)
    whole = create_manifest(g, ledger)
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(
            "DELETE FROM economic_exit_manifests WHERE manifest_id = ?",
            (whole.manifest_id,),
        )
    partial = partial_manifest(g, ledger)
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(
            "DELETE FROM partial_recycle_manifests WHERE manifest_id = ?",
            (partial.manifest_id,),
        )
    whole_store = EconomicExitManifestStore(ledger.path)
    partial_store = PartialRecycleStore(ledger.path)

    def create(item):
        store, manifest = item
        try:
            store.create(manifest)
            return "CREATED"
        except (RuntimeError, ValueError):
            return "BLOCKED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(create, [(whole_store, whole), (partial_store, partial)])
        )
    assert sorted(outcomes) == ["BLOCKED", "CREATED"]
    assert (
        sum(
            store.open_for_position("xrp-position-1") is not None
            for store in (whole_store, partial_store)
        )
        == 1
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("pair", "XRP-EUR"),
        ("account_name", "other"),
        ("connector_name", "binance"),
        ("order_id", "unknown-order"),
    ],
)
def test_cross_scope_or_unknown_order_fills_cannot_dispose_active_inventory(
    whole_case, monkeypatch, field, value
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    trade = fill(manifest, "whole-trade-1", "10", **{field: value})
    install_evidence(monkeypatch, manifest, (trade,), status="PARTIALLY_FILLED")
    with pytest.raises((RuntimeError, ValueError)):
        synchronize(whole_case, client)
    assert ledger.snapshot(manifest.position_id).active_base == D("40")
    assert ledger.get_lot("retained-43").base_remaining == D("43")
    assert ledger.get_lot("unattributed-20").base_remaining == D("20")


def test_actual_oversell_never_uses_retained_inventory_to_cover_deficit(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    trade = fill(manifest, "whole-trade-1", "41")
    install_evidence(monkeypatch, manifest, (trade,), status="FILLED")
    with pytest.raises((RuntimeError, ValueError)):
        synchronize(whole_case, client)
    assert ledger.snapshot(manifest.position_id).active_base == D("40")
    assert ledger.get_lot("retained-43").base_remaining == D("43")


def test_canceled_known_sell_does_not_dispatch_market_fallback_or_release_inventory(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.known = True
    install_evidence(monkeypatch, manifest, status="CANCELED")
    result = execute(whole_case, client)
    assert result["economic_exit_state"] != EconomicExitProgressState.COMPLETED.value
    assert client.executors.created == []
    assert ledger.snapshot(manifest.position_id).active_base == D("40")
    assert g.current_recovery_exposure_quote() == D("8")


def test_claim_is_committed_before_any_external_sell_submission(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    install_evidence(monkeypatch, manifest)
    original = client.executors.create_executor

    async def create(**kwargs):
        assert EconomicExitManifestStore(ledger.path).dispatch_claimed(
            manifest.manifest_id
        )
        return await original(**kwargs)

    monkeypatch.setattr(client.executors, "create_executor", create)
    execute(whole_case, client)
    assert len(client.executors.created) == 1


def test_filled_order_ack_without_trade_evidence_never_disposes_inventory(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.known = True
    order = evidence_order(manifest, status="FILLED", filled_amount=D("40"))
    install_evidence(monkeypatch, manifest, orders=(order,))
    result = synchronize(whole_case, client)
    assert result["completed"] is False
    assert ledger.snapshot(manifest.position_id).active_base == D("40")
    assert g.current_recovery_exposure_quote() == D("8")


def test_fee_breach_cannot_be_hidden_by_missing_journal_after_crash(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    trade = fill(manifest, "whole-trade-1", "40", fee=D("2"))
    install_evidence(monkeypatch, manifest, (trade,), status="FILLED")
    original = EconomicExitManifestStore.record_fill

    def interrupted_record(self, **kwargs):
        raise RuntimeError("crash before durable fill journal")

    monkeypatch.setattr(EconomicExitManifestStore, "record_fill", interrupted_record)
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client)
    assert ledger.snapshot(manifest.position_id).active_base == D("0")
    store = EconomicExitManifestStore(ledger.path)
    assert not store.fill_recorded(trade.trade_id)
    with pytest.raises(RuntimeError):
        assess_position_exit_manifest(manifest, ledger=ledger, store=store, now=2021)
    assert store.load(manifest.manifest_id).status == EconomicExitManifestStatus.OPEN
    monkeypatch.setattr(EconomicExitManifestStore, "record_fill", original)
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client, ledger=EconomicPositionLedger(ledger.path))
    assert store.fill_recorded(trade.trade_id)
    assert store.protection_conflicts(manifest.manifest_id)
    assert ledger.snapshot(manifest.position_id).realized_pnl_quote == D("-0.40")
    assert store.load(manifest.manifest_id).status == EconomicExitManifestStatus.OPEN


def test_profitable_fill_journal_can_recover_after_ledger_commit_crash(
    whole_case, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.known = True
    client.executors.sell_status = "TERMINATED"
    client.executors.sell_close_type = "COMPLETED"
    trade = fill(manifest, "whole-trade-1", "40")
    install_evidence(monkeypatch, manifest, (trade,), status="FILLED")
    original = EconomicExitManifestStore.record_fill

    def interrupted_record(self, **kwargs):
        raise RuntimeError("crash before durable fill journal")

    monkeypatch.setattr(EconomicExitManifestStore, "record_fill", interrupted_record)
    with pytest.raises(RuntimeError):
        synchronize(whole_case, client)
    assert ledger.snapshot(manifest.position_id).active_base == D("0")
    store = EconomicExitManifestStore(ledger.path)
    with pytest.raises(RuntimeError, match="coverage"):
        assess_position_exit_manifest(manifest, ledger=ledger, store=store, now=2021)
    monkeypatch.setattr(EconomicExitManifestStore, "record_fill", original)
    restored = EconomicPositionLedger(ledger.path)
    completed = synchronize(whole_case, client, ledger=restored)
    assert completed["completed"] is True
    assert store.fill_recorded(trade.trade_id)
    assert restored.snapshot(manifest.position_id).realized_pnl_quote == D("1.52160")
    assert g.current_recovery_exposure_quote() == D("0")


def test_unjournaled_disposal_cannot_manufacture_protected_completion(whole_case):
    g, ledger, manifest = whole_case
    ledger.record_partial_exit(
        event_id="unrelated-unprotected-disposal",
        position_id=manifest.position_id,
        base_sold=D("40"),
        gross_proceeds_quote=D("20"),
        occurred_at=2010,
        eligible_lot_ids=manifest.lot_ids,
    )
    store = EconomicExitManifestStore(ledger.path)
    with pytest.raises(RuntimeError, match="coverage"):
        assess_position_exit_manifest(manifest, ledger=ledger, store=store, now=2020)
    assert store.load(manifest.manifest_id).status == EconomicExitManifestStatus.OPEN


@pytest.mark.parametrize(
    "seller_status,order_status", [("RUNNING", "FILLED"), ("TERMINATED", "OPEN")]
)
def test_exact_full_fill_stays_pending_until_seller_and_all_orders_are_final(
    whole_case, monkeypatch, seller_status, order_status
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.known = True
    client.executors.sell_status = seller_status
    trade = fill(manifest, "whole-trade-1", "40")
    install_evidence(monkeypatch, manifest, (trade,), status=order_status)
    pending = synchronize(whole_case, client)
    assert pending["completed"] is False
    assert ledger.snapshot(manifest.position_id).active_base == D("0")
    assert (
        EconomicExitManifestStore(ledger.path).load(manifest.manifest_id).status
        == EconomicExitManifestStatus.OPEN
    )
    client.executors.sell_status = "TERMINATED"
    client.executors.sell_close_type = "COMPLETED"
    install_evidence(monkeypatch, manifest, (trade,), status="FILLED")
    completed = synchronize(
        whole_case, client, ledger=EconomicPositionLedger(ledger.path)
    )
    assert completed["completed"] is True
    assert ledger.snapshot(manifest.position_id).realized_pnl_quote == D("1.52160")
    assert client.executors.created == []


def test_restart_reconciles_sell_but_requires_exact_retirement_before_completion(
    whole_case, tmp_path, monkeypatch
):
    g, ledger, manifest = whole_case
    client = EvidenceClient(manifest)
    client.executors.known = True
    lifecycle = VCAPStateStore(tmp_path / "whole-runtime-state.sqlite3")
    lifecycle.save(
        VCAPLifecycleState(
            envelope_proposal_id="whole-envelope",
            pair="XRP-USD",
            next_sequence=1,
            remaining_authorized_quote=D("100"),
            recovery_proposal_ids=("recovery-1", "recovery-2"),
            position_base=D("40"),
            cost_basis_quote=D("18"),
            last_known_price=D("0.50"),
            profit_state=None,
            status=VCAPLifecycleStatus.ACTIVE,
            updated_at=2000,
        )
    )
    runtime = VCAPRuntimeStore(tmp_path / "whole-runtime.sqlite3")
    cycle = runtime.begin_or_resume(
        position_id=manifest.position_id,
        envelope_proposal_id="whole-envelope",
        now=2000,
    )
    cycle = runtime.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.MUTATION_DISPATCHING,
        now=2001,
        planned_action=VCAPLifecycleAction.EXIT.value,
        mutation_key="EXIT:whole-envelope",
        expected_phases=(VCAPRuntimePhase.STARTED,),
    )
    cycle = runtime.transition(
        cycle.cycle_id,
        phase=VCAPRuntimePhase.AWAITING_RECONCILIATION,
        now=2002,
        execution_result={"manifest_id": manifest.manifest_id},
        expected_phases=(VCAPRuntimePhase.MUTATION_DISPATCHING,),
    )

    async def accounting(*args, **kwargs):
        return replace(healthy(now=kwargs["now"]), position_id=manifest.position_id)

    monkeypatch.setattr(runtime_tool, "_refresh_accounting", accounting)
    first = fill(manifest, "whole-trade-1", "10")
    install_evidence(monkeypatch, manifest, (first,), status="PARTIALLY_FILLED")

    def resume(active_cycle, active_ledger, active_runtime, active_lifecycle, now):
        return asyncio.run(
            runtime_tool._resume_reconciliation(
                client,
                cycle=active_cycle,
                governor=g,
                ledger=active_ledger,
                lifecycle_store=active_lifecycle,
                runtime_store=active_runtime,
                accounting_supervisor=AccountingHealthSupervisor(active_ledger.path),
                now=now,
            )
        )

    pending = resume(cycle, ledger, runtime, lifecycle, 2020)
    assert pending["phase"] == VCAPRuntimePhase.AWAITING_RECONCILIATION.value
    assert ledger.snapshot(manifest.position_id).active_base == D("30")
    restarted_runtime = VCAPRuntimeStore(runtime.path)
    restarted_cycle = restarted_runtime.load(cycle.cycle_id)
    restarted_ledger = EconomicPositionLedger(ledger.path)
    restarted_lifecycle = VCAPStateStore(lifecycle.path)
    second = fill(manifest, "whole-trade-2", "30")
    client.executors.sell_status = "TERMINATED"
    client.executors.sell_close_type = "COMPLETED"
    install_evidence(monkeypatch, manifest, (first, second), status="FILLED")
    completed = resume(
        restarted_cycle, restarted_ledger, restarted_runtime, restarted_lifecycle, 2030
    )
    assert completed["phase"] == VCAPRuntimePhase.AWAITING_RECONCILIATION.value
    assert restarted_ledger.get_retirement_certificate(manifest.position_id) is None
    assert (
        restarted_lifecycle.load("whole-envelope").status == VCAPLifecycleStatus.ACTIVE
    )
    assert restarted_ledger.snapshot(manifest.position_id).active_base == D("0")
    assert restarted_ledger.get_lot("retained-43").base_remaining == D("43")
    assert restarted_ledger.get_lot("unattributed-20").base_remaining == D("20")
    assert client.executors.created == []
