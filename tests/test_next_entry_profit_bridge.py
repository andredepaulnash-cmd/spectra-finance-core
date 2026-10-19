"""Synthetic paper receipts with real native SQLite ledger/reconciliation calls."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import types

import pytest

import spectra_core.capital_governor as governor_module
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.fill_reconciliation import EconomicFillRole, LiveFillLedgerReconciler
from spectra_core.models import CapitalPolicy, CapitalProposal, CapitalSnapshot, IntentKind
from spectra_core.next_entry_allocator import Allocator, Member, MembershipSnapshot, Policy
from spectra_core.next_entry_profit_bridge import ESCROW_POOL, NativeSettledProfitBridge, ProfitBridgeBlocked

D = Decimal
NOW = 1_800_000_100
ACCOUNT = "paper-account"
CONNECTOR = "isolated-paper"
OWNER = "spectra-paper"


def encoded(item):
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def allocator(root):
    return Allocator(root / "allocator.sqlite", account_id=ACCOUNT, currency="USD",
                     policy=Policy(D("0.00000001"), D("0.01"), 300, 60))


def bridge(root, *, enabled=True, hook=None):
    return NativeSettledProfitBridge(paper_db=root / "paper.sqlite", native_db=root / "native.sqlite",
        allocator=allocator(root), account_id=ACCOUNT, connector=CONNECTOR, owner_id=OWNER,
        enabled=enabled, fault_hook=hook)


def members():
    return MembershipSnapshot("members-v1", ACCOUNT, "USD", NOW - 1000, NOW + 1000,
        (Member("BTC", "btc-owner", "POSITION_ALLOCATION"), Member("VCAP", "vcap-owner", "VCAP")))


def mutate_state(root, function):
    with sqlite3.connect(root / "paper.sqlite") as db:
        value = json.loads(db.execute("SELECT value FROM state WHERE id=1").fetchone()[0])
        function(value)
        db.execute("UPDATE state SET value=? WHERE id=1", (encoded(value),))


def execute(root, statement, args=(), *, native=False):
    with sqlite3.connect(root / ("native.sqlite" if native else "paper.sqlite")) as db:
        return db.execute(statement, args).fetchall()


def seed(root, monkeypatch, *, prices=("80", "120"), sale_price="120", sale_amount=None,
         entry_fee="1", exit_fee="2", terminal=True):
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(governor_module, "time", types.SimpleNamespace(time=lambda: NOW - 80))
    native = root / "native.sqlite"
    gov = CapitalGovernor(native, CapitalPolicy(D(0), D(10000), D(10000), D(10000), D(5000)))
    ledger = EconomicPositionLedger(native)
    ledger.open_position(position_id="position", pair="BTC-USD", created_at=NOW - 90)
    proposal = CapitalProposal("proposal", "BTC-USD", IntentKind.ENTRY, D(1000))
    gov.reserve(proposal, CapitalSnapshot(D(10000)))
    assert gov.commit("proposal")
    assert gov.reconcile_execution(proposal_id="proposal", executor_id="executor",
        account_name=ACCOUNT, connector_name=CONNECTOR, controller_id=OWNER)
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=gov)
    for oid, role in (("buy", EconomicFillRole.BASE_ENTRY),):
        reconciler.bind_executor_order(order_id=oid, position_id="position", role=role,
            proposal_id="proposal", account_name=ACCOUNT, connector_name=CONNECTOR,
            pair="BTC-USD", created_at=NOW - 70)
    receipts = []
    def raw(trade, order, side, quantity, price, fee, when):
        return dict(trade_id=trade, order_id=order, account_name=ACCOUNT, connector_name=CONNECTOR,
            trading_pair="BTC-USD", trade_type=side, amount=str(quantity), price=price,
            fee_paid=fee, fee_currency="USD", timestamp=datetime.fromtimestamp(when, timezone.utc).isoformat())
    for index, price in enumerate(prices):
        fill = raw("buy-" + str(index), "buy", "BUY", "1", price, entry_fee, NOW - 60 + index)
        reconciler.reconcile_trade_fill(fill, reconciled_at=NOW - 50)
        receipts.append(fill)
    reconciler.bind_executor_order(order_id="sell", position_id="position", role=EconomicFillRole.POSITION_EXIT,
        proposal_id="proposal", account_name=ACCOUNT, connector_name=CONNECTOR,
        pair="BTC-USD", created_at=NOW - 40)
    quantity = str(len(prices)) if sale_amount is None else sale_amount
    fill = raw("sell-fill", "sell", "SELL", quantity, sale_price, exit_fee, NOW - 20)
    reconciler.reconcile_trade_fill(fill, reconciled_at=NOW - 15)
    receipts.append(fill)
    shared = dict(pair="BTC-USD", position="position", proposal="proposal", executor="executor", pool="seed", target="0")
    state = {"identity": {"schema": 1, "sources": [], "config": {"account": ACCOUNT, "connector": CONNECTOR,
              "owner": OWNER, "initial_cash": "2000", "protected_cash": "500"}},
        "now": NOW, "quarantine": [], "breaker": "NORMAL", "orders": {
            "buy": dict(shared, id="buy", side="BUY", status="FILLED", terminal="FILLED", quantity=str(len(prices)), decision_at=NOW-70),
            "sell": dict(shared, id="sell", side="SELL", status="FILLED" if terminal else "PARTIAL",
                         terminal="FILLED" if terminal else None, quantity=quantity, decision_at=NOW-40,
                         eligible_lot_ids=["trade-fill:buy-" + str(i) for i in range(len(prices))])}}
    with sqlite3.connect(root / "paper.sqlite") as db:
        db.executescript("""
            CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE receipts (id TEXT PRIMARY KEY, order_id TEXT NOT NULL, payload TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE audit_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        db.execute("INSERT INTO state VALUES (1,?)", (encoded(state),))
        for receipt in receipts:
            db.execute("INSERT INTO receipts VALUES (?,?,?,1)", (receipt["trade_id"], receipt["order_id"], encoded(receipt)))
        if terminal:
            db.execute("INSERT INTO audit_events(at,kind,payload) VALUES (?,?,?)", (NOW-10, "terminal_reconciliation", encoded({"order": "sell", "status": "FILLED"})))
    return bridge(root)


def test_disabled_does_not_touch_cash_store(tmp_path):
    current = bridge(tmp_path, enabled=False)
    assert not (tmp_path / "paper.sqlite").exists()
    with pytest.raises(ProfitBridgeBlocked, match="disabled"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)


def test_mixed_lots_fee_net_profit_and_balanced_projection(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch, prices=("60", "140"), sale_price="120")
    staged = current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    event = json.loads(staged["event_payload"])
    assert event["gross_realized_proceeds"] == "240"
    assert D(event["returned_principal"]) == 200
    assert D(event["total_attributable_fees"]) == 4
    assert D(event["fee_net_realized_profit"]) == 36
    source = json.loads(staged["source_payload"])
    assert len(source["allocations"]) == 2  # The losing 140-basis lot is included.
    assert allocator(tmp_path).audit()["eligible_profit"] == "0.00000000"
    projection = current.cash_projection()
    assert projection.profit_escrow == 36
    assert projection.cash_pools["seed"] == 1000
    assert projection.cash_pools["protected"] == 500
    assert projection.total_physical_cash == 2036
    with pytest.raises(ProfitBridgeBlocked, match="outbox"):
        current.before_new_spending()
    result = current.dispatch(trade_id="sell-fill", now=NOW)
    assert result["equal_share"] == "18.00000000"
    assert current.before_new_spending().new_spending_allowed
    assert sum(current.cash_projection().cash_pools.values()) == 2036
    assert current.integration_callbacks().before_new_spending().profit_escrow == 36


def test_identical_replay_preserves_original_bytes_and_ids(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    staged = current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    original = execute(tmp_path, "SELECT * FROM next_profit_cash")
    first = current.dispatch(trade_id="sell-fill", now=NOW)
    mutate_state(tmp_path, lambda s: s.update(now=NOW+1000))
    reopened = bridge(tmp_path)
    again = reopened.stage(trade_id="sell-fill", snapshot=members(), now=NOW+1000)
    assert again["source_payload"] == staged["source_payload"]
    assert again["event_payload"] == staged["event_payload"]
    assert again["operation_id"] == staged["operation_id"]
    assert reopened.dispatch(trade_id="sell-fill", now=NOW+1000) == first
    assert execute(tmp_path, "SELECT * FROM next_profit_cash") == original
    assert allocator(tmp_path).audit()["event_count"] == 1


@pytest.mark.parametrize("sale_price", ["100", "90"])
def test_nonpositive_whole_receipt_creates_no_entitlement(tmp_path, monkeypatch, sale_price):
    current = seed(tmp_path, monkeypatch, prices=("50", "150"), sale_price=sale_price, entry_fee="0", exit_fee="0")
    result = current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert result["status"] == "INELIGIBLE" and result["event_payload"] is None
    assert current.dispatch(trade_id="sell-fill", now=NOW) is None
    assert current.cash_projection().profit_escrow == 0
    assert allocator(tmp_path).audit()["event_count"] == 0
    assert current.before_new_spending().new_spending_allowed


def test_native_event_without_cash_applied_is_not_settled(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    execute(tmp_path, "UPDATE receipts SET applied=0 WHERE id='sell-fill'")
    with pytest.raises(ProfitBridgeBlocked, match="Unapplied"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert not execute(tmp_path, "SELECT * FROM next_profit_outbox")
    assert allocator(tmp_path).audit()["event_count"] == 0


def test_nonterminal_sale_is_not_settled(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch, terminal=False)
    with pytest.raises(ProfitBridgeBlocked, match="terminal"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)


@pytest.mark.parametrize("change,match", [
    (lambda s: s["orders"]["sell"].update(pool="protected"), "Protected"),
    (lambda s: s["orders"]["sell"].update(executor="foreign"), "executor"),
    (lambda s: s["orders"]["sell"].update(position="foreign"), "binding"),
    (lambda s: s.update(now=NOW+1), "Future"),
    (lambda s: s.update(quarantine=["unresolved"]), "quarantine"),
])
def test_changed_paper_identity_scope_and_future_fail_closed(tmp_path, monkeypatch, change, match):
    current = seed(tmp_path, monkeypatch)
    mutate_state(tmp_path, change)
    with pytest.raises(ProfitBridgeBlocked, match=match):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert allocator(tmp_path).audit()["event_count"] == 0


@pytest.mark.parametrize("sql,match", [
    ("UPDATE economic_trade_fill_reconciliation SET order_id='foreign' WHERE trade_id='sell-fill'", "binding"),
    ("UPDATE economic_trade_fill_reconciliation SET price='119' WHERE trade_id='sell-fill'", "price"),
    ("UPDATE economic_trade_fill_reconciliation SET fee_paid='1' WHERE trade_id='sell-fill'", "fee_paid"),
    ("UPDATE economic_trade_fill_reconciliation SET amount_base='1' WHERE trade_id='sell-fill'", "amount_base"),
    ("UPDATE economic_trade_fill_reconciliation SET side='BUY' WHERE trade_id='sell-fill'", "identity"),
    ("UPDATE economic_trade_fill_reconciliation SET account_name='foreign' WHERE trade_id='sell-fill'", "binding"),
    ("UPDATE execution_provenance SET controller_id='foreign'", "Foreign"),
    ("UPDATE execution_provenance SET connector_name='foreign'", "Foreign"),
    ("UPDATE economic_position_events SET realized_pnl_quote='40'", "P&L"),
    ("DELETE FROM economic_exit_allocations WHERE lot_id='trade-fill:buy-1'", "aggregation|cross-lot"),
    ("UPDATE economic_exit_allocations SET proposal_id='foreign'", "owner"),
    ("UPDATE economic_trade_fill_reconciliation SET reconciled_at=1800000101 WHERE trade_id='sell-fill'", "chronology"),
])
def test_native_exact_evidence_required(tmp_path, monkeypatch, sql, match):
    current = seed(tmp_path, monkeypatch)
    execute(tmp_path, sql, native=True)
    with pytest.raises(ProfitBridgeBlocked, match=match):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert not execute(tmp_path, "SELECT * FROM next_profit_cash")


def test_buy_or_deposit_cannot_be_realized_profit(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    with pytest.raises(ProfitBridgeBlocked, match="executed sales"):
        current.stage(trade_id="buy-0", snapshot=members(), now=NOW)
    other = tmp_path / "other"
    current = seed(other, monkeypatch)
    raw = json.loads(execute(other, "SELECT payload FROM receipts WHERE id='sell-fill'")[0][0])
    raw.update(trade_type="DEPOSIT")
    execute(other, "UPDATE receipts SET payload=? WHERE id='sell-fill'", (encoded(raw),))
    with pytest.raises(ProfitBridgeBlocked):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert allocator(other).audit()["event_count"] == 0


def test_new_stale_event_and_expired_unaccepted_outbox_fail_closed(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    with pytest.raises(ProfitBridgeBlocked, match="Stale"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW+400)
    other = tmp_path / "other"
    current = seed(other, monkeypatch)
    current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    with pytest.raises(ProfitBridgeBlocked, match="[Ee]xpir|[Ss]tale"):
        current.dispatch(trade_id="sell-fill", now=NOW+61)
    assert current.cash_projection().profit_escrow == 36
    assert not current.cash_projection().new_spending_allowed
    assert allocator(other).audit()["event_count"] == 0


@pytest.mark.parametrize("point", ["before_stage_commit", "after_stage_commit", "after_allocator_commit", "before_ack_commit", "after_ack_commit"])
def test_exception_crash_recovery_at_every_transaction_boundary(tmp_path, monkeypatch, point):
    current = seed(tmp_path, monkeypatch)
    def crash(where):
        if where == point:
            raise RuntimeError("simulated process crash")
    current.fault_hook = crash
    with pytest.raises(RuntimeError, match="simulated"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
        current.dispatch(trade_id="sell-fill", now=NOW)
    reopened = bridge(tmp_path)
    if point == "before_stage_commit":
        assert execute(tmp_path, "SELECT * FROM next_profit_outbox") == []
        assert execute(tmp_path, "SELECT * FROM next_profit_cash") == []
    reopened.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    result = reopened.dispatch(trade_id="sell-fill", now=NOW)
    assert result["eligible_profit"] == "36.00000000"
    assert allocator(tmp_path).audit()["event_count"] == 1
    assert reopened.before_new_spending().profit_escrow == 36
    assert len(execute(tmp_path, "SELECT * FROM next_profit_cash")) == 1


def test_commit_before_ack_replays_original_expired_attestation(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    def crash(point):
        if point == "after_allocator_commit":
            raise RuntimeError("crash")
    current.fault_hook = crash
    with pytest.raises(RuntimeError):
        current.dispatch(trade_id="sell-fill", now=NOW)
    original = execute(tmp_path, "SELECT event_payload,operation_id FROM next_profit_outbox")
    mutate_state(tmp_path, lambda s: s.update(now=NOW+1000))
    reopened = bridge(tmp_path)
    reopened.dispatch(trade_id="sell-fill", now=NOW+1000)
    assert execute(tmp_path, "SELECT event_payload,operation_id FROM next_profit_outbox") == original
    assert allocator(tmp_path).audit()["event_count"] == 1
    assert reopened.before_new_spending().new_spending_allowed


def test_changed_membership_preserves_forensic_outbox(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    original = current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    changed = replace(members(), members=(Member("ETH", "eth-owner", "POSITION_ALLOCATION"), members().members[1]))
    with pytest.raises(ProfitBridgeBlocked, match="membership"):
        current.stage(trade_id="sell-fill", snapshot=changed, now=NOW)
    assert execute(tmp_path, "SELECT event_payload,source_payload FROM next_profit_outbox")[0] == (original["event_payload"], original["source_payload"])
    assert execute(tmp_path, "SELECT reason FROM next_profit_discrepancies")
    with pytest.raises(ProfitBridgeBlocked, match="discrepancy"):
        current.before_new_spending()


def test_changed_native_after_stage_never_dispatches(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    original = current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    execute(tmp_path, "UPDATE economic_position_events SET occurred_at=occurred_at+1", native=True)
    with pytest.raises(ProfitBridgeBlocked):
        current.dispatch(trade_id="sell-fill", now=NOW)
    assert execute(tmp_path, "SELECT source_payload FROM next_profit_outbox")[0][0] == original["source_payload"]
    assert allocator(tmp_path).audit()["event_count"] == 0


def test_unbalanced_journal_and_backing_deficit_block_spending(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    current.dispatch(trade_id="sell-fill", now=NOW)
    execute(tmp_path, "UPDATE next_profit_cash SET source_debit='35'")
    with pytest.raises(ProfitBridgeBlocked, match="Unbalanced"):
        current.before_new_spending()
    execute(tmp_path, "UPDATE next_profit_cash SET source_debit='36'")
    def add_hold(state):
        state["orders"]["competing"] = dict(id="competing", side="BUY", status="OPEN", pool="seed", target="1001")
    mutate_state(tmp_path, add_hold)
    with pytest.raises(ProfitBridgeBlocked, match="deficit"):
        current.before_new_spending()


def test_partial_disposal_and_ambient_decimal_precision(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch, sale_amount="1", exit_fee="1")
    with localcontext() as ctx:
        ctx.prec = 6
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
        result = current.dispatch(trade_id="sell-fill", now=NOW)
    assert result["eligible_profit"] == "18.00000000"
    assert current.before_new_spending().profit_escrow == 18


@pytest.mark.parametrize("point", ["before_stage_commit", "after_stage_commit", "after_allocator_commit", "before_ack_commit", "after_ack_commit",
                                   "allocator:before_write", "allocator:after_write_before_commit", "allocator:after_commit"])
def test_actual_process_exit_recovers_exactly_once(tmp_path, monkeypatch, point):
    seed(tmp_path, monkeypatch)
    code = '''
import os, sys
from pathlib import Path
from decimal import Decimal as D
from spectra_core.next_entry_allocator import Allocator, Member, MembershipSnapshot, Policy
from spectra_core.next_entry_profit_bridge import NativeSettledProfitBridge
root, point = Path(sys.argv[1]), sys.argv[2]
now = 1800000100
def die(where):
    if where == point:
        os._exit(79)
def allocator_die(where):
    die("allocator:" + where)
a = Allocator(root/"allocator.sqlite", account_id="paper-account", currency="USD",
    policy=Policy(D("0.00000001"),D("0.01"),300,60), fault_hook=allocator_die)
b = NativeSettledProfitBridge(paper_db=root/"paper.sqlite", native_db=root/"native.sqlite", allocator=a,
    account_id="paper-account", connector="isolated-paper", owner_id="spectra-paper", enabled=True, fault_hook=die)
s = MembershipSnapshot("members-v1", "paper-account", "USD", now-1000, now+1000,
    (Member("BTC","btc-owner","POSITION_ALLOCATION"), Member("VCAP","vcap-owner","VCAP")))
b.stage(trade_id="sell-fill", snapshot=s, now=now)
b.dispatch(trade_id="sell-fill", now=now)
'''
    process = subprocess.run([sys.executable, "-c", code, str(tmp_path), point], capture_output=True, text=True)
    assert process.returncode == 79, process.stderr
    reopened = bridge(tmp_path)
    reopened.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    reopened.dispatch(trade_id="sell-fill", now=NOW)
    assert reopened.before_new_spending().profit_escrow == 36
    assert allocator(tmp_path).audit()["event_count"] == 1
    assert len(execute(tmp_path, "SELECT * FROM next_profit_cash")) == 1
    assert not execute(tmp_path, "SELECT * FROM next_profit_discrepancies")


def test_unstaged_settled_sale_blocks_new_base_spending(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    assert current.cash_projection().pending_trade_ids == ("sell-fill",)
    assert not current.cash_projection().new_spending_allowed
    with pytest.raises(ProfitBridgeBlocked, match="outbox"):
        current.before_new_spending()


def test_changed_original_basis_is_rejected_even_with_matching_event(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    execute(tmp_path, "UPDATE economic_lots SET cost_basis_original_quote='1' WHERE lot_id='trade-fill:buy-0'", native=True)
    with pytest.raises(ProfitBridgeBlocked, match="acquisition cost/fees|lot conservation"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)


def test_duplicate_receipt_with_new_trade_id_cannot_reuse_native_realization(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    current.dispatch(trade_id="sell-fill", now=NOW)
    original = execute(tmp_path, "SELECT payload FROM receipts WHERE id='sell-fill'")[0][0]
    raw = json.loads(original)
    raw["trade_id"] = "duplicate-sale"
    execute(tmp_path, "INSERT INTO receipts VALUES ('duplicate-sale','sell',?,1)", (encoded(raw),))
    with pytest.raises(ProfitBridgeBlocked):
        current.stage(trade_id="duplicate-sale", snapshot=members(), now=NOW)
    assert allocator(tmp_path).audit()["event_count"] == 1
    with pytest.raises(ProfitBridgeBlocked):
        current.before_new_spending()


def test_receipt_byte_change_is_forensically_rejected(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    original = current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    raw = json.loads(execute(tmp_path, "SELECT payload FROM receipts WHERE id='sell-fill'")[0][0])
    execute(tmp_path, "UPDATE receipts SET payload=? WHERE id='sell-fill'", (json.dumps(raw, indent=2),))
    with pytest.raises(ProfitBridgeBlocked, match="changed"):
        current.dispatch(trade_id="sell-fill", now=NOW)
    assert execute(tmp_path, "SELECT source_payload FROM next_profit_outbox")[0][0] == original["source_payload"]


def test_allocator_reset_or_foreign_grant_blocks_cash_projection(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    current.dispatch(trade_id="sell-fill", now=NOW)
    # Deleting an entire allocator authority is a simulated operator/storage fault.
    # It must not make the cash reclassification silently spendable again.
    with sqlite3.connect(tmp_path / "allocator.sqlite") as db:
        db.execute("UPDATE state SET body=?", (encoded(Allocator._empty_state()),))
        db.execute("DELETE FROM operations")
    with pytest.raises(ProfitBridgeBlocked, match="coverage"):
        current.before_new_spending()


@pytest.mark.parametrize("mutation,match,native", [
    ("DELETE FROM receipts WHERE id='buy-0'", "Missing receipts|coverage", False),
    ("UPDATE economic_lots SET created_at=1800000200 WHERE lot_id='trade-fill:buy-0'", "chronology|complete owner inventory", True),
    ("UPDATE economic_trade_fill_reconciliation SET fee_paid='0' WHERE trade_id='buy-0'", "acquisition fill", True),
    ("UPDATE economic_lots SET entry_fees_original_quote='0' WHERE lot_id='trade-fill:buy-0'", "acquisition cost/fees", True),
])
def test_original_acquisition_cash_and_chronology_are_required(tmp_path, monkeypatch, mutation, match, native):
    current = seed(tmp_path, monkeypatch)
    execute(tmp_path, mutation, native=native)
    with pytest.raises(ProfitBridgeBlocked, match=match):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert allocator(tmp_path).audit()["event_count"] == 0
    assert not execute(tmp_path, "SELECT * FROM next_profit_cash")


def test_sale_cannot_reassign_acquisition_source_pool(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    mutate_state(tmp_path, lambda state: state["orders"]["sell"].update(pool="repair"))
    with pytest.raises(ProfitBridgeBlocked, match="source pool/owner"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert allocator(tmp_path).audit()["event_count"] == 0


def test_missing_buy_after_grant_disables_cash_projection(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    current.dispatch(trade_id="sell-fill", now=NOW)
    execute(tmp_path, "DELETE FROM receipts WHERE id='buy-0'")
    with pytest.raises(ProfitBridgeBlocked, match="coverage"):
        current.cash_projection()
    with pytest.raises(ProfitBridgeBlocked):
        current.before_new_spending()


@pytest.mark.parametrize("pool", ["missing-owner", "protected", ESCROW_POOL])
def test_pending_hold_cannot_bypass_zero_balance_or_protected_pool(tmp_path, monkeypatch, pool):
    current = seed(tmp_path, monkeypatch)
    current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    current.dispatch(trade_id="sell-fill", now=NOW)
    def add_hold(state):
        state["orders"]["competing"] = dict(id="competing", side="BUY", status="OPEN", pool=pool, target="1000")
    mutate_state(tmp_path, add_hold)
    with pytest.raises(ProfitBridgeBlocked, match="deficit|pending order"):
        current.before_new_spending()


def test_explicit_native_accounting_conflict_blocks_bridge_gate(tmp_path, monkeypatch):
    from spectra_core.accounting_health import AccountingHealthSupervisor
    current = seed(tmp_path, monkeypatch)
    current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    current.dispatch(trade_id="sell-fill", now=NOW)
    AccountingHealthSupervisor(tmp_path / "native.sqlite").record_conflict(
        position_id="position", now=NOW, reason="unresolved_native_conflict")
    with pytest.raises(ProfitBridgeBlocked, match="Native accounting conflict"):
        current.before_new_spending()


def test_negative_or_orphan_historical_allocation_cannot_offset_basis(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    execute(tmp_path, "INSERT INTO economic_exit_allocations VALUES ('orphan','trade-fill:buy-0','position','proposal','executor','ACTIVE_TRADE','0','-1','0')", native=True)
    with pytest.raises(ProfitBridgeBlocked, match="historical disposal"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert allocator(tmp_path).audit()["event_count"] == 0


def test_partial_disposal_cannot_shift_principal_into_unsold_remainder(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch, sale_amount="1", exit_fee="1")
    execute(tmp_path, "UPDATE economic_exit_allocations SET allocated_cost_basis_quote='0' WHERE lot_id='trade-fill:buy-0'", native=True)
    execute(tmp_path, "UPDATE economic_position_events SET allocated_cost_basis_quote='60',realized_pnl_quote='58'", native=True)
    execute(tmp_path, "UPDATE economic_lots SET cost_basis_remaining_quote='80' WHERE lot_id='trade-fill:buy-0'", native=True)
    with pytest.raises(ProfitBridgeBlocked, match="proportional disposal"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert allocator(tmp_path).audit()["event_count"] == 0


def test_partial_sale_cannot_cherry_pick_profitable_lot(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch, sale_amount="1", exit_fee="1")
    execute(tmp_path, "DELETE FROM economic_exit_allocations WHERE lot_id='trade-fill:buy-1'", native=True)
    execute(tmp_path, "UPDATE economic_exit_allocations SET base_sold='1',allocated_cost_basis_quote='80',allocated_entry_fees_quote='1' WHERE lot_id='trade-fill:buy-0'", native=True)
    execute(tmp_path, "UPDATE economic_lots SET base_remaining='0',cost_basis_remaining_quote='0',entry_fees_remaining_quote='0' WHERE lot_id='trade-fill:buy-0'", native=True)
    execute(tmp_path, "UPDATE economic_lots SET base_remaining='1',cost_basis_remaining_quote='120',entry_fees_remaining_quote='1' WHERE lot_id='trade-fill:buy-1'", native=True)
    execute(tmp_path, "UPDATE economic_position_events SET allocated_cost_basis_quote='80',allocated_entry_fees_quote='1',realized_pnl_quote='38'", native=True)
    with pytest.raises(ProfitBridgeBlocked, match="cross-lot proportional"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert allocator(tmp_path).audit()["event_count"] == 0


@pytest.mark.parametrize("hide", ["created_at=1800000101", "proposal_id='foreign',executor_id='foreign'"])
def test_undisposed_losing_lot_cannot_be_hidden_from_candidate_scope(tmp_path, monkeypatch, hide):
    current = seed(tmp_path, monkeypatch, sale_amount="1", exit_fee="1")
    execute(tmp_path, "DELETE FROM economic_exit_allocations WHERE lot_id='trade-fill:buy-1'", native=True)
    execute(tmp_path, "UPDATE economic_exit_allocations SET base_sold='1',allocated_cost_basis_quote='80',allocated_entry_fees_quote='1' WHERE lot_id='trade-fill:buy-0'", native=True)
    execute(tmp_path, "UPDATE economic_lots SET base_remaining='0',cost_basis_remaining_quote='0',entry_fees_remaining_quote='0' WHERE lot_id='trade-fill:buy-0'", native=True)
    execute(tmp_path, "UPDATE economic_lots SET base_remaining='1',cost_basis_remaining_quote='120',entry_fees_remaining_quote='1'," + hide + " WHERE lot_id='trade-fill:buy-1'", native=True)
    execute(tmp_path, "UPDATE economic_position_events SET allocated_cost_basis_quote='80',allocated_entry_fees_quote='1',realized_pnl_quote='38'", native=True)
    with pytest.raises(ProfitBridgeBlocked, match="Complete acquisition"):
        current.stage(trade_id="sell-fill", snapshot=members(), now=NOW)
    assert allocator(tmp_path).audit()["event_count"] == 0


def test_default_extension_hooks_are_fail_closed_and_identity_is_bound(tmp_path, monkeypatch):
    current = seed(tmp_path, monkeypatch)
    assert current._extension_identity() == {"implementation": "grant-only-v1"}
    with pytest.raises(ProfitBridgeBlocked, match="separately reviewed"):
        current._validate_funding_allocator(None, {"reservations": {"foreign": {}}, "commits": {}})
    with pytest.raises(ProfitBridgeBlocked, match="source pool"):
        current._source_pool_proof({}, {"pool": "repair"}, {"pool": "seed"}, "trade")
    assert current._funding_projection(None, {}, {"seed": D(1)}, D(2)) == ({"seed": D(1)}, D(2))
    assert current._pending_funding(None, {}) == ()
    class UnreviewedVersion(NativeSettledProfitBridge):
        def _extension_identity(self):
            return {"implementation": "changed"}
    with pytest.raises(ProfitBridgeBlocked, match="identity changed"):
        UnreviewedVersion(paper_db=tmp_path / "paper.sqlite", native_db=tmp_path / "native.sqlite",
            allocator=allocator(tmp_path), account_id=ACCOUNT, connector=CONNECTOR, owner_id=OWNER, enabled=True)
