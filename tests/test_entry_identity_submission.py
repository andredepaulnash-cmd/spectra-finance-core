import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from pathlib import Path
from threading import Barrier

import pytest

from mcp_servers.hummingbot_api.tools import executor_create
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.models import (
    BreakerMode, CapitalPolicy, CapitalProposal, CapitalSnapshot,
    DecisionStatus, IntentKind,
    ExecutorAccountingObservation,
)


def governor(path):
    return CapitalGovernor(path, CapitalPolicy(
        reserve_floor_quote=D(0), max_total_committed_quote=D(5000),
        max_pair_quote=D(5000), max_position_quote=D(5000),
        max_recovery_quote=D(2000), max_leverage=D(1),
    ))


def reserve(g, proposal_id, pair="XRP-USD", intent=IntentKind.ENTRY):
    proposal = CapitalProposal(proposal_id=proposal_id, pair=pair, intent=intent,
        amount_quote=D(50), leverage=D(1))
    result = g.reserve(proposal, CapitalSnapshot(
        free_quote=D(5000), committed_quote=D(0), pair_exposure_quote=D(0),
        position_exposure_quote=D(0), recovery_exposure_quote=D(0),
    ))
    assert result.status == DecisionStatus.RESERVED
    return proposal


def prefill(path, pid="position-1", pair="XRP-USD"):
    ledger = EconomicPositionLedger(path)
    ledger.open_position(position_id=pid, pair=pair, created_at=1000)
    return ledger


def commit(g, proposal, ledger, pid="position-1", account="master_account"):
    return g.commit_entry_submission_if_matches(proposal, position_id=pid,
        ledger_path=ledger.path, account_name=account,
        connector_name="kraken", controller_id="spectra")


class FakeClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.connectors = self
        self.executors = self

    async def get_trading_rules(self, connector_name, trading_pairs):
        return {pair: {"min_order_size":"1", "min_notional_size":"1"}
            for pair in trading_pairs}

    async def create_executor(self, executor_config, account_name, controller_id):
        self.calls.append((executor_config, account_name, controller_id))
        if self.fail:
            raise RuntimeError("synthetic uncertain create")
        return {"executor_id": f"fake-executor-{len(self.calls)}"}


def configure(monkeypatch, g, ledger):
    monkeypatch.setattr(executor_create.settings, "agent_slug", "spectra")
    monkeypatch.setattr(executor_create, "get_capital_governor", lambda: g)
    monkeypatch.setattr(executor_create, "get_economic_position_ledger", lambda: ledger)
    monkeypatch.setattr(executor_create, "get_breaker_mode", lambda: BreakerMode.NORMAL)
    executor_create.trading_rules_cache.clear()


def create(client, proposal, pid="position-1"):
    return asyncio.run(executor_create.create_grid_executor(client,
        connector_name="kraken", trading_pair=proposal.pair, side=1,
        start_price=1, end_price=1.1, limit_price=.9, total_amount_quote=50,
        leverage=1, spectra_proposal_id=proposal.proposal_id,
        spectra_intent="ENTRY", spectra_position_id=pid))


def test_own_prefill_is_accepted_and_second_submission_is_refused(monkeypatch, tmp_path):
    g = governor(tmp_path / "capital.sqlite")
    ledger = prefill(tmp_path / "economic.sqlite")
    configure(monkeypatch, g, ledger)
    client = FakeClient()
    p1, p2 = reserve(g,"entry-1"), reserve(g,"entry-2")
    assert "error" not in create(client,p1)
    assert "protected ENTRY" in create(client,p2)["error"]
    assert len(client.calls) == 1
    claim = g.get_entry_submission_claim(p1.proposal_id)
    assert claim["state"] == "DISPATCHED"
    assert claim["position_id"] == "position-1"
    assert claim["ledger_path"] == str(ledger.path.resolve())


def test_missing_or_foreign_position_is_refused_before_capital_commit(monkeypatch, tmp_path):
    g = governor(tmp_path / "capital.sqlite")
    ledger = prefill(tmp_path / "economic.sqlite")
    configure(monkeypatch,g,ledger)
    client = FakeClient()
    for pid in (None,"foreign-position"):
        proposal=reserve(g,f"entry-{pid}")
        assert "error" in create(client,proposal,pid)
        assert g.get_decision(proposal.proposal_id).status == DecisionStatus.RESERVED
    assert not client.calls


def test_held_inventory_cannot_be_reused_as_prefill(monkeypatch,tmp_path):
    g=governor(tmp_path/"capital.sqlite")
    ledger=prefill(tmp_path/"economic.sqlite")
    ledger.record_base_fill(position_id="position-1",lot_id="held",pair="XRP-USD",
        base_amount=D(3),cost_basis_quote=D(3),occurred_at=1001)
    configure(monkeypatch,g,ledger)
    client=FakeClient()
    assert "held active inventory" in create(client,reserve(g,"entry"))["error"]
    assert not client.calls


@pytest.mark.parametrize("alias_pair",["XBT-USD","XXBT-USDT","BTC-USDT"])
def test_canonical_alias_and_quote_competitor_is_refused_across_ledger_paths(tmp_path,alias_pair):
    g=governor(tmp_path/"capital.sqlite")
    first=prefill(tmp_path/"economic.sqlite",pair="BTC-USD")
    assert commit(g,reserve(g,"btc","BTC-USD"),first)
    competing=prefill(tmp_path/"other-economic.sqlite",pid="other",pair=alias_pair)
    with pytest.raises(ValueError,match="protected ENTRY"):
        commit(g,reserve(g,"alias",alias_pair),competing,pid="other")
    assert g.get_decision("alias").status==DecisionStatus.RESERVED


def test_ambiguous_dispatch_and_restart_never_allow_another_create(monkeypatch,tmp_path):
    g=governor(tmp_path/"capital.sqlite")
    ledger=prefill(tmp_path/"economic.sqlite")
    configure(monkeypatch,g,ledger)
    client=FakeClient(fail=True)
    first=reserve(g,"uncertain")
    assert "synthetic uncertain" in create(client,first)["error"]
    restarted=governor(g.db_path)
    configure(monkeypatch,restarted,EconomicPositionLedger(ledger.path))
    assert "error" in create(client,first)
    second=reserve(restarted,"next")
    assert "protected ENTRY" in create(client,second)["error"]
    assert len(client.calls)==1
    claim=restarted.get_entry_submission_claim("uncertain")
    assert claim["state"]=="COMMITTED" and claim["executor_id"] is None
    assert restarted.get_decision("uncertain").status==DecisionStatus.COMMITTED


def test_concurrent_proposals_have_exactly_one_committed_identity(tmp_path):
    g=governor(tmp_path/"capital.sqlite")
    ledger=prefill(tmp_path/"economic.sqlite")
    proposals=[reserve(g,f"concurrent-{i}") for i in range(6)]
    barrier=Barrier(len(proposals))
    def compete(proposal):
        barrier.wait()
        try:
            return commit(g,proposal,ledger)
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=len(proposals)) as pool:
        results=list(pool.map(compete,proposals))
    assert results.count(True)==1
    assert sum(g.get_decision(p.proposal_id).status==DecisionStatus.COMMITTED for p in proposals)==1
    assert len(g.get_entry_submissions_for_position(position_id="position-1",ledger_path=ledger.path))==1


def test_receipt_account_scope_cannot_drift_after_commit(tmp_path):
    g=governor(tmp_path/"capital.sqlite")
    ledger=prefill(tmp_path/"economic.sqlite")
    proposal=reserve(g,"entry")
    assert commit(g,proposal,ledger)
    with pytest.raises(RuntimeError,match="scope conflicts"):
        g.reconcile_execution(proposal_id="entry",executor_id="fake",
            account_name="other-account",connector_name="kraken",controller_id="spectra")
    assert g.get_execution_receipt("entry") is None
    assert g.get_entry_submission_claim("entry")["state"]=="COMMITTED"
    assert g.reconcile_execution(proposal_id="entry",executor_id="fake",
        account_name="master_account",connector_name="kraken",controller_id="spectra")


@pytest.mark.parametrize("override",[
    {"account_name":"other"}, {"controller_id":"other"},
    {"connector_name":"other"}, {"position_id":"other"},
])
def test_recovery_scope_is_bound_to_existing_guarded_entry(tmp_path,override):
    g=governor(tmp_path/"capital.sqlite")
    ledger=prefill(tmp_path/"economic.sqlite")
    assert commit(g,reserve(g,"entry"),ledger)
    proposal=reserve(g,"recovery",intent=IntentKind.RECOVERY)
    scope=dict(account_name="master_account",connector_name="kraken",
        controller_id="spectra",position_id="position-1")
    scope.update(override)
    with pytest.raises(ValueError,match="RECOVERY scope"):
        g.commit_recovery_submission_if_matches(proposal,**scope)
    assert g.get_decision("recovery").status==DecisionStatus.RESERVED


def test_recovery_is_accepted_and_unknown_executor_stays_visible_for_retirement(tmp_path):
    g=governor(tmp_path/"capital.sqlite")
    ledger=prefill(tmp_path/"economic.sqlite")
    assert commit(g,reserve(g,"entry"),ledger)
    proposal=reserve(g,"recovery",intent=IntentKind.RECOVERY)
    assert g.commit_recovery_submission_if_matches(proposal,
        account_name="master_account",connector_name="kraken",controller_id="spectra",
        position_id="position-1")
    owners={row["proposal_id"]:row for row in g.get_asset_submission_proposals("XRP")}
    assert owners["recovery"]["position_id"]=="position-1"
    assert owners["recovery"]["submission_state"]=="COMMITTED"
    assert owners["recovery"]["executor_id"] is None


def test_exact_flat_exit_without_terminal_certificate_keeps_entry_lock(tmp_path):
    g=governor(tmp_path/"capital.sqlite")
    ledger=prefill(tmp_path/"economic.sqlite")
    assert commit(g,reserve(g,"entry"),ledger)
    assert g.reconcile_execution(proposal_id="entry",executor_id="fake",
        account_name="master_account",connector_name="kraken",controller_id="spectra")
    ledger.record_entry_fill(governor=g,position_id="position-1",lot_id="buy",
        proposal_id="entry",base_amount=D(50),cost_basis_quote=D(50),occurred_at=1001)
    ledger.record_partial_exit(event_id="sell",position_id="position-1",base_sold=D(50),
        gross_proceeds_quote=D(51),occurred_at=1002)
    ledger.open_position(position_id="replacement",pair="XRP-USD",created_at=1003)
    with pytest.raises(ValueError,match="protected ENTRY"):
        commit(g,reserve(g,"replacement"),ledger,pid="replacement")


def test_recovery_cannot_submit_after_old_position_is_flat(tmp_path):
    g=governor(tmp_path/"capital.sqlite")
    ledger=prefill(tmp_path/"economic.sqlite")
    assert commit(g,reserve(g,"entry"),ledger)
    assert g.reconcile_execution(proposal_id="entry",executor_id="fake",
        account_name="master_account",connector_name="kraken",controller_id="spectra")
    ledger.record_entry_fill(governor=g,position_id="position-1",lot_id="buy",
        proposal_id="entry",base_amount=D(50),cost_basis_quote=D(50),occurred_at=1001)
    ledger.record_partial_exit(event_id="sell",position_id="position-1",base_sold=D(50),
        gross_proceeds_quote=D(51),occurred_at=1002)
    proposal=reserve(g,"late-recovery",intent=IntentKind.RECOVERY)
    with pytest.raises(ValueError,match="ACTIVE economic position"):
        g.commit_recovery_submission_if_matches(proposal,
            account_name="master_account",connector_name="kraken",controller_id="spectra",
            position_id="position-1")
    assert g.get_decision("late-recovery").status==DecisionStatus.RESERVED
    assert not [row for row in g.get_asset_submission_proposals("XRP")
        if row["proposal_id"]=="late-recovery" and row["submission_state"] is not None]


def test_native_tool_contract_forwards_exact_position_identity():
    # Importing the full server starts application dependencies; validate its
    # actual tool signatures and forwarding call without executing the server.
    import ast
    path=Path(__file__).parents[1]/"mcp_servers/hummingbot_api/server.py"
    tree=ast.parse(path.read_text())
    for name in ("create_grid_executor","create_dca_executor"):
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
        args=node.args.args+node.args.kwonlyargs
        assert "spectra_position_id" in {a.arg for a in args}
        forwarded=[call for call in ast.walk(node) if isinstance(call,ast.Call)
            and isinstance(call.func,ast.Attribute) and call.func.attr==name]
        assert len(forwarded)==1
        assert any(k.arg=="spectra_position_id" and isinstance(k.value,ast.Name)
            and k.value.id=="spectra_position_id" for k in forwarded[0].keywords)


def test_fresh_identical_terminal_accounting_survives_restart_without_recredit(monkeypatch,tmp_path):
    import spectra_core.capital_governor as module
    clock=[5000.0]
    monkeypatch.setattr(module.time,"time",lambda:clock[0])
    g=governor(tmp_path/"capital.sqlite")
    proposal=reserve(g,"recovery",intent=IntentKind.RECOVERY)
    assert g.commit(proposal.proposal_id)
    assert g.reconcile_execution(proposal_id=proposal.proposal_id,executor_id="terminal",
        account_name="master_account",connector_name="kraken",controller_id="spectra")
    observation=ExecutorAccountingObservation(executor_id="terminal",executor_type="grid_executor",
        status="TERMINATED",close_type="TAKE_PROFIT",filled_amount_quote=D(12),
        cum_fees_quote=D(".12"),position_size_quote=D(0))
    first=g.reconcile_executor_accounting(observation)
    assert first.filled_delta_quote==D(12)
    clock[0]=5061.0
    reopened=governor(g.db_path)
    assert reopened.get_latest_executor_accounting("terminal")[1]==5000.0
    refreshed=reopened.reconcile_executor_accounting(observation)
    assert refreshed.filled_delta_quote==D(0) and refreshed.fee_delta_quote==D(0)
    assert reopened.get_latest_executor_accounting("terminal")[1]==5061.0
    with reopened._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM executor_accounting_observations").fetchone()[0]==1


def test_verified_empty_terminal_certificate_allows_replacement_entry_after_restart(tmp_path):
    from tests.test_residual_position_lifecycle import fixture
    g,ledger,kwargs=fixture(tmp_path,empty=True,guarded_empty_noorders=True)
    certificate=ledger.retire_terminal_position(**kwargs)
    assert certificate["kind"]=="EMPTY_TERMINAL"
    restarted=governor(g.db_path)
    ledger=EconomicPositionLedger(ledger.path)
    ledger.open_position(position_id="replacement",pair="XXRP-USDT",created_at=kwargs["now"]+1)
    proposal=reserve(restarted,"replacement","XXRP-USDT")
    assert commit(restarted,proposal,ledger,pid="replacement")
    assert restarted.get_entry_submission_claim("owner")["state"]=="RETIRED"
    assert restarted.get_entry_submission_claim("replacement")["state"]=="COMMITTED"


def test_corrupt_terminal_certificate_keeps_old_entry_protected(tmp_path):
    import json
    from tests.test_residual_position_lifecycle import fixture
    g,ledger,kwargs=fixture(tmp_path,empty=True,guarded_empty_noorders=True)
    certificate=ledger.retire_terminal_position(**kwargs)
    certificate["proof"]["current_price"]="corrupt-proof"
    with ledger._connect() as conn:
        conn.execute("UPDATE economic_position_retirements SET certificate_json=?",
            (json.dumps(certificate,default=str),))
    ledger.open_position(position_id="replacement",pair="XRP-USD",created_at=kwargs["now"]+1)
    proposal=reserve(g,"replacement")
    with pytest.raises(ValueError,match="protected ENTRY"):
        commit(g,proposal,ledger,pid="replacement")
    assert g.get_entry_submission_claim("owner")["state"]=="DISPATCHED"


@pytest.mark.parametrize("receipt", [False, True])
def test_uncertified_legacy_recovery_blocks_alias_entry_before_create(monkeypatch,tmp_path,receipt):
    g=governor(tmp_path/"capital.sqlite")
    old=reserve(g,"legacy-recovery","XBT-USD",IntentKind.RECOVERY)
    assert g.commit(old.proposal_id)
    if receipt:
        assert g.reconcile_execution(proposal_id=old.proposal_id,executor_id="old-executor",
            account_name="master_account",connector_name="kraken",controller_id="spectra")
    restarted=governor(g.db_path)
    ledger=prefill(tmp_path/"economic.sqlite",pair="XXBT-USDT")
    configure(monkeypatch,restarted,ledger)
    client=FakeClient()
    new=reserve(restarted,"replacement","XXBT-USDT")
    assert "uncertified protected submission" in create(client,new)["error"]
    assert not client.calls
    assert restarted.get_decision(new.proposal_id).status==DecisionStatus.RESERVED
    assert restarted.get_entry_submission_claim(new.proposal_id) is None


def test_certified_legacy_recovery_does_not_block_alias_entry_rotation(tmp_path):
    from tests.test_residual_position_lifecycle import fixture
    g,ledger,kwargs=fixture(tmp_path,intent=IntentKind.RECOVERY)
    certificate=ledger.retire_terminal_position(**kwargs)
    assert certificate["proposal_ids"]==["owner"]
    ledger.open_position(position_id="replacement",pair="XXRP-USDT",created_at=kwargs["now"]+1)
    proposal=reserve(g,"replacement","XXRP-USDT")
    assert commit(g,proposal,ledger,pid="replacement")


def test_guarded_recovery_basis_floor_survives_release_restart_and_missing_ledger(tmp_path):
    from tests.test_residual_position_lifecycle import fixture
    g,ledger,_=fixture(tmp_path,intent=IntentKind.RECOVERY,guarded_recovery=True)
    g.reconcile_executor_accounting(ExecutorAccountingObservation("executor","TERMINATED",
        D("149.99"),D(".3"),position_size_quote=D(0),close_type="COMPLETED",executor_type="dca_executor"))
    remaining=ledger.snapshot("position").active_cost_basis_quote
    assert remaining==D(".005")
    g.record_recovery_recycle_release(release_id="already-sold",proposal_id="owner",amount_quote=D(50))
    restarted=governor(g.db_path)
    assert restarted.current_recovery_exposure_quote()==remaining
    saved=ledger.path.with_name("saved-ledger.db")
    ledger.path.rename(saved)
    try:
        assert restarted.current_recovery_exposure_quote()==D(100)
    finally:
        saved.rename(ledger.path)
    with ledger._connect() as conn:
        conn.execute("UPDATE economic_lots SET executor_id='wrong-owner' WHERE proposal_id='owner'")
    assert restarted.current_recovery_exposure_quote()==D(100)
