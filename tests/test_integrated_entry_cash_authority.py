"""Combined funded ENTRY identity and exact productive/PARK accounting."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from decimal import localcontext
from threading import Barrier
from time import time

import pytest

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.models import (
    BreakerMode,
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)

SCOPE = dict(account_name="funded", connector_name="kraken", quote_asset="USD")
CREATE_SCOPE = dict(
    account_name="funded", connector_name="kraken", controller_id="spectra"
)


def funded(tmp_path, *, productive="100", parked="400", fees="40"):
    g = CapitalGovernor(
        tmp_path / "capital.db",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("5000"),
            max_pair_quote=D("5000"),
            max_position_quote=D("5000"),
            max_recovery_quote=D("5000"),
            max_leverage=D("1"),
        ),
    )
    assert g.activate_cash_authority(
        **SCOPE,
        opening_productive_quote=D(productive),
        opening_parked_quote=D(parked),
        activation_id="reviewed",
        activated_at=1000,
        entry_fee_buffer_bps=D(fees),
    )
    ledger = EconomicPositionLedger(tmp_path / "economic.db")
    ledger.open_position(position_id="position", pair="XRP-USD", created_at=1001)
    return g, ledger


def proposal(
    name="entry", amount="50", *, pair="XRP-USD", intent=IntentKind.ENTRY, **scope
):
    return CapitalProposal(name, pair, intent, D(amount), **(SCOPE | scope))


def reserve(g, p, *, free="500"):
    return g.reserve(p, CapitalSnapshot(free_quote=D(free), **SCOPE))


def config(*, pair="XRP-USD", amount="100", price="0.5", executor_id="buyer"):
    return dict(
        id=executor_id,
        type="order_executor",
        side=1,
        execution_strategy="LIMIT",
        connector_name="kraken",
        trading_pair=pair,
        amount=amount,
        price=price,
        leverage=1,
        position_action="OPEN",
    )


def commit(g, ledger, p, *, pid="position", protected=None, **scope):
    return g.commit_entry_submission_if_matches(
        p,
        position_id=pid,
        ledger_path=ledger.path,
        protected_buy_config=protected,
        **(CREATE_SCOPE | scope),
    )


def burn_count(g):
    with g._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM cash_buy_burns").fetchone()[0]


def pending_sale(g):
    g.bind_cash_sale(
        manifest_id="pending-sale",
        sell_executor_id="seller",
        pair="XRP-USD",
        sale_purpose="RECOVERY_PARTIAL",
        **SCOPE,
    )
    assert g.begin_cash_fill(
        event_id="pending-fill",
        manifest_id="pending-sale",
        trade_id="trade",
        order_id="order",
        executor_id="seller",
        pair="XRP-USD",
        sale_purpose="RECOVERY_PARTIAL",
        account_name="funded",
        connector_name="kraken",
        gross_proceeds_quote=D("10"),
        exit_fee_quote=D("0.04"),
        base_sold=D("20"),
        price=D("0.5"),
        fee_currency="USD",
        fee_paid=D("0.04"),
        occurred_at=2001,
    )


def test_canonical_entry_burns_fee_aware_productive_cash_atomically(tmp_path):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    assert commit(g, ledger, p, protected=config())
    state = g.cash_authority_snapshot()
    assert state.productive_quote == D("49.8") and state.parked_quote == D("400")
    assert burn_count(g) == 1
    assert g.get_entry_submission_claim(p.proposal_id)["state"] == "COMMITTED"
    stored = g.get_protected_buy_submission(p.proposal_id)
    assert stored["authorized_quote_budget"] == "50"
    assert stored["config"] == config() and stored["executor_id"] is None
    assert stored["ledger_path"] == str(ledger.path.resolve())
    assert g.reconcile_execution(
        proposal_id=p.proposal_id, executor_id="buyer", **CREATE_SCOPE
    )
    assert g.get_protected_buy_submission(p.proposal_id)["executor_id"] == "buyer"


@pytest.mark.parametrize(
    "scope", [dict(account_name="foreign"), dict(connector_name="binance")]
)
def test_reserved_cash_cannot_be_committed_to_foreign_entry_scope(tmp_path, scope):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    assert not commit(g, ledger, p, protected=config(), **scope)
    assert g.get_decision(p.proposal_id).status == DecisionStatus.RESERVED
    assert burn_count(g) == 0 and g.get_entry_submission_claim(p.proposal_id) is None


def test_cash_scope_does_not_promote_legacy_unscoped_reservation(tmp_path):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    with g._connect() as conn:
        conn.execute(
            "UPDATE capital_reservations SET account_name=NULL WHERE proposal_id=?",
            (p.proposal_id,),
        )
    assert not commit(g, ledger, p, protected=config())
    assert burn_count(g) == 0 and g.get_entry_submission_claim(p.proposal_id) is None


def test_park_and_unknown_quote_cannot_fund_entry(tmp_path):
    g, ledger = funded(tmp_path, productive="50", fees="0")
    assert reserve(g, proposal(amount="50.01")).status == DecisionStatus.REJECTED
    p = proposal("cross-quote", pair="XXRP-USDT", quote_asset="USDT")
    assert reserve(g, p).status == DecisionStatus.REJECTED
    assert burn_count(g) == 0 and g.cash_authority_snapshot().parked_quote == D("400")


def test_pending_sale_after_reservation_blocks_canonical_commit_after_restart(tmp_path):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    pending_sale(g)
    g = CapitalGovernor(g.db_path, g.policy)
    assert g.cash_authority_snapshot().blocked
    assert not commit(g, ledger, p, protected=config())
    assert g.get_decision(p.proposal_id).status == DecisionStatus.RESERVED
    assert burn_count(g) == 0 and g.get_entry_submission_claim(p.proposal_id) is None


def test_bad_protected_config_rolls_back_burn_and_identity_together(tmp_path):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    with pytest.raises(ValueError, match="original quote budget"):
        commit(g, ledger, p, protected=config(amount="101"))
    assert burn_count(g) == 0
    assert g.cash_authority_snapshot().productive_quote == D("100")
    assert g.get_entry_submission_claim(p.proposal_id) is None
    assert g.get_protected_buy_submission(p.proposal_id) is None


def test_uncertain_create_budget_and_identity_remain_locked_after_restart(tmp_path):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    assert commit(g, ledger, p, protected=config())
    reopened = CapitalGovernor(g.db_path, g.policy)
    assert not commit(reopened, ledger, p, protected=config())
    second = proposal("second", amount="10")
    assert reserve(reopened, second).status == DecisionStatus.RESERVED
    with pytest.raises(ValueError, match="protected ENTRY"):
        commit(reopened, ledger, second, protected=config(amount="20"))
    assert burn_count(reopened) == 1
    assert reopened.cash_authority_snapshot().productive_quote == D("49.8")


def test_concurrent_alias_claims_burn_once_and_preserve_other_reservations(tmp_path):
    g, first = funded(tmp_path)
    pairs = ["XRP-USD", "XXRP-USD"] * 3
    ledgers, proposals = [], []
    for index, pair in enumerate(pairs):
        ledger = EconomicPositionLedger(tmp_path / f"economic-{index}.db")
        ledger.open_position(
            position_id=f"position-{index}", pair=pair, created_at=1001
        )
        p = proposal(f"entry-{index}", amount="10", pair=pair)
        assert reserve(g, p).status == DecisionStatus.RESERVED
        ledgers.append(ledger)
        proposals.append(p)
    barrier = Barrier(len(proposals))

    def attempt(index):
        barrier.wait()
        try:
            return commit(
                g,
                ledgers[index],
                proposals[index],
                pid=f"position-{index}",
                protected=config(pair=pairs[index], amount="20"),
            )
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(attempt, range(6))).count(True) == 1
    assert burn_count(g) == 1
    assert g.cash_authority_snapshot().productive_quote == D("89.96")
    assert (
        sum(
            g.get_decision(p.proposal_id).status == DecisionStatus.RESERVED
            for p in proposals
        )
        == 5
    )


def test_scoped_recovery_keeps_cash_burn_and_exact_buy_config(tmp_path):
    g, ledger = funded(tmp_path)
    entry = proposal()
    assert reserve(g, entry).status == DecisionStatus.RESERVED
    assert commit(g, ledger, entry, protected=config(executor_id="entry-buyer"))
    assert g.reconcile_execution(
        proposal_id=entry.proposal_id, executor_id="entry-buyer", **CREATE_SCOPE
    )
    ledger.record_entry_fill(
        governor=g,
        position_id="position",
        lot_id="entry-lot",
        proposal_id=entry.proposal_id,
        base_amount=D("100"),
        cost_basis_quote=D("50"),
        occurred_at=2001,
    )
    recovery = proposal("recovery", amount="10", intent=IntentKind.RECOVERY)
    assert reserve(g, recovery).status == DecisionStatus.RESERVED
    assert g.commit_recovery_submission_if_matches(
        recovery,
        position_id="position",
        ledger_path=ledger.path,
        protected_buy_config=config(amount="20"),
        **CREATE_SCOPE,
    )
    assert g.cash_authority_snapshot().productive_quote == D("39.76")
    assert burn_count(g) == 2
    assert g.get_protected_buy_submission("recovery")["position_id"] == "position"


def test_confirmed_buy_fees_are_reconciled_once_against_full_native_budget(tmp_path):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    assert commit(g, ledger, p, protected=config(amount="98"))
    assert g.reconcile_execution(
        proposal_id=p.proposal_id, executor_id="buyer", **CREATE_SCOPE
    )
    fill = dict(
        trade_id="buy-fill",
        order_id="buy-order",
        proposal_id=p.proposal_id,
        executor_id="buyer",
        pair="XRP-USD",
        account_name="funded",
        connector_name="kraken",
        gross_spent_quote=D("49"),
        base_acquired=D("98"),
        price=D("0.5"),
        fee_paid=D("0.196"),
        fee_currency="USD",
        occurred_at=2002,
    )
    assert g.record_cash_buy_fill(**fill)
    assert g.record_cash_buy_fill(**fill)
    assert g.cash_authority_snapshot().productive_quote == D("49.8")
    with g._connect() as conn:
        rows = conn.execute("SELECT spent_quote FROM cash_buy_fills").fetchall()
    assert [D(row["spent_quote"]) for row in rows] == [D("49.196")]


def test_actual_buy_overrun_records_truth_and_blocks_next_entry(tmp_path):
    g, ledger = funded(tmp_path, fees="0")
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    assert commit(g, ledger, p, protected=config())
    assert g.reconcile_execution(
        proposal_id=p.proposal_id, executor_id="buyer", **CREATE_SCOPE
    )
    assert g.record_cash_buy_fill(
        trade_id="overrun",
        order_id="buy-order",
        proposal_id=p.proposal_id,
        executor_id="buyer",
        pair="XRP-USD",
        account_name="funded",
        connector_name="kraken",
        gross_spent_quote=D("50"),
        base_acquired=D("100"),
        price=D("0.5"),
        fee_paid=D("0.2"),
        fee_currency="USD",
        occurred_at=2002,
    )
    assert g.cash_authority_snapshot().productive_quote == D("49.8")
    assert g.cash_authority_snapshot().blocked
    assert (
        reserve(g, proposal("next", pair="BTC-USD", amount="10")).status
        == DecisionStatus.REJECTED
    )


def test_fee_aware_burn_remains_exact_under_small_decimal_context(tmp_path):
    g, ledger = funded(tmp_path, productive="100.00000000000000000000000001")
    p = proposal(amount="10.123456789123456789")
    with localcontext() as context:
        context.prec = 6
        assert reserve(g, p).status == DecisionStatus.RESERVED
        assert commit(g, ledger, p, protected=config(amount="1"))
        state = g.cash_authority_snapshot()
    assert state.productive_quote == D("89.83604938372004938384400001")


class ProtectedClient:
    def __init__(self, *, uncertain=False, rules=None):
        self.calls = []
        self.uncertain = uncertain
        self.rules = rules
        self.connectors = self
        self.executors = self

    async def get_trading_rules(self, *, connector_name, trading_pairs):
        assert connector_name == "kraken"
        if self.rules is not None:
            return self.rules
        return {
            pair: dict(
                min_order_size="1",
                min_notional_size="1",
                min_price_increment="0.0001",
                min_base_amount_increment="0.01",
                min_quote_amount_increment="0.0001",
                supports_limit_orders=True,
                supports_market_orders=True,
            )
            for pair in trading_pairs
        }

    async def create_executor(self, *, executor_config, account_name, controller_id):
        self.calls.append((executor_config, account_name, controller_id))
        if self.uncertain:
            raise RuntimeError("synthetic lost create response")
        return {"executor_id": executor_config["id"]}


def configure_protected_wrapper(
    monkeypatch,
    g,
    ledger,
    *,
    ask="0.5001",
    age=0,
    venue_base="1",
    venue_cost="1",
    rules_age=None,
):
    from mcp_servers.hummingbot_api.tools import execution_book, executor_create
    from mcp_servers.hummingbot_api.tools.execution_book import FreshVenueBook
    from spectra_core.economic_position import normalized_base_asset

    monkeypatch.setattr(executor_create.settings, "agent_slug", "spectra")
    monkeypatch.setattr(executor_create, "get_capital_governor", lambda: g)
    monkeypatch.setattr(executor_create, "get_economic_position_ledger", lambda: ledger)
    monkeypatch.setattr(executor_create, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    monkeypatch.setattr(
        executor_create.executor_preferences,
        "merge_with_defaults",
        lambda _, cfg: dict(cfg),
    )
    calls = []

    async def fresh(rule):
        calls.append(rule)
        observed = time() - age
        return FreshVenueBook(
            rule.trading_pair,
            "XRPUSD",
            D("0.4999"),
            D(ask),
            ((D("0.4999"), D("1000")),),
            ((D(ask), D("1000")),),
            observed - 0.01,
            observed,
            "test-fresh-Kraken-Depth",
            "a" * 64,
            venue_min_order_size=(None if venue_base is None else D(venue_base)),
            venue_min_cost_quote=(None if venue_cost is None else D(venue_cost)),
            venue_base_asset=normalized_base_asset(rule.trading_pair),
            venue_quote_asset=rule.trading_pair.rsplit("-", 1)[1],
            venue_rules_observed_at=(
                observed if rules_age is None else time() - rules_age
            ),
            venue_rules_sha256="d" * 64,
        )

    monkeypatch.setattr(execution_book, "fetch_protected_kraken_book", fresh)
    return executor_create, calls


def protected_create(module, client, p, *, prices=None, amounts=None, **policy):
    return asyncio.run(
        module.create_dca_executor(
            client,
            connector_name="kraken",
            trading_pair=p.pair,
            side=1,
            amounts_quote=amounts or [str(p.amount_quote)],
            prices=prices or ["0.5"],
            leverage=1,
            mode="TAKER",
            account_name="funded",
            spectra_proposal_id=p.proposal_id,
            spectra_intent=p.intent.value,
            spectra_position_id="position",
            **policy,
        )
    )


POLICY = dict(spectra_max_price_slippage_bps="50", spectra_fee_allowance_bps="40")


@pytest.mark.parametrize("policy", [{}, POLICY])
def test_unactivated_spectra_still_requires_bounded_buy_policy(
    monkeypatch, tmp_path, policy
):
    activated, ledger = funded(tmp_path)
    g = CapitalGovernor(tmp_path / "unactivated.db", activated.policy)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    assert g.cash_authority_snapshot() is None
    module, books = configure_protected_wrapper(monkeypatch, g, ledger)
    client = ProtectedClient()
    result = protected_create(module, client, p, **policy)
    if not policy:
        assert "explicit" in result["error"]
        assert not client.calls and not books
        assert g.get_decision("entry").status == DecisionStatus.RESERVED
    else:
        assert "error" not in result
        assert client.calls[0][0]["type"] == "order_executor"
        assert client.calls[0][0]["execution_strategy"] == "LIMIT"
        assert (
            g.get_protected_buy_submission("entry")["authorized_quote_budget"] == "50"
        )


def test_inactive_cash_conflict_blocks_every_same_scope_new_spend_after_restart(
    monkeypatch, tmp_path
):
    activated, ledger = funded(tmp_path)
    g = CapitalGovernor(tmp_path / "unactivated.db", activated.policy)
    entry = proposal()
    ready = proposal("ready", amount="10", pair="BTC-USD")
    recovery = proposal("ready-recovery", amount="10", intent=IntentKind.RECOVERY)
    envelope = proposal("envelope", amount="80", intent=IntentKind.RECOVERY)
    for p in (entry, ready, recovery, envelope):
        assert reserve(g, p).status == DecisionStatus.RESERVED
    module, _ = configure_protected_wrapper(monkeypatch, g, ledger)
    assert "error" not in protected_create(module, ProtectedClient(), entry, **POLICY)
    g.record_protected_buy_conflict(
        proposal_id="entry", reason="actual order breached price cap"
    )
    reopened = CapitalGovernor(g.db_path, g.policy)
    ledger.open_position(position_id="second", pair="BTC-USD", created_at=2001)
    assert reopened.cash_authority_snapshot() is None
    assert (
        reserve(reopened, proposal("new", pair="BTC-USD", amount="10")).status
        == DecisionStatus.REJECTED
    )
    assert reserve(reopened, entry).status == DecisionStatus.REJECTED
    assert not reopened.commit_if_matches(ready)
    assert not reopened.commit("ready")
    assert not reopened.commit_entry_submission_if_matches(
        ready, position_id="second", ledger_path=ledger.path, **CREATE_SCOPE
    )
    assert not reopened.commit_recovery_submission_if_matches(
        recovery, position_id="position", ledger_path=ledger.path, **CREATE_SCOPE
    )
    child = proposal("child", amount="10", intent=IntentKind.RECOVERY)
    assert not reopened.carve_reserved_recovery_tranche(
        envelope_proposal_id="envelope", tranche_proposal=child
    )
    assert reopened.get_decision("envelope").reserved_quote == D("80")
    with pytest.raises(RuntimeError, match="protected BUY execution conflict"):
        reopened.activate_cash_authority(
            **SCOPE,
            opening_productive_quote=D("100"),
            opening_parked_quote=D("400"),
            activation_id="later",
            activated_at=2002,
        )
    assert reopened.cash_authority_snapshot() is None
    assert reopened.get_protected_buy_conflicts("entry") == (
        "actual order breached price cap",
    )


def test_protected_conflict_gate_is_scoped_and_unknown_legacy_scope_fails_closed(
    monkeypatch, tmp_path
):
    activated, ledger = funded(tmp_path)
    g = CapitalGovernor(tmp_path / "unactivated.db", activated.policy)
    entry = proposal()
    legacy = CapitalProposal("legacy", "BTC-USD", IntentKind.RECOVERY, D("10"))
    assert reserve(g, entry).status == DecisionStatus.RESERVED
    assert (
        g.reserve(legacy, CapitalSnapshot(free_quote=D("500"))).status
        == DecisionStatus.RESERVED
    )
    module, _ = configure_protected_wrapper(monkeypatch, g, ledger)
    assert "error" not in protected_create(module, ProtectedClient(), entry, **POLICY)
    g.record_protected_buy_conflict(proposal_id="entry", reason="fee ceiling breached")
    assert not g.commit("legacy") and not g.commit_if_matches(legacy)
    assert g.protected_buy_scope_rejection(account_name=None, connector_name=None)
    assert g.protected_buy_scope_rejection(
        account_name="funded", connector_name="Kraken"
    )
    assert not g.protected_buy_scope_rejection(
        account_name="different", connector_name="kraken"
    )
    assert not g.protected_buy_scope_rejection(
        account_name="funded", connector_name="different"
    )
    other = proposal("other", pair="BTC-USD", amount="10", account_name="different")
    assert (
        g.reserve(
            other,
            CapitalSnapshot(
                free_quote=D("500"), **(SCOPE | {"account_name": "different"})
            ),
        ).status
        == DecisionStatus.RESERVED
    )
    assert g.commit_if_matches(other)


def test_live_boundary_submits_bounded_limit_but_commits_original_native_budget(
    monkeypatch, tmp_path
):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, book_calls = configure_protected_wrapper(monkeypatch, g, ledger)
    client = ProtectedClient()
    result = protected_create(module, client, p, **POLICY)
    assert "error" not in result
    backend, account, controller = client.calls[0]
    assert (
        backend["type"] == "order_executor" and backend["execution_strategy"] == "LIMIT"
    )
    assert backend["side"] == 1 and "mode" not in backend
    assert D(backend["price"]) == D("0.5001")
    assert result["executor_type"] == "order_executor"
    assert result["requested_executor_type"] == "dca_executor"
    assert (account, controller) == ("funded", "spectra") and len(book_calls) == 1
    stored = g.get_protected_buy_submission("entry")
    assert stored["executor_id"] == backend["id"]
    assert stored["authorized_quote_budget"] == "50"
    assert stored["config"] == backend
    policy = stored["policy"]
    assert D(policy["reference_price"]) == D("0.5")
    assert D(policy["price_cap"]) == D("0.5025")
    assert D(policy["max_total_quote"]) <= D("50")
    assert D(policy["max_principal_quote"]) < D("50")
    assert policy["fee_allowance_bps"] == "40"
    assert policy["venue_book"]["response_sha256"] == "a" * 64
    with g._connect() as conn:
        assert D(
            conn.execute(
                "SELECT amount_quote FROM capital_reservations WHERE proposal_id='entry'"
            ).fetchone()[0]
        ) == D("50")
    assert g.cash_authority_snapshot().productive_quote == D("49.8")


@pytest.mark.parametrize(
    "policy",
    [{}, {"spectra_max_price_slippage_bps": "50"}, {"spectra_fee_allowance_bps": "40"}],
)
def test_activated_buy_missing_declared_policy_never_commits_or_submits(
    monkeypatch, tmp_path, policy
):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, books = configure_protected_wrapper(monkeypatch, g, ledger)
    client = ProtectedClient()
    result = protected_create(module, client, p, **policy)
    assert "explicit" in result["error"]
    assert not client.calls and not books
    assert g.get_decision("entry").status == DecisionStatus.RESERVED
    assert burn_count(g) == 0 and g.get_entry_submission_claim("entry") is None


@pytest.mark.parametrize(
    "ask,age,error",
    [("0.51", 0, "immutable buy price cap"), ("0.5001", 31, "stale or future")],
)
def test_unmarketable_or_stale_book_cannot_consume_native_cash(
    monkeypatch, tmp_path, ask, age, error
):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, books = configure_protected_wrapper(
        monkeypatch, g, ledger, ask=ask, age=age
    )
    client = ProtectedClient()
    result = protected_create(module, client, p, **POLICY)
    assert error in result["error"] and len(books) == 1
    assert not client.calls and burn_count(g) == 0
    assert g.get_decision("entry").status == DecisionStatus.RESERVED


def test_protected_buy_refuses_multilevel_without_collapsing_authorization(
    monkeypatch, tmp_path
):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, _ = configure_protected_wrapper(monkeypatch, g, ledger)
    client = ProtectedClient()
    result = protected_create(
        module, client, p, amounts=["25", "25"], prices=["0.5", "0.49"], **POLICY
    )
    assert "exactly one funded parcel" in result["error"]
    assert not client.calls and burn_count(g) == 0
    assert g.get_decision("entry").reserved_quote == D("50")


def test_rules_cannot_be_borrowed_from_a_different_pair(monkeypatch, tmp_path):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, books = configure_protected_wrapper(monkeypatch, g, ledger)
    client = ProtectedClient(rules={"BTC-USD": {"min_order_size": "1"}})
    result = protected_create(module, client, p, **POLICY)
    assert "exact pair scope" in result["error"]
    assert not client.calls and not books and burn_count(g) == 0


@pytest.mark.parametrize(
    "api_base,api_cost,venue_base,venue_cost",
    [
        ("1", "0", "1", "60"),  # API's default zero cannot erase venue costmin.
        ("1", "1", "200", "1"),
        ("1", "60", "1", "1"),  # Fresh venue metadata cannot relax stricter API rules.
        ("200", "1", "1", "1"),
    ],
)
def test_trusted_venue_minima_tighten_api_defaults_and_never_relax_existing_limits(
    monkeypatch,
    tmp_path,
    api_base,
    api_cost,
    venue_base,
    venue_cost,
):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, _ = configure_protected_wrapper(
        monkeypatch, g, ledger, venue_base=venue_base, venue_cost=venue_cost
    )
    client = ProtectedClient(
        rules={
            "XRP-USD": dict(
                min_order_size=api_base,
                min_notional_size=api_cost,
                min_price_increment="0.0001",
                min_base_amount_increment="0.01",
                supports_limit_orders=True,
            )
        }
    )
    result = protected_create(module, client, p, **POLICY)
    assert "cannot meet current venue minimums" in result["error"]
    assert not client.calls and burn_count(g) == 0
    assert g.cash_authority_snapshot().productive_quote == D("100")
    assert g.get_decision("entry").status == DecisionStatus.RESERVED
    assert g.get_entry_submission_claim("entry") is None


@pytest.mark.parametrize("venue_base,venue_cost", [(None, "1"), ("1", None)])
def test_missing_fresh_venue_minimum_cannot_authorize_with_api_defaults(
    monkeypatch,
    tmp_path,
    venue_base,
    venue_cost,
):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, _ = configure_protected_wrapper(
        monkeypatch, g, ledger, venue_base=venue_base, venue_cost=venue_cost
    )
    client = ProtectedClient()
    result = protected_create(module, client, p, **POLICY)
    assert "lacks exact trusted venue minimum evidence" in result["error"]
    assert not client.calls and burn_count(g) == 0
    assert g.get_decision("entry").status == DecisionStatus.RESERVED


def test_fresh_depth_cannot_refresh_stale_venue_minimum_evidence(monkeypatch, tmp_path):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, _ = configure_protected_wrapper(monkeypatch, g, ledger, rules_age=31)
    client = ProtectedClient()
    result = protected_create(module, client, p, **POLICY)
    assert "venue minimum evidence is stale or future" in result["error"]
    assert not client.calls and burn_count(g) == 0


def test_exact_venue_minimum_evidence_persists_with_original_budget(
    monkeypatch, tmp_path
):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, _ = configure_protected_wrapper(
        monkeypatch, g, ledger, venue_base="2", venue_cost="20"
    )
    client = ProtectedClient()
    assert "error" not in protected_create(module, client, p, **POLICY)
    stored = g.get_protected_buy_submission("entry")
    book = stored["policy"]["venue_book"]
    assert book["venue_min_order_size"] == "2"
    assert book["venue_min_cost_quote"] == "20"
    assert book["effective_min_order_size"] == "2"
    assert book["effective_min_quote"] == "20"
    assert book["venue_base_asset"] == "XRP" and book["venue_quote_asset"] == "USD"
    assert book["venue_rules_sha256"] == "d" * 64
    assert stored["authorized_quote_budget"] == "50"


def test_lost_limit_create_response_restarts_with_config_cash_and_identity_locked(
    monkeypatch, tmp_path
):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, _ = configure_protected_wrapper(monkeypatch, g, ledger)
    client = ProtectedClient(uncertain=True)
    result = protected_create(module, client, p, **POLICY)
    assert "lost create response" in result["error"]
    assert len(client.calls) == 1 and burn_count(g) == 1
    reopened = CapitalGovernor(g.db_path, g.policy)
    module, _ = configure_protected_wrapper(monkeypatch, reopened, ledger)
    retry = ProtectedClient()
    result = protected_create(module, retry, p, **POLICY)
    assert "capital claim refused" in result["error"] and not retry.calls
    stored = reopened.get_protected_buy_submission("entry")
    assert stored["submission_state"] == "COMMITTED" and stored["executor_id"] is None
    assert stored["config"]["execution_strategy"] == "LIMIT"
    assert stored["policy"]["fee_allowance_bps"] == "40"
    assert reopened.cash_authority_snapshot().productive_quote == D("49.8")


def test_durable_buy_breach_blocks_cash_without_discarding_later_financial_truth(
    monkeypatch, tmp_path
):
    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, _ = configure_protected_wrapper(monkeypatch, g, ledger)
    client = ProtectedClient()
    assert "error" not in protected_create(module, client, p, **POLICY)
    g.record_protected_buy_conflict(
        proposal_id="entry", reason="actual fee exceeds declared allowance"
    )
    g.record_protected_buy_conflict(
        proposal_id="entry", reason="actual fee exceeds declared allowance"
    )
    reopened = CapitalGovernor(g.db_path, g.policy)
    assert reopened.get_protected_buy_conflicts("entry") == (
        "actual fee exceeds declared allowance",
    )
    assert reopened.cash_authority_snapshot().blocked
    assert reopened.record_cash_buy_fill(
        trade_id="later-truth",
        order_id="actual-order",
        proposal_id="entry",
        executor_id=client.calls[0][0]["id"],
        pair="XRP-USD",
        account_name="funded",
        connector_name="kraken",
        gross_spent_quote=D("5"),
        base_acquired=D("10"),
        price=D("0.5"),
        fee_paid=D("0.04"),
        fee_currency="USD",
        occurred_at=2002,
    )
    with reopened._connect() as conn:
        assert D(
            conn.execute(
                "SELECT spent_quote FROM cash_buy_fills WHERE trade_id='later-truth'"
            ).fetchone()[0]
        ) == D("5.04")


@pytest.mark.parametrize(
    "rules_age,delay,error",
    [
        (None, 31, "venue book became stale"),
        (5, 27, "venue minima became stale"),
    ],
)
def test_book_expiring_inside_atomic_commit_rolls_back_cash_and_identity(
    monkeypatch,
    tmp_path,
    rules_age,
    delay,
    error,
):
    import spectra_core.capital_governor as governor_module
    from spectra_core import cash_authority

    g, ledger = funded(tmp_path)
    p = proposal()
    assert reserve(g, p).status == DecisionStatus.RESERVED
    module, _ = configure_protected_wrapper(monkeypatch, g, ledger, rules_age=rules_age)
    now = [time() + 1]
    monkeypatch.setattr(governor_module.time, "time", lambda: now[0])
    original_burn = cash_authority.burn

    def delayed_burn(*args, **kwargs):
        result = original_burn(*args, **kwargs)
        now[0] += delay
        return result

    monkeypatch.setattr(cash_authority, "burn", delayed_burn)
    client = ProtectedClient()
    result = protected_create(module, client, p, **POLICY)
    assert error in result["error"]
    assert not client.calls and burn_count(g) == 0
    assert g.cash_authority_snapshot().productive_quote == D("100")
    assert g.get_entry_submission_claim("entry") is None
    assert g.get_protected_buy_submission("entry") is None
    assert g.get_decision("entry").status == DecisionStatus.RESERVED
