"""Synthetic paper fixtures backed by actual native reservation/fill/health calls."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3
import types

import pytest

import spectra_core.capital_governor as governor_module
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.economic_position import EconomicPositionLedger
from spectra_core.executor_provenance_bridge import ExecutorLedgerSyncReport
from spectra_core.fill_reconciliation import EconomicFillRole, LiveFillLedgerReconciler
from spectra_core.models import CapitalPolicy, CapitalProposal, CapitalSnapshot, ExecutorAccountingObservation, IntentKind
from spectra_core.next_entry_profit_bridge import ProfitBridgeBlocked
from spectra_core.next_entry_settlement_proof import (
    EntrySettlementContext, canonical_json, fingerprint, identity,
    verify_entry_receipts, verify_terminal_closure,
)

D = Decimal
NOW = 1_800_000_100
ACCOUNT = "paper-account"
CONNECTOR = "isolated-paper"
OWNER = "spectra-paper"


def execute(root, sql, args=(), native=False):
    with sqlite3.connect(root / ("native.sqlite" if native else "paper.sqlite")) as db:
        return db.execute(sql, args).fetchall()


def mutate_state(root, function):
    with sqlite3.connect(root / "paper.sqlite") as db:
        state = json.loads(db.execute("SELECT value FROM state WHERE id=1").fetchone()[0])
        function(state)
        db.execute("UPDATE state SET value=? WHERE id=1", (canonical_json(state),))


def mutate_raw(root, **values):
    payload = json.loads(execute(root, "SELECT payload FROM receipts WHERE id='z-first'")[0][0])
    payload.update(values)
    execute(root, "UPDATE receipts SET payload=? WHERE id='z-first'", (canonical_json(payload),))


def seed(root, monkeypatch, *, fills=True, terminal=None, health=True, funding_owner="seed", execution=True, count=2, fees=(".11", ".17")):
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(governor_module, "time", types.SimpleNamespace(time=lambda: NOW - 20))
    gov = CapitalGovernor(root / "native.sqlite", CapitalPolicy(D(0), D(10000), D(10000), D(10000), D(5000)))
    ledger = EconomicPositionLedger(root / "native.sqlite")
    ledger.open_position(position_id="position", pair="BTC-USD", created_at=NOW-30)
    intent = IntentKind.ENTRY if funding_owner == "seed" else IntentKind.RECOVERY
    proposal = CapitalProposal("proposal", "BTC-USD", intent, D(110))
    gov.reserve(proposal, CapitalSnapshot(D(10000)))
    assert gov.commit("proposal")
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=gov)
    if execution:
        assert gov.reconcile_execution(proposal_id="proposal", executor_id="executor", account_name=ACCOUNT,
            connector_name=CONNECTOR, controller_id=OWNER)
        reconciler.bind_executor_order(order_id="buy", position_id="position", proposal_id="proposal",
            role=EconomicFillRole.BASE_ENTRY if funding_owner == "seed" else EconomicFillRole.VCAP_RECOVERY,
            account_name=ACCOUNT, connector_name=CONNECTOR, pair="BTC-USD", created_at=NOW-19)
    context = EntrySettlementContext(account_id=ACCOUNT, connector=CONNECTOR, controller_id=OWNER,
        entry_id="entry", order_id="buy", pair="BTC-USD", position_id="position", proposal_id="proposal",
        executor_id="executor", native_reservation_id="proposal", entry_pool="next_entry:entry",
        base_budget=D(80), profit_budget=D(40), authorized_quantity=D(1), reference_price=D(100),
        reserved_at=NOW-19, funding_owner=funding_owner, snapshot_id="snapshot", allocation_id="allocation",
        owner_id="allocation-owner", allocation_role="POSITION_ALLOCATION", source_pool=funding_owner)
    raws = []
    if fills:
        for trade, quantity, price, fee, at in (("z-first", ".4", "100", fees[0], NOW-15),
                                               ("a-second", ".6", "90", fees[1], NOW-10))[:count]:
            raw = dict(trade_id=trade, order_id="buy", account_name=ACCOUNT, connector_name=CONNECTOR,
                trading_pair="BTC-USD", trade_type="BUY", amount=quantity, price=price, fee_paid=fee,
                fee_currency="USD", timestamp=datetime.fromtimestamp(at, timezone.utc).isoformat())
            reconciler.reconcile_trade_fill(raw, reconciled_at=at+1)
            raws.append(raw)
    order = dict(id="buy", entry_id="entry", side="BUY", pair="BTC-USD", position="position",
        proposal="proposal", executor="executor", pool="next_entry:entry", source_pool=funding_owner,
        funding_owner=funding_owner, intent=intent.value, target="120", principal="110", quantity="1",
        reference="100", decision_at=NOW-20, created_at=NOW-20, status=terminal or ("PARTIAL" if fills else "OPEN"),
        terminal=terminal, execution_frozen=True, base_budget="80", profit_budget="40", snapshot_id="snapshot",
        allocation_id="allocation", allocation_owner_id="allocation-owner", allocation_role="POSITION_ALLOCATION")
    state = dict(identity=dict(schema=1, config=dict(account=ACCOUNT, connector=CONNECTOR, owner=OWNER)),
        now=NOW, orders={"buy": order}, breaker="NORMAL", quarantine=[])
    with sqlite3.connect(root / "paper.sqlite") as db:
        db.executescript('''CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE receipts (id TEXT PRIMARY KEY, order_id TEXT NOT NULL, payload TEXT NOT NULL, applied INTEGER NOT NULL);
            CREATE TABLE audit_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);''')
        db.execute("INSERT INTO state VALUES(1,?)", (canonical_json(state),))
        for raw in raws:
            db.execute("INSERT INTO receipts VALUES(?,?,?,1)", (raw["trade_id"], "buy", canonical_json(raw)))
            db.execute("INSERT INTO audit_events(at,kind,payload) VALUES(?,'receipt_reconciled',?)",
                       (NOW-8, canonical_json({"trade_id": raw["trade_id"]})))
        if terminal:
            db.execute("INSERT INTO audit_events(at,kind,payload) VALUES(?,'terminal_reconciliation',?)",
                       (NOW-5, canonical_json({"order": "buy", "status": terminal})))
    if health:
        supervisor = AccountingHealthSupervisor(root / "native.sqlite")
        report = ExecutorLedgerSyncReport("proposal", "executor", "position", 1, 1, len(raws), len(raws),
            len(raws), 0, (), (), True, ("synthetic_exact_native_sync",))
        supervisor.record_reports(position_id="position", reports=(report,), now=NOW-3)
    return context


def receipts(root, context, now=NOW):
    with sqlite3.connect(root / "paper.sqlite") as paper:
        return verify_entry_receipts(paper, root / "native.sqlite", context, now)


def closure(root, context, now=NOW, minimum=None):
    with sqlite3.connect(root / "paper.sqlite") as paper:
        return verify_terminal_closure(paper, root / "native.sqlite", context, now,
                                      minimum_verified_after=minimum)


def test_native_buy_exact_all_fees_and_insertion_order(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch)
    batch = receipts(tmp_path, context)
    assert batch.settlement_ids == ("z-first", "a-second")
    assert batch.total_quantity == 1
    assert batch.total_fee_inclusive_debit == D("94.28")
    assert batch.latest_reconciled_at == NOW-9
    assert batch.source_digest == fingerprint(batch.source_payload)
    first = json.loads(batch.receipts[0])
    assert first["original_lot"]["cost_basis_original_quote"] == "40.0"
    assert D(first["original_lot"]["entry_fees_original_quote"]) == D(".11")
    assert first["paper_receipt"]["payload"] == execute(tmp_path, "SELECT payload FROM receipts WHERE id='z-first'")[0][0]
    with pytest.raises(FrozenInstanceError):
        batch.source_payload = "rewritten"
    detached = batch.as_dict()
    detached["receipts"][0]["original_lot"]["base_original"] = "999"
    assert receipts(tmp_path, context).source_payload == batch.source_payload
    with localcontext() as ctx:
        ctx.prec = 3
        assert receipts(tmp_path, context).total_fee_inclusive_debit == D("94.28")


def test_receipt_evidence_stable_across_later_now_and_mutable_inventory(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch)
    batch = receipts(tmp_path, context)
    execute(tmp_path, "UPDATE economic_lots SET base_remaining='0',cost_basis_remaining_quote='0',entry_fees_remaining_quote='0'", native=True)
    mutate_state(tmp_path, lambda state: state.update(now=NOW+100))
    assert receipts(tmp_path, context, NOW+100).source_payload == batch.source_payload
    assert batch.latest_reconciled_at == NOW-9


def test_empty_batch_requires_no_fabricated_execution(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, fills=False, health=False, execution=False)
    batch = receipts(tmp_path, context)
    assert batch.settlement_ids == () and batch.receipts == ()
    assert batch.total_fee_inclusive_debit == 0 and batch.latest_reconciled_at is None
    assert execute(tmp_path, "SELECT * FROM execution_receipts", native=True) == []


@pytest.mark.parametrize("funding_owner", ["seed", "repair"])
def test_real_native_terminal_health_proof(tmp_path, monkeypatch, funding_owner):
    context = seed(tmp_path, monkeypatch, terminal="FILLED", funding_owner=funding_owner)
    result = closure(tmp_path, context)
    assert result.terminal_at == NOW-5 and result.verified_at == NOW-3
    assert result.batch.settlement_ids == ("z-first", "a-second")
    assert result.source_digest == fingerprint(result.source_payload)
    assert result.as_dict()["native_health"]["last_success_at"] == NOW-3


def test_no_fill_canceled_terminal(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, terminal="CANCELED", fills=False)
    result = closure(tmp_path, context)
    assert result.batch.total_fee_inclusive_debit == 0


def test_release_needs_actual_strictly_newer_health_observation(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, terminal="CANCELED")
    assert closure(tmp_path, context, minimum=NOW-4).verified_at == NOW-3
    with pytest.raises(ProfitBridgeBlocked, match="genuinely later"):
        closure(tmp_path, context, minimum=NOW-3)
    mutate_state(tmp_path, lambda state: state.update(now=NOW+1))
    with pytest.raises(ProfitBridgeBlocked, match="genuinely later"):
        closure(tmp_path, context, NOW+1, minimum=NOW-3)
    supervisor = AccountingHealthSupervisor(tmp_path / "native.sqlite")
    report = ExecutorLedgerSyncReport("proposal", "executor", "position", 1, 1, 2, 2, 0, 2, (), (), True, ())
    supervisor.record_reports(position_id="position", reports=(report,), now=NOW+1)
    assert closure(tmp_path, context, NOW+1, minimum=NOW-3).verified_at == NOW+1


@pytest.mark.parametrize("field,value", [("entry_id", "foreign"), ("id", "foreign"), ("side", "SELL"),
    ("pair", "ETH-USD"), ("position", "foreign"), ("proposal", "foreign"), ("executor", "foreign"),
    ("pool", "seed"), ("source_pool", "foreign"), ("funding_owner", "repair"), ("intent", "RECOVERY"), ("base_budget", "79"),
    ("profit_budget", "41"), ("snapshot_id", "foreign"), ("allocation_id", "foreign"),
    ("allocation_owner_id", "foreign"), ("allocation_role", "VCAP"), ("quantity", "1.1"),
    ("target", "121"), ("reference", "101"), ("principal", "99"), ("execution_frozen", False)])
def test_paper_order_scope_changes_fail_closed(tmp_path, monkeypatch, field, value):
    context = seed(tmp_path, monkeypatch)
    mutate_state(tmp_path, lambda state: state["orders"]["buy"].update({field: value}))
    with pytest.raises(ProfitBridgeBlocked):
        receipts(tmp_path, context)


@pytest.mark.parametrize("sql", [
    "UPDATE capital_reservations SET status='RESERVED'",
    "UPDATE capital_reservations SET amount_quote='111'",
    "UPDATE capital_reservations SET leverage='2'",
    "UPDATE capital_reservations SET pair='ETH-USD'",
    "UPDATE execution_provenance SET controller_id='foreign'",
    "UPDATE execution_provenance SET authorized_quote='111'",
    "UPDATE execution_provenance SET executor_id='foreign'",
    "UPDATE execution_provenance SET intent='RECOVERY'",
    "DELETE FROM execution_receipts",
    "UPDATE execution_receipts SET executor_id='foreign'",
    "DELETE FROM economic_fill_bindings",
    "UPDATE economic_fill_bindings SET proposal_id='foreign'",
    "UPDATE economic_fill_bindings SET account_name='foreign'",
    "UPDATE economic_fill_bindings SET connector_name='foreign'",
    "UPDATE economic_fill_bindings SET executor_id='foreign'",
    "UPDATE economic_fill_bindings SET role='POSITION_EXIT'",
    "DELETE FROM economic_trade_fill_reconciliation WHERE trade_id='z-first'",
    "UPDATE economic_trade_fill_reconciliation SET order_id='foreign' WHERE trade_id='z-first'",
    "UPDATE economic_trade_fill_reconciliation SET fee_paid='0' WHERE trade_id='z-first'",
    "UPDATE economic_trade_fill_reconciliation SET price='99' WHERE trade_id='z-first'",
    "UPDATE economic_trade_fill_reconciliation SET fee_currency='BTC' WHERE trade_id='z-first'",
    "UPDATE economic_trade_fill_reconciliation SET amount_base='.3' WHERE trade_id='z-first'",
    "UPDATE economic_trade_fill_reconciliation SET result_id='foreign' WHERE trade_id='z-first'",
    "UPDATE economic_trade_fill_reconciliation SET reconciled_at=1800000101",
    "UPDATE economic_lots SET base_original='0.1' WHERE lot_id='trade-fill:z-first'",
    "UPDATE economic_lots SET cost_basis_original_quote='39' WHERE lot_id='trade-fill:z-first'",
    "UPDATE economic_lots SET entry_fees_original_quote='0' WHERE lot_id='trade-fill:z-first'",
    "UPDATE economic_lots SET ownership='UNATTRIBUTED' WHERE lot_id='trade-fill:z-first'",
    "UPDATE economic_lots SET proposal_id='foreign' WHERE lot_id='trade-fill:z-first'",
    "UPDATE economic_lots SET executor_id='foreign' WHERE lot_id='trade-fill:z-first'",
    "UPDATE economic_lots SET source='VCAP_RECOVERY' WHERE lot_id='trade-fill:z-first'",
])
def test_exact_native_evidence_required(tmp_path, monkeypatch, sql):
    context = seed(tmp_path, monkeypatch)
    execute(tmp_path, sql, native=True)
    with pytest.raises(ProfitBridgeBlocked):
        receipts(tmp_path, context)


@pytest.mark.parametrize("raw", [{"amount": 0.4}, {"price": 100.0}, {"fee_paid": .11}, {"fee_currency": "BTC"},
    {"fee_currency": "EUR"}, {"account_name": "foreign"}, {"connector_name": "foreign"},
    {"trading_pair": "ETH-USD"}, {"trade_type": "SELL"}, {"trade_id": "foreign"},
    {"order_id": "foreign"}, {"price": "101"}, {"timestamp": "2027-01-16T08:01:41+00:00"}])
def test_exact_paper_receipt_required(tmp_path, monkeypatch, raw):
    context = seed(tmp_path, monkeypatch)
    mutate_raw(tmp_path, **raw)
    with pytest.raises(ProfitBridgeBlocked):
        receipts(tmp_path, context)


@pytest.mark.parametrize("sql", ["UPDATE receipts SET applied=0 WHERE id='z-first'",
    "UPDATE receipts SET order_id='foreign' WHERE id='z-first'", "DELETE FROM receipts WHERE id='z-first'"])
def test_unapplied_or_hidden_receipts_fail_closed(tmp_path, monkeypatch, sql):
    context = seed(tmp_path, monkeypatch)
    execute(tmp_path, sql)
    with pytest.raises(ProfitBridgeBlocked):
        receipts(tmp_path, context)


@pytest.mark.parametrize("sql", ["DROP TABLE economic_accounting_health", "DELETE FROM economic_accounting_health",
    "UPDATE economic_accounting_health SET recorded_state='HOLD'",
    "UPDATE economic_accounting_health SET last_success_at=NULL",
    "UPDATE economic_accounting_health SET last_success_at=1800000000,last_attempt_at=1800000000",
    "UPDATE economic_accounting_health SET last_success_at=1800000101,last_attempt_at=1800000101",
    "UPDATE economic_accounting_health SET known_orders=2",
    "UPDATE economic_accounting_health SET bound_orders=0",
    "UPDATE economic_accounting_health SET confirmed_fills=1",
    "UPDATE economic_accounting_health SET ledgered_fills=1",
    "UPDATE economic_accounting_health SET proposal_count=2",
    "UPDATE economic_accounting_health SET fully_reconciled_proposals=0",
    "UPDATE economic_accounting_health SET unresolved_order_ids_json='[\"buy\"]'",
    "UPDATE economic_accounting_health SET unresolved_trade_ids_json='[\"z-first\"]'"])
def test_terminal_requires_real_current_complete_health(tmp_path, monkeypatch, sql):
    context = seed(tmp_path, monkeypatch, terminal="FILLED")
    execute(tmp_path, sql, native=True)
    with pytest.raises(ProfitBridgeBlocked):
        closure(tmp_path, context)


def test_staleness_uses_actual_now_not_stale_paper_clock(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, terminal="FILLED")
    with pytest.raises(ProfitBridgeBlocked, match="stale"):
        closure(tmp_path, context, NOW+31)


@pytest.mark.parametrize("sql", ["DELETE FROM audit_events WHERE kind='terminal_reconciliation'",
    "UPDATE audit_events SET at=1800000101 WHERE kind='terminal_reconciliation'",
    "UPDATE audit_events SET at=1800000086 WHERE kind='terminal_reconciliation'",
    "UPDATE audit_events SET payload='{\"order\":\"buy\",\"status\":\"CANCELED\"}' WHERE kind='terminal_reconciliation'",
    "INSERT INTO audit_events(at,kind,payload) SELECT at,kind,payload FROM audit_events WHERE kind='terminal_reconciliation'",
    "INSERT INTO audit_events(at,kind,payload) VALUES(1800000098,'receipt_reconciled','{\"trade_id\":\"z-first\"}')"])
def test_terminal_audit_causality_and_late_receipts(tmp_path, monkeypatch, sql):
    context = seed(tmp_path, monkeypatch, terminal="FILLED")
    execute(tmp_path, sql)
    with pytest.raises(ProfitBridgeBlocked):
        closure(tmp_path, context)


def test_pending_order_cannot_release(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch)
    with pytest.raises(ProfitBridgeBlocked, match="liability"):
        closure(tmp_path, context)


def test_filled_requires_exact_authorized_quantity(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, fills=False, terminal="FILLED")
    with pytest.raises(ProfitBridgeBlocked, match="quantity"):
        closure(tmp_path, context)


def test_read_only_no_database_changes_or_creation(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, terminal="FILLED")
    def dump(name):
        with sqlite3.connect(tmp_path / name) as db:
            return tuple(db.iterdump())
    before = dump("paper.sqlite"), dump("native.sqlite")
    receipts(tmp_path, context)
    closure(tmp_path, context)
    assert before == (dump("paper.sqlite"), dump("native.sqlite"))
    missing = tmp_path / "missing.sqlite"
    with sqlite3.connect(tmp_path / "paper.sqlite") as paper, pytest.raises(ProfitBridgeBlocked):
        verify_entry_receipts(paper, missing, context, NOW)
    assert not missing.exists()


def test_context_frozen_exact_and_identity_pins(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch)
    with pytest.raises(FrozenInstanceError):
        context.account_id = "foreign"
    for change in ({"base_budget": 80.0}, {"profit_budget": D("NaN")}, {"native_reservation_id": "other"},
                   {"funding_owner": "unknown"}, {"pair": "BTC-EUR"}, {"reserved_at": True}):
        with pytest.raises(ProfitBridgeBlocked):
            replace(context, **change)
    pins = identity()
    assert pins["sources"]["next_entry_profit_bridge.py"] == "9e675449f12d771c8825af08ecc591e513e4b5f10ebc3fa892f285e724aefc6a"
    assert len(pins["sources"]["next_entry_settlement_proof.py"]) == 64
    assert context.source_digest == fingerprint(context.source_payload)


def test_actual_partial_cancellation_preserves_fee_net_debit(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, count=1, terminal="CANCELED")
    result = closure(tmp_path, context)
    assert result.batch.total_quantity == D(".4")
    assert result.batch.total_fee_inclusive_debit == D("40.11")
    assert result.batch.settlement_ids == ("z-first",)


def test_quantity_overfill_cannot_be_hidden_by_larger_native_budget(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch)
    context = replace(context, authorized_quantity=D(".9"))
    mutate_state(tmp_path, lambda state: state["orders"]["buy"].update(quantity=".9"))
    with pytest.raises(ProfitBridgeBlocked, match="overfill"):
        receipts(tmp_path, context)


def test_actual_quote_fees_must_fit_full_budget_even_when_principal_fits(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, fees=("13.11", "13.17"))
    with pytest.raises(ProfitBridgeBlocked, match="overbudget"):
        receipts(tmp_path, context)


def test_high_precision_native_fees_are_not_rounded_by_caller_context(tmp_path, monkeypatch):
    tiny = "0.0000000000000000000000000000000000000000000000000000000000001"
    context = seed(tmp_path, monkeypatch, fees=(tiny, tiny))
    with localcontext() as ctx:
        ctx.prec = 512
        expected = D(94) + D(tiny) * 2
    with localcontext() as ctx:
        ctx.prec = 2
        batch = receipts(tmp_path, context)
    assert batch.total_fee_inclusive_debit == expected
    assert json.loads(batch.source_payload)["total_fee_inclusive_debit"] == str(expected)


def test_new_receipt_appends_without_rewriting_original_proof_bytes(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, count=1)
    first = receipts(tmp_path, context)
    governor = CapitalGovernor(tmp_path / "native.sqlite", CapitalPolicy(D(0), D(10000), D(10000), D(10000), D(5000)))
    reconciler = LiveFillLedgerReconciler(ledger=EconomicPositionLedger(tmp_path / "native.sqlite"), governor=governor)
    raw = dict(trade_id="a-second", order_id="buy", account_name=ACCOUNT, connector_name=CONNECTOR,
        trading_pair="BTC-USD", trade_type="BUY", amount=".6", price="90", fee_paid=".17",
        fee_currency="USD", timestamp=datetime.fromtimestamp(NOW-10, timezone.utc).isoformat())
    reconciler.reconcile_trade_fill(raw, reconciled_at=NOW-9)
    execute(tmp_path, "INSERT INTO receipts VALUES(?,?,?,1)", ("a-second", "buy", canonical_json(raw)))
    execute(tmp_path, "INSERT INTO audit_events(at,kind,payload) VALUES(?,'receipt_reconciled',?)",
            (NOW-8, canonical_json({"trade_id": "a-second"})))
    after = receipts(tmp_path, context)
    assert after.receipts[0] == first.receipts[0]
    assert after.total_fee_inclusive_debit == D("94.28")
    assert first.latest_reconciled_at == NOW-14 and after.latest_reconciled_at == NOW-9


def test_orphan_native_lot_without_receipt_blocks_empty_and_filled_batch(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch)
    execute(tmp_path, "DELETE FROM receipts")
    execute(tmp_path, "DELETE FROM economic_trade_fill_reconciliation", native=True)
    with pytest.raises(ProfitBridgeBlocked, match="no exact order receipt"):
        receipts(tmp_path, context)


def test_missing_application_audit_prevents_terminal_proof(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, terminal="CANCELED")
    execute(tmp_path, "DELETE FROM audit_events WHERE kind='receipt_reconciled'")
    with pytest.raises(ProfitBridgeBlocked, match="Missing applied receipt audit"):
        closure(tmp_path, context)
    with pytest.raises(ProfitBridgeBlocked):
        receipts(tmp_path, context)


def test_consistent_application_audit_replay_before_closure_is_allowed(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, terminal="CANCELED")
    execute(tmp_path, "UPDATE audit_events SET seq=seq+10 WHERE kind='terminal_reconciliation'")
    execute(tmp_path, "INSERT INTO audit_events(seq,at,kind,payload) VALUES(3,?,'receipt_reconciled',?)",
            (NOW-7, canonical_json({"trade_id": "z-first"})))
    result = closure(tmp_path, context)
    assert len(result.as_dict()["receipt_audits"]) == 3


@pytest.mark.parametrize("sql", [
    "UPDATE capital_reservations SET updated_at=1800000101",
    "UPDATE execution_provenance SET created_at=1800000085",
    "UPDATE economic_fill_bindings SET created_at=1800000079",
    "UPDATE economic_lots SET created_at=1800000086 WHERE lot_id='trade-fill:z-first'",
])
def test_native_causal_boundary_tampering_blocks(tmp_path, monkeypatch, sql):
    context = seed(tmp_path, monkeypatch)
    execute(tmp_path, sql, native=True)
    with pytest.raises(ProfitBridgeBlocked):
        receipts(tmp_path, context)


@pytest.mark.parametrize("payload", ['[]', '{"order_id":"buy","order_id":"other"}', '{"order_id":"buy","fee_paid":NaN}'])
def test_malformed_json_is_domain_blocked(tmp_path, monkeypatch, payload):
    context = seed(tmp_path, monkeypatch)
    execute(tmp_path, "UPDATE receipts SET payload=? WHERE id='z-first'", (payload,))
    with pytest.raises(ProfitBridgeBlocked):
        receipts(tmp_path, context)


def test_empty_terminal_cannot_release_a_committed_native_liability(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, fills=False, terminal="CANCELED")
    execute(tmp_path, "UPDATE capital_reservations SET status='COMMITTED'", native=True)
    with pytest.raises(ProfitBridgeBlocked, match="not reconciled"):
        closure(tmp_path, context)


@pytest.mark.parametrize("sql", ["DELETE FROM execution_receipts", "DELETE FROM execution_provenance",
    "UPDATE execution_provenance SET controller_id='foreign'", "DELETE FROM economic_fill_bindings"])
def test_empty_terminal_still_requires_exact_native_execution(tmp_path, monkeypatch, sql):
    context = seed(tmp_path, monkeypatch, fills=False, terminal="CANCELED")
    execute(tmp_path, sql, native=True)
    with pytest.raises(ProfitBridgeBlocked):
        closure(tmp_path, context)


def test_unopened_no_fill_cancellation_needs_no_fabricated_frozen_execution(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, fills=False, terminal="CANCELED")
    mutate_state(tmp_path, lambda state: state["orders"]["buy"].update(execution_frozen=False))
    result = closure(tmp_path, context)
    assert result.batch.receipts == ()
    assert result.as_dict()["native_authority"]["reservation"]["proposal_id"] == "proposal"


def test_raw_receipt_bytes_are_not_silently_canonicalized(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch)
    original = receipts(tmp_path, context)
    raw = json.loads(execute(tmp_path, "SELECT payload FROM receipts WHERE id='z-first'")[0][0])
    execute(tmp_path, "UPDATE receipts SET payload=? WHERE id='z-first'", (json.dumps(raw, indent=2),))
    changed = receipts(tmp_path, context)
    assert original.total_fee_inclusive_debit == changed.total_fee_inclusive_debit
    assert original.source_digest != changed.source_digest
    assert json.loads(changed.receipts[0])["paper_receipt"]["payload"] == json.dumps(raw, indent=2)


def test_arithmetic_context_is_independent_of_callers_exponent_limits(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.Emax = 1
        assert context.full_budget == D(120)
        assert receipts(tmp_path, context).total_fee_inclusive_debit == D("94.28")


@pytest.mark.parametrize("table,field,value", [
    ("economic_fill_bindings", "created_at", NOW+1),
    ("economic_fill_bindings", "created_at", NOW-4),
    ("economic_fill_bindings", "created_at", NOW-21),
    ("execution_provenance", "created_at", NOW+1),
    ("execution_provenance", "created_at", NOW-4),
    ("execution_provenance", "created_at", NOW-21),
    ("execution_receipts", "created_at", NOW+1),
    ("execution_receipts", "created_at", NOW-4),
    ("execution_receipts", "created_at", NOW-21),
    ("capital_reservations", "created_at", NOW+1),
    ("capital_reservations", "created_at", NOW-4),
    ("capital_reservations", "updated_at", NOW+1),
    ("capital_reservations", "updated_at", NOW-21),
    ("economic_positions", "created_at", NOW+1),
    ("economic_positions", "created_at", NOW-4),
    ("economic_positions", "created_at", NOW-18),
    ("economic_positions", "updated_at", NOW+1),
    ("economic_positions", "updated_at", NOW-31),
])
def test_zero_fill_terminal_has_unconditional_native_authority_chronology(
        tmp_path, monkeypatch, table, field, value):
    context = seed(tmp_path, monkeypatch, fills=False, terminal="CANCELED")
    assert closure(tmp_path, context).batch.receipts == ()
    execute(tmp_path, f"UPDATE {table} SET {field}=?", (value,), native=True)
    with pytest.raises(ProfitBridgeBlocked, match="chronology"):
        closure(tmp_path, context)
    with pytest.raises(ProfitBridgeBlocked, match="chronology"):
        receipts(tmp_path, context)


def test_zero_fill_terminal_cannot_move_complete_authority_chain_after_closure(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, fills=False, terminal="CANCELED")
    # Preserve relative creation ordering but move all authority to a timestamp
    # after the terminal audit and before current paper time.
    for table in ("execution_provenance", "execution_receipts", "economic_fill_bindings"):
        execute(tmp_path, f"UPDATE {table} SET created_at=?", (NOW-4,), native=True)
    execute(tmp_path, "UPDATE capital_reservations SET updated_at=?", (NOW-4,), native=True)
    with pytest.raises(ProfitBridgeBlocked, match="terminal boundary"):
        closure(tmp_path, context)


def test_later_native_position_mutation_does_not_rewrite_original_closure_authority(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, fills=False, terminal="CANCELED")
    before = closure(tmp_path, context)
    # Native position updated_at is mutable. Original creation still predates
    # the binding, and a subsequent valid position mutation may follow closure.
    execute(tmp_path, "UPDATE economic_positions SET updated_at=?", (NOW-1,), native=True)
    assert closure(tmp_path, context).source_payload == before.source_payload


def test_later_mutable_reservation_watermark_preserves_original_closure_authority(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, fills=False, terminal="CANCELED")
    before = closure(tmp_path, context)
    execute(tmp_path, "UPDATE capital_reservations SET updated_at=?", (NOW-1,), native=True)
    assert closure(tmp_path, context).source_payload == before.source_payload


def test_actual_later_native_sell_preserves_buy_receipt_and_authority_proofs(tmp_path, monkeypatch):
    context = seed(tmp_path, monkeypatch, terminal="FILLED")
    original = closure(tmp_path, context)
    later = NOW+23
    monkeypatch.setattr(governor_module, "time", types.SimpleNamespace(time=lambda: later))
    governor = CapitalGovernor(tmp_path / "native.sqlite", CapitalPolicy(D(0), D(10000), D(10000), D(10000), D(5000)))
    ledger = EconomicPositionLedger(tmp_path / "native.sqlite")
    reconciler = LiveFillLedgerReconciler(ledger=ledger, governor=governor)
    reconciler.bind_executor_order(order_id="sell", position_id="position", proposal_id="proposal",
        role=EconomicFillRole.POSITION_EXIT, account_name=ACCOUNT, connector_name=CONNECTOR,
        pair="BTC-USD", created_at=NOW+10)
    raw = dict(trade_id="later-sale", order_id="sell", account_name=ACCOUNT, connector_name=CONNECTOR,
        trading_pair="BTC-USD", trade_type="SELL", amount="1", price="120", fee_paid=".21",
        fee_currency="USD", timestamp=datetime.fromtimestamp(NOW+20, timezone.utc).isoformat())
    reconciler.reconcile_trade_fill(raw, reconciled_at=NOW+21)
    # Exercise actual native executor accounting after disposing all bought lots.
    governor.reconcile_executor_accounting(ExecutorAccountingObservation(
        executor_id="executor", status="TERMINATED", filled_amount_quote=D(214),
        cum_fees_quote=D(".49"), position_size_quote=D(0), executor_type="grid_executor", close_type="COMPLETED"))
    sell = dict(id="sell", side="SELL", pair="BTC-USD", position="position", proposal="proposal",
        executor="executor", pool=context.entry_pool, funding_owner="seed", intent="ENTRY", target="0",
        principal="0", quantity="1", reference="120", decision_at=NOW+10, created_at=NOW+10,
        status="FILLED", terminal="FILLED", eligible_lot_ids=["trade-fill:z-first", "trade-fill:a-second"])
    def record_order(state):
        state["orders"]["sell"] = sell
        state["now"] = later
    mutate_state(tmp_path, record_order)
    execute(tmp_path, "INSERT INTO receipts VALUES(?,?,?,1)", ("later-sale", "sell", canonical_json(raw)))
    execute(tmp_path, "INSERT INTO audit_events(at,kind,payload) VALUES(?,'receipt_reconciled',?)",
            (NOW+21, canonical_json({"trade_id": "later-sale"})))
    execute(tmp_path, "INSERT INTO audit_events(at,kind,payload) VALUES(?,'terminal_reconciliation',?)",
            (NOW+22, canonical_json({"order": "sell", "status": "FILLED"})))
    report = ExecutorLedgerSyncReport("proposal", "executor", "position", 2, 2, 3, 3, 1, 2, (), (), True, ())
    AccountingHealthSupervisor(tmp_path / "native.sqlite").record_reports(
        position_id="position", reports=(report,), now=later)
    assert execute(tmp_path, "SELECT status FROM economic_positions", native=True)[0][0] == "CLOSED"
    assert all(D(row[0]) == 0 for row in execute(tmp_path, "SELECT base_remaining FROM economic_lots", native=True))
    # A later causal native reservation watermark is allowed without granting
    # any new authority or changing the immutable original BUY receipt bytes.
    execute(tmp_path, "UPDATE capital_reservations SET updated_at=?", (later,), native=True)
    replay = closure(tmp_path, context, later)
    assert replay.terminal_at == original.terminal_at
    assert replay.verified_at == later
    assert replay.batch.source_payload == original.batch.source_payload
    assert replay.as_dict()["native_authority"] == original.as_dict()["native_authority"]
    assert replay.batch.total_fee_inclusive_debit == D("94.28")
    # Immutable original BUY binding cannot move with the later SELL.
    execute(tmp_path, "UPDATE economic_fill_bindings SET created_at=? WHERE order_id='buy'", (later,), native=True)
    with pytest.raises(ProfitBridgeBlocked, match="chronology"):
        closure(tmp_path, context, later)


@pytest.mark.parametrize('funding_owner', ['seed', 'repair'])
def test_actual_bound_zero_fill_rejected_terminal_closes_exactly(tmp_path, monkeypatch, funding_owner):
    context = seed(tmp_path, monkeypatch, fills=False, terminal='REJECTED', funding_owner=funding_owner)
    batch = receipts(tmp_path, context)
    assert batch.settlement_ids == () and batch.total_fee_inclusive_debit == 0
    result = closure(tmp_path, context)
    assert result.terminal_status == 'REJECTED'
    assert result.batch == batch
    assert closure(tmp_path, context).source_payload == result.source_payload
    # A rejected pre-admission candidate without native authority is not counted.
    mutate_state(tmp_path, lambda state: state['orders'].update({'unadmitted': dict(
        state['orders']['buy'], id='unadmitted', proposal='unadmitted', executor='unadmitted')}))
    assert closure(tmp_path, context).source_payload == result.source_payload


@pytest.mark.parametrize('count', [1, 2])
def test_actual_fills_cannot_be_relabeled_rejected(tmp_path, monkeypatch, count):
    context = seed(tmp_path, monkeypatch, terminal='REJECTED', count=count)
    with pytest.raises(ProfitBridgeBlocked, match='REJECTED terminal has actual fills'):
        receipts(tmp_path, context)
    with pytest.raises(ProfitBridgeBlocked, match='REJECTED terminal has actual fills'):
        closure(tmp_path, context)


@pytest.mark.parametrize('sql', [
    "DELETE FROM economic_fill_bindings",
    "DELETE FROM execution_provenance",
    "UPDATE capital_reservations SET status='COMMITTED'",
    "UPDATE economic_accounting_health SET known_orders=0,bound_orders=0",
    "UPDATE economic_accounting_health SET confirmed_fills=1,ledgered_fills=1",
])
def test_rejected_zero_fill_requires_actual_native_closure(tmp_path, monkeypatch, sql):
    context = seed(tmp_path, monkeypatch, fills=False, terminal='REJECTED')
    execute(tmp_path, sql, native=True)
    with pytest.raises(ProfitBridgeBlocked):
        closure(tmp_path, context)
