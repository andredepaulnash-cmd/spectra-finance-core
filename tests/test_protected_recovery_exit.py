"""Adversarial execution tests for profit-protected recovery child exits.

These exercise the real manifest store, economic ledger and Governor. The
exchange stub controls only observable executor, order and trade evidence.
"""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

import mcp_servers.hummingbot_api.tools.partial_recycle as partial_tool
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import (
    ExecutorOrderEvidence,
    ExecutorOrderSnapshot,
    ExecutorTradeSnapshot,
)
from spectra_core.fill_reconciliation import TradeFillEvidence
from spectra_core.models import BreakerMode, IntentKind
from spectra_core.partial_recycle import (
    PartialRecycleManifestStatus,
    PartialRecyclePolicy,
    PartialRecycleStore,
    assess_partial_recycle_manifest,
    plan_partial_recycle,
    protected_partial_recycle_price,
    validate_partial_recycle_manifest,
)
from tests.test_partial_recovery_recycling import (
    add_recovery,
    governor,
    ledger,
    prepare_execution,
    rule,
)

D = Decimal


def protected_price(**overrides):
    values = dict(
        target_base=D("10"),
        target_cost_basis_quote=D("4"),
        target_entry_fees_quote=D("0.04"),
        target_net_profit_bps=D("174"),
        expected_exit_cost_bps=D("45"),
        trading_rule=rule(),
    )
    values.update(overrides)
    return protected_partial_recycle_price(**values)


class LookupStatusError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"Executor lookup HTTP {status}")
        self.response = SimpleNamespace(status_code=status)
        self.status = status


def status_error(status):
    return LookupStatusError(status)


@pytest.fixture
def protected_case(tmp_path, monkeypatch):
    g = governor(tmp_path)
    book = ledger(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry",
        executor_id="entry-exec",
        intent=IntentKind.ENTRY,
        amount="10",
    )
    book.record_entry_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="entry-lot",
        proposal_id="entry",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        occurred_at=1010,
    )
    add_recovery(
        g,
        book,
        proposal_id="recovery-profit",
        executor_id="recovery-profit-exec",
        lot_id="recovery-profit-lot",
        base="10",
        basis="4",
        occurred_at=1020,
    )
    decision = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.50"),
        expected_exit_fee_bps=D("40"),
        slippage_allowance_bps=D("5"),
        now=2000,
        policy=PartialRecyclePolicy(target_net_profit_bps=D("174")),
    )
    assert decision.manifest is not None
    monkeypatch.setattr(partial_tool, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    return g, book, decision


class EvidenceExecutors:
    def __init__(self, manifest):
        self.manifest = manifest
        self.created = []
        self.known = False
        self.lookup_failure = None
        self.create_failure = None

    async def get_executor(self, executor_id):
        if executor_id == "recovery-profit-exec":
            return dict(
                id=executor_id,
                executor_type="dca_executor",
                status="TERMINATED",
                close_type="POSITION_HOLD",
                filled_amount_quote="4",
                cum_fees_quote="0",
                position_size_quote="4",
                net_pnl_quote="0",
                custom_info={"realized_pnl_quote": "0"},
            )
        assert executor_id == self.manifest.sell_executor_id
        if self.lookup_failure is not None:
            if isinstance(self.lookup_failure, Exception):
                raise self.lookup_failure
            return self.lookup_failure
        if not self.known:
            raise status_error(404)
        return dict(
            id=executor_id,
            executor_type="order_executor",
            status="RUNNING",
            close_type=None,
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
                amount=str(self.manifest.target_base),
                price=str(self.manifest.protected_limit_price),
                execution_strategy="LIMIT",
            ),
        )

    async def create_executor(self, *, executor_config, account_name, controller_id):
        self.created.append((executor_config, account_name, controller_id))
        self.known = True
        if self.create_failure is not None:
            raise self.create_failure
        return {"executor_id": executor_config["id"], "status": "RUNNING"}


class EvidenceClient:
    def __init__(self, manifest):
        self.executors = EvidenceExecutors(manifest)


def install_evidence(
    monkeypatch,
    manifest,
    fills=(),
    *,
    order_type="LIMIT",
    price=None,
    status="OPEN",
    amount=None,
    no_orders=False,
):
    order_price = manifest.protected_limit_price if price is None else price
    order_amount = manifest.target_base if amount is None else amount
    total = sum((fill.amount_base for fill in fills), D("0"))
    order = ExecutorOrderEvidence(
        executor_id=manifest.sell_executor_id,
        order_id="protected-sell-order",
        order_type=order_type,
        side="SELL",
        amount=order_amount,
        price=order_price,
        status=status,
        filled_amount=total,
        average_fill_price=None if not fills else fills[0].price,
        created_at=2001,
        updated_at=2002,
    )

    async def orders(client, *, executor_id):
        assert executor_id == manifest.sell_executor_id
        return ExecutorOrderSnapshot(
            executor_id=executor_id, orders=() if no_orders else (order,)
        )

    async def trades(client, *, executor_id):
        assert executor_id == manifest.sell_executor_id
        return ExecutorTradeSnapshot(executor_id=executor_id, trades=tuple(fills))

    monkeypatch.setattr(partial_tool, "fetch_executor_order_snapshot", orders)
    monkeypatch.setattr(partial_tool, "fetch_executor_trade_snapshot", trades)


def fill(manifest, trade_id, amount, *, price=None, fee=None):
    sale_price = manifest.protected_limit_price if price is None else price
    base = D(amount)
    return TradeFillEvidence(
        trade_id=trade_id,
        order_id="protected-sell-order",
        account_name="master_account",
        connector_name="kraken",
        pair="XRP-USD",
        side="SELL",
        amount_base=base,
        price=sale_price,
        fee_paid=base * sale_price * D("0.004") if fee is None else fee,
        fee_currency="USD",
        occurred_at=2002 if trade_id.endswith("1") else 2003,
    )


def execute(case, client, *, current_price=D("0.50"), trading_rule=None):
    g, book, decision = case
    return asyncio.run(
        partial_tool.execute_partial_recycle(
            client,
            decision=decision,
            governor=g,
            ledger=book,
            trading_rule=rule() if trading_rule is None else trading_rule,
            current_price=current_price,
            now=2010,
        )
    )


def synchronize(case, client, *, book=None, manifest=None):
    g, original_book, decision = case
    active_book = original_book if book is None else book
    return asyncio.run(
        partial_tool.synchronize_partial_recycle_executor(
            client,
            manifest=decision.manifest if manifest is None else manifest,
            governor=g,
            ledger=active_book,
            store=PartialRecycleStore(active_book.path),
            now=2020,
        )
    )


def test_protected_price_includes_entry_fees_exit_cost_and_rounds_up():
    price = protected_price()
    assert price == D("0.4129")
    invested = D("4.04")
    net = D("10") * price * D("0.9955")
    required = invested * D("1.0174")
    assert net >= required
    assert D("10") * (price - D("0.0001")) * D("0.9955") < required


def test_exchange_quote_minimum_can_raise_the_protected_price():
    price = protected_price(trading_rule=rule(min_quote="5"))
    assert price == D("0.5")
    assert price * D("10") >= D("5")


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_base", D("0")),
        ("target_base", D("NaN")),
        ("target_base", D("10.01")),
        ("target_cost_basis_quote", D("-1")),
        ("target_entry_fees_quote", D("-1")),
        ("target_net_profit_bps", D("-1")),
        ("target_net_profit_bps", D("Infinity")),
        ("expected_exit_cost_bps", D("-1")),
        ("expected_exit_cost_bps", D("10000")),
        ("expected_exit_cost_bps", D("10001")),
    ],
)
def test_invalid_or_unrepresentable_profit_geometry_is_rejected(field, value):
    with pytest.raises(ValueError):
        protected_price(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_price_increment", D("0")),
        ("min_price_increment", D("NaN")),
        ("min_base_amount_increment", D("0")),
        ("supports_limit_orders", False),
    ],
)
def test_missing_limit_capability_or_valid_steps_is_rejected(field, value):
    with pytest.raises(ValueError):
        protected_price(trading_rule=replace(rule(), **{field: value}))


def test_target_and_limit_are_durable_and_not_recomputed_from_a_new_policy(
    protected_case,
):
    g, book, decision = protected_case
    original = decision.manifest
    reloaded = PartialRecycleStore(book.path).load(original.manifest_id)
    assert reloaded.target_net_profit_bps == D("174")
    assert reloaded.protected_limit_price == original.protected_limit_price
    resumed = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("0.01"),
        expected_exit_fee_bps=D("0"),
        slippage_allowance_bps=D("0"),
        now=2011,
        policy=PartialRecyclePolicy(target_net_profit_bps=D("100")),
    )
    assert resumed.manifest == reloaded


def test_new_dispatch_is_exact_limit_and_acknowledgement_does_not_release_capital(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    client = EvidenceClient(manifest)
    install_evidence(monkeypatch, manifest)
    result = execute(protected_case, client)
    assert result["executed"] is True
    config, account, controller = client.executors.created[0]
    assert config["execution_strategy"] == "LIMIT"
    assert D(config["price"]) == manifest.protected_limit_price
    assert D(config["amount"]) == D("10")
    assert account == "master_account" and controller == "spectra"
    assert g.current_recovery_exposure_quote() == D("4")
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")
    assert book.get_lot("entry-lot").base_remaining == D("20")
    assert (
        PartialRecycleStore(book.path).open_for_position("xrp-position-1") is not None
    )
    execute(protected_case, client)
    assert len(client.executors.created) == 1


@pytest.mark.parametrize("failure", [TimeoutError("lookup timeout"), status_error(500)])
def test_ambiguous_executor_lookup_never_dispatches_or_changes_inventory(
    protected_case,
    monkeypatch,
    failure,
):
    g, book, decision = protected_case
    client = EvidenceClient(decision.manifest)
    client.executors.lookup_failure = failure
    install_evidence(monkeypatch, decision.manifest)
    try:
        result = execute(protected_case, client)
        assert result["executed"] is False
    except (RuntimeError, TimeoutError):
        pass
    assert client.executors.created == []
    assert g.current_recovery_exposure_quote() == D("4")
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")


def test_uncertain_creation_is_not_retried_even_if_next_lookup_says_not_found(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    client = EvidenceClient(decision.manifest)
    client.executors.create_failure = TimeoutError("submission acknowledgement lost")
    install_evidence(monkeypatch, decision.manifest)
    with pytest.raises((RuntimeError, TimeoutError)):
        execute(protected_case, client)
    assert len(client.executors.created) == 1
    client.executors.known = False
    client.executors.create_failure = None
    try:
        result = execute(protected_case, client)
        assert result["executed"] is False
    except RuntimeError:
        pass
    assert len(client.executors.created) == 1
    assert g.current_recovery_exposure_quote() == D("4")
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")


def test_none_executor_lookup_is_ambiguous_and_never_permission_to_dispatch(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    client = EvidenceClient(decision.manifest)
    original_lookup = client.executors.get_executor

    async def lookup(executor_id):
        if executor_id == decision.manifest.sell_executor_id:
            return None
        return await original_lookup(executor_id)

    monkeypatch.setattr(client.executors, "get_executor", lookup)
    install_evidence(monkeypatch, decision.manifest)
    with pytest.raises((RuntimeError, ValueError)):
        execute(protected_case, client)
    assert client.executors.created == []
    assert g.current_recovery_exposure_quote() == D("4")
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")


def test_partial_fill_restart_and_replay_preserve_exact_ownership_until_final_fill(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    client = EvidenceClient(manifest)
    client.executors.known = True
    first = fill(manifest, "protected-trade-1", "4")
    install_evidence(monkeypatch, manifest, (first,), status="PARTIALLY_FILLED")
    result = synchronize(protected_case, client)
    assert result["completed"] is False
    assert D(result["remaining_base"]) == D("6")
    assert book.get_lot("entry-lot").base_remaining == D("20")
    assert book.get_lot("recovery-profit-lot").base_remaining == D("6")
    assert g.current_recovery_exposure_quote() == D("2.4")
    restarted_book = EconomicPositionLedger(book.path)
    restarted_manifest = PartialRecycleStore(book.path).load(manifest.manifest_id)
    replay = synchronize(
        protected_case, client, book=restarted_book, manifest=restarted_manifest
    )
    assert replay["completed"] is False
    assert g.current_recovery_exposure_quote() == D("2.4")
    assert restarted_book.get_lot("recovery-profit-lot").base_remaining == D("6")
    second = fill(manifest, "protected-trade-2", "6")
    install_evidence(monkeypatch, manifest, (first, second), status="FILLED")
    completed = synchronize(
        protected_case, client, book=restarted_book, manifest=restarted_manifest
    )
    assert completed["completed"] is True
    assert restarted_book.get_lot("recovery-profit-lot").base_remaining == D("0")
    assert restarted_book.get_lot("entry-lot").base_remaining == D("20")
    assert g.current_recovery_exposure_quote() == D("0")
    stored = PartialRecycleStore(book.path).load(manifest.manifest_id)
    assert stored.status == PartialRecycleManifestStatus.COMPLETED
    realized = PartialRecycleStore(book.path).realized_pnl(manifest.manifest_id)
    synchronize(protected_case, client, book=restarted_book, manifest=stored)
    assert PartialRecycleStore(book.path).realized_pnl(manifest.manifest_id) == realized
    assert client.executors.created == []


def test_known_executor_reconciles_even_when_current_price_falls_below_minimum(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    client = EvidenceClient(manifest)
    client.executors.known = True
    trade = fill(manifest, "protected-trade-1", "4")
    install_evidence(monkeypatch, manifest, (trade,), status="PARTIALLY_FILLED")
    execute(protected_case, client, current_price=D("0.001"))
    assert book.get_lot("recovery-profit-lot").base_remaining == D("6")
    assert g.current_recovery_exposure_quote() == D("2.4")
    assert client.executors.created == []


@pytest.mark.parametrize("order_type,below_limit", [("MARKET", False), ("LIMIT", True)])
def test_unprotected_order_evidence_is_rejected_without_consuming_inventory(
    protected_case,
    monkeypatch,
    order_type,
    below_limit,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    client = EvidenceClient(manifest)
    client.executors.known = True
    price = manifest.protected_limit_price - D("0.0001") if below_limit else None
    install_evidence(monkeypatch, manifest, order_type=order_type, price=price)
    with pytest.raises((RuntimeError, ValueError)):
        synchronize(protected_case, client)
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")
    assert g.current_recovery_exposure_quote() == D("4")


def test_legacy_schema_migrates_without_inventing_a_target_or_dispatching_a_sell(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    with sqlite3.connect(book.path) as conn:
        conn.execute(
            "ALTER TABLE partial_recycle_manifests DROP COLUMN target_net_profit_bps"
        )
        conn.execute(
            "ALTER TABLE partial_recycle_manifests DROP COLUMN protected_limit_price"
        )
    store = PartialRecycleStore(book.path)
    legacy = store.load(manifest.manifest_id)
    assert legacy.target_net_profit_bps is None
    assert legacy.protected_limit_price is None
    client = EvidenceClient(legacy)
    install_evidence(monkeypatch, legacy, order_type="MARKET", price=D("0.50"))
    legacy_case = (g, book, replace(decision, manifest=legacy))
    try:
        result = execute(legacy_case, client)
        assert result["executed"] is False
    except (RuntimeError, ValueError):
        pass
    assert client.executors.created == []
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")
    assert store.open_for_position("xrp-position-1") is not None


def test_legacy_confirmed_market_fill_remains_truthfully_reconcilable(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    with sqlite3.connect(book.path) as conn:
        conn.execute(
            "UPDATE partial_recycle_manifests SET target_net_profit_bps = NULL, "
            "protected_limit_price = NULL WHERE manifest_id = ?",
            (manifest.manifest_id,),
        )
    legacy = PartialRecycleStore(book.path).load(manifest.manifest_id)
    client = EvidenceClient(legacy)
    client.executors.known = True
    trade = fill(legacy, "protected-trade-1", "4", price=D("0.50"))
    install_evidence(
        monkeypatch,
        legacy,
        (trade,),
        order_type="MARKET",
        price=D("0.50"),
        status="PARTIALLY_FILLED",
    )
    synchronize(protected_case, client, manifest=legacy)
    assert book.get_lot("recovery-profit-lot").base_remaining == D("6")
    assert g.current_recovery_exposure_quote() == D("2.4")
    assert book.get_lot("entry-lot").base_remaining == D("20")


@pytest.mark.parametrize("bad_price,excess_fee", [(True, False), (False, True)])
def test_profit_protection_breach_books_real_fill_once_and_durably_blocks_completion(
    protected_case,
    monkeypatch,
    bad_price,
    excess_fee,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    client = EvidenceClient(manifest)
    client.executors.known = True
    execution_price = D("0.30") if bad_price else manifest.protected_limit_price
    bad_fill = fill(
        manifest,
        "protected-trade-1",
        "10",
        price=execution_price,
        fee=D("0.10") if excess_fee else None,
    )
    install_evidence(monkeypatch, manifest, (bad_fill,), status="FILLED")
    with pytest.raises(RuntimeError, match="profit protection conflict"):
        synchronize(protected_case, client)
    assert book.get_lot("recovery-profit-lot").base_remaining == D("0")
    assert book.get_lot("entry-lot").base_remaining == D("20")
    assert g.current_recovery_exposure_quote() == D("0")
    store = PartialRecycleStore(book.path)
    booked_pnl = store.realized_pnl(manifest.manifest_id)
    assert booked_pnl == D("10") * execution_price - bad_fill.fee_paid - D("4")
    assert store.load(manifest.manifest_id).status == PartialRecycleManifestStatus.OPEN
    restarted_book = EconomicPositionLedger(book.path)
    restarted_manifest = PartialRecycleStore(book.path).load(manifest.manifest_id)
    with pytest.raises(RuntimeError, match="profit protection conflict"):
        synchronize(
            protected_case, client, book=restarted_book, manifest=restarted_manifest
        )
    assert (
        PartialRecycleStore(book.path).realized_pnl(manifest.manifest_id) == booked_pnl
    )
    assert (
        PartialRecycleStore(book.path).load(manifest.manifest_id).status
        == PartialRecycleManifestStatus.OPEN
    )
    assert client.executors.created == []


@pytest.mark.parametrize(
    "change", ["target", "remove_limit", "raise_limit", "owner", "lot"]
)
def test_durable_manifest_prevents_target_floor_and_owner_tampering(
    protected_case, change
):
    g, book, decision = protected_case
    manifest = decision.manifest
    if change == "target":
        tampered = replace(manifest, target_net_profit_bps=D("0"))
    elif change == "remove_limit":
        tampered = replace(manifest, protected_limit_price=None)
    elif change == "raise_limit":
        tampered = replace(
            manifest, protected_limit_price=manifest.protected_limit_price + D("0.01")
        )
    elif change == "owner":
        tampered = replace(
            manifest, owners=(replace(manifest.owners[0], proposal_id="entry"),)
        )
    else:
        tampered = replace(
            manifest, owners=(replace(manifest.owners[0], lot_ids=("entry-lot",)),)
        )
    with pytest.raises(RuntimeError, match="immutable manifest"):
        validate_partial_recycle_manifest(tampered, ledger=book, governor=g)
    assert book.get_lot("entry-lot").base_remaining == D("20")
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")
    assert g.current_recovery_exposure_quote() == D("4")


def test_mixed_basis_parcel_uses_proportional_aggregate_profit_floor(
    tmp_path, monkeypatch
):
    g = governor(tmp_path)
    book = ledger(tmp_path)
    prepare_execution(
        g,
        proposal_id="entry",
        executor_id="entry-exec",
        intent=IntentKind.ENTRY,
        amount="10",
    )
    book.record_entry_fill(
        governor=g,
        position_id="xrp-position-1",
        lot_id="entry-lot",
        proposal_id="entry",
        base_amount=D("20"),
        cost_basis_quote=D("10"),
        occurred_at=1010,
    )
    for name, basis, timestamp in (("low", "4", 1020), ("high", "8", 1030)):
        add_recovery(
            g,
            book,
            proposal_id=f"recovery-{name}",
            executor_id=f"recovery-{name}-exec",
            lot_id=f"recovery-{name}-lot",
            base="10",
            basis=basis,
            occurred_at=timestamp,
        )
    decision = plan_partial_recycle(
        ledger=book,
        governor=g,
        position_id="xrp-position-1",
        trading_rule=rule(),
        current_price=D("1"),
        expected_exit_fee_bps=D("40"),
        slippage_allowance_bps=D("5"),
        now=2000,
        policy=PartialRecyclePolicy(target_net_profit_bps=D("174")),
    )
    manifest = decision.manifest
    assert manifest is not None and len(manifest.owners) == 2
    assert manifest.target_cost_basis_quote == D("12")
    client = EvidenceClient(manifest)
    client.executors.known = True
    trade = fill(manifest, "protected-trade-1", "10")
    install_evidence(monkeypatch, manifest, (trade,), status="PARTIALLY_FILLED")
    result = synchronize((g, book, decision), client)
    assert result["completed"] is False
    assert book.get_lot("recovery-low-lot").base_remaining == D("5")
    assert book.get_lot("recovery-high-lot").base_remaining == D("5")
    assert book.get_lot("recovery-low-lot").cost_basis_remaining_quote == D("2")
    assert book.get_lot("recovery-high-lot").cost_basis_remaining_quote == D("4")
    assert book.get_lot("entry-lot").base_remaining == D("20")
    assert g.current_recovery_exposure_quote() == D("6")
    realized = D("10") * trade.price - trade.fee_paid - D("6")
    assert D(result["realized_net_pnl_quote"]) == realized
    assert realized >= D("6") * D("0.0174")
    # One expensive child can still be below its own basis. This preserves
    # the existing proportional aggregate policy; it does not impose FIFO
    # disposal or a new most-expensive-child threshold.
    assert D("5") * trade.price * D("0.996") < D("4")


def test_concurrent_dispatch_claim_has_one_winner_and_survives_restart(protected_case):
    _, book, decision = protected_case
    manifest = decision.manifest
    stores = (PartialRecycleStore(book.path), PartialRecycleStore(book.path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(
            pool.map(
                lambda store: store.claim_dispatch(
                    manifest_id=manifest.manifest_id, now=2010
                ),
                stores,
            )
        )
    assert winners.count(True) == 1
    assert winners.count(False) == 1
    restarted = PartialRecycleStore(book.path)
    assert restarted.dispatch_claimed(manifest.manifest_id) is True
    assert restarted.claim_dispatch(manifest_id=manifest.manifest_id, now=2020) is False


def test_combined_live_orders_exceeding_inventory_create_a_durable_conflict(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    client = EvidenceClient(manifest)
    client.executors.known = True
    orders = tuple(
        ExecutorOrderEvidence(
            executor_id=manifest.sell_executor_id,
            order_id=f"live-order-{index}",
            order_type="LIMIT",
            side="SELL",
            amount=D("6"),
            price=manifest.protected_limit_price,
            status="OPEN",
            filled_amount=D("0"),
            average_fill_price=None,
            created_at=2001,
            updated_at=2002,
        )
        for index in (1, 2)
    )

    async def order_snapshot(client, *, executor_id):
        return ExecutorOrderSnapshot(executor_id=executor_id, orders=orders)

    async def trade_snapshot(client, *, executor_id):
        return ExecutorTradeSnapshot(executor_id=executor_id, trades=())

    monkeypatch.setattr(partial_tool, "fetch_executor_order_snapshot", order_snapshot)
    monkeypatch.setattr(partial_tool, "fetch_executor_trade_snapshot", trade_snapshot)
    with pytest.raises(RuntimeError, match="profit protection conflict"):
        synchronize(protected_case, client)
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")
    assert g.current_recovery_exposure_quote() == D("4")
    store = PartialRecycleStore(book.path)
    assert "outstanding_orders_exceed_manifest_inventory" in store.protection_conflicts(
        manifest.manifest_id
    )
    install_evidence(monkeypatch, manifest, no_orders=True)
    with pytest.raises(RuntimeError, match="profit protection conflict"):
        synchronize(protected_case, client)
    assert store.open_for_position("xrp-position-1") is not None


def test_known_executor_without_order_or_fill_proof_keeps_manifest_pending(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    client = EvidenceClient(manifest)
    client.executors.known = True
    original_lookup = client.executors.get_executor

    async def lookup(executor_id):
        raw = await original_lookup(executor_id)
        if executor_id == manifest.sell_executor_id:
            raw["config"]["execution_strategy"] = "MARKET"
            raw["config"].pop("price", None)
        return raw

    monkeypatch.setattr(client.executors, "get_executor", lookup)
    install_evidence(monkeypatch, manifest, no_orders=True)
    result = execute(protected_case, client)
    assert result["result"]["completed"] is False
    assert client.executors.created == []
    assert book.get_lot("recovery-profit-lot").base_remaining == D("10")
    assert g.current_recovery_exposure_quote() == D("4")
    assert (
        PartialRecycleStore(book.path).open_for_position("xrp-position-1") is not None
    )


def test_crash_after_bad_fill_journal_cannot_complete_when_next_snapshot_omits_trade(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    client = EvidenceClient(manifest)
    client.executors.known = True
    # Price breaches are persisted before accounting; an excess venue fee
    # exercises the remaining crash window after the realized P&L is known.
    trade = replace(fill(manifest, "protected-trade-1", "10"), fee_paid=D("1"))
    install_evidence(monkeypatch, manifest, (trade,), status="FILLED")

    def crash_after_journal(self, *, manifest_id, conflict, now):
        assert self.fill_recorded(trade.trade_id)
        raise RuntimeError("simulated crash before protection conflict commit")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            PartialRecycleStore, "record_protection_conflict", crash_after_journal
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            synchronize(protected_case, client)

    store = PartialRecycleStore(book.path)
    expected_pnl = D("10") * trade.price - trade.fee_paid - D("4")
    assert store.realized_pnl(manifest.manifest_id) == expected_pnl
    assert book.snapshot("xrp-position-1").realized_pnl_quote == expected_pnl
    assert book.get_lot("recovery-profit-lot").base_remaining == D("0")
    assert book.get_lot("entry-lot").base_remaining == D("20")
    assert store.protection_conflicts(manifest.manifest_id) == ()

    # The provider no longer returns the offending fill. Durable accounting
    # must establish the breach rather than relying on a repeated API page.
    install_evidence(monkeypatch, manifest, no_orders=True)
    with pytest.raises(RuntimeError, match="profit protection conflict"):
        synchronize(protected_case, client)
    restarted_store = PartialRecycleStore(book.path)
    assert restarted_store.protection_conflicts(manifest.manifest_id)
    assert (
        restarted_store.load(manifest.manifest_id).status
        == PartialRecycleManifestStatus.OPEN
    )
    assert restarted_store.realized_pnl(manifest.manifest_id) == expected_pnl
    with pytest.raises(RuntimeError, match="profit protection conflict"):
        assess_partial_recycle_manifest(
            manifest,
            ledger=EconomicPositionLedger(book.path),
            governor=g,
            store=restarted_store,
            now=2030,
        )


def test_crash_after_ledger_write_with_missing_journal_cannot_infer_completion(
    protected_case,
    monkeypatch,
):
    g, book, decision = protected_case
    manifest = decision.manifest
    client = EvidenceClient(manifest)
    client.executors.known = True
    trade = fill(manifest, "protected-trade-1", "10")
    install_evidence(monkeypatch, manifest, (trade,), status="FILLED")

    def crash_before_journal(self, **kwargs):
        raise RuntimeError("simulated crash after economic ledger commit")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(PartialRecycleStore, "record_fill", crash_before_journal)
        with pytest.raises(RuntimeError, match="simulated crash"):
            synchronize(protected_case, client)
    expected_pnl = D("10") * trade.price - trade.fee_paid - D("4")
    assert book.snapshot("xrp-position-1").realized_pnl_quote == expected_pnl
    assert book.get_lot("recovery-profit-lot").base_remaining == D("0")
    assert g.current_recovery_exposure_quote() == D("0")
    assert PartialRecycleStore(book.path).fill_recorded(trade.trade_id) is False

    install_evidence(monkeypatch, manifest, no_orders=True)
    with pytest.raises(RuntimeError):
        synchronize(protected_case, client)
    assert (
        PartialRecycleStore(book.path).load(manifest.manifest_id).status
        == PartialRecycleManifestStatus.OPEN
    )
    assert book.snapshot("xrp-position-1").realized_pnl_quote == expected_pnl
    assert client.executors.created == []
