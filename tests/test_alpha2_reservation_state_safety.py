"""Reservation-state safety audit: capacity cannot leak in SUBMITTED/partial/uncertain states."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from spectra_alpha2.governor import CapitalDenied, CapitalGovernor, ReservationStatus


SAFE_ENV={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false"}
CONNECTOR="kraken_paper_trade"


def reserve(gov, position_id, asset_id=None):
    return gov.reserve_initial_position(
        position_id=position_id,
        asset_id=asset_id or f"ASSET-{position_id}-USD",
        reference_price=Decimal("100"),
        idempotency_key=f"idem-{position_id}",
    )


def make_open(gov, position_id, asset_id=None):
    auth=reserve(gov, position_id, asset_id)
    gov.consume_authorization(auth.authorization_id, idempotency_key=auth.idempotency_key, connector=CONNECTOR)
    gov.bind_order_id(auth.authorization_id, order_id=f"order-{position_id}")
    gov.settle_fill(
        auth.authorization_id,
        actual_gross_notional=Decimal("30"),
        actual_fee=Decimal("0.12"),
        actual_quantity=Decimal("0.3"),
        average_price=Decimal("100"),
    )
    return auth


def make_submitted(gov, position_id, asset_id=None):
    auth=reserve(gov, position_id, asset_id)
    gov.consume_authorization(auth.authorization_id, idempotency_key=auth.idempotency_key, connector=CONNECTOR)
    gov.bind_order_id(auth.authorization_id, order_id=f"order-{position_id}")
    return auth


class ReservationStateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/"audit.sqlite3"
        self.gov=CapitalGovernor(self.db, environment=SAFE_ENV)

    def tearDown(self):
        self.tmp.cleanup()

    def test_submitted_is_order_status_not_separate_position_status_and_counts_capacity(self):
        auth=make_submitted(self.gov, "p1", "BTC-USD")
        self.assertEqual(self.gov.snapshot()["open_positions"],1)
        positions=self.gov.list_positions()
        self.assertEqual(len(positions),1)
        self.assertEqual(positions[0]["status"],"RESERVED")  # position remains RESERVED
        self.assertNotIn("SUBMITTED",{p["status"] for p in positions})
        # SUBMITTED is a separate persisted order/reservation status while position is RESERVED.
        conn=sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT status FROM positions WHERE position_id='p1'").fetchone()[0],"RESERVED")
        self.assertEqual(conn.execute("SELECT status FROM reservations WHERE reservation_id=?",(auth.authorization_id,)).fetchone()[0],"SUBMITTED")
        conn.close()
        pending=self.gov.list_pending_authorizations()
        self.assertEqual([p["kind"] for p in pending],["BUY"])
        self.assertEqual(pending[0]["authorization_id"],auth.authorization_id)
        self.assertEqual(pending[0]["order_id"],"order-p1")
        # Duplicate asset blocked while prior submission is unresolved.
        with self.assertRaises(CapitalDenied):
            reserve(self.gov, "p2", "BTC-USD")
        self.assertEqual(self.gov.snapshot()["open_positions"],1)

    def test_uncertain_submission_retains_capacity_and_no_fill_flag_is_required(self):
        auth=make_submitted(self.gov, "p1", "BTC-USD")
        # Uncertain submission: not settled, not released, not closed; capacity retained.
        self.assertEqual(self.gov.snapshot()["open_positions"],1)
        with self.assertRaises(CapitalDenied):
            self.gov.release_unfilled_reservation(auth.authorization_id)  # SUBMITTED needs confirmed no-fill
        self.assertEqual(self.gov.snapshot()["open_positions"],1)
        self.assertEqual(len(self.gov.list_pending_authorizations()),1)

    def test_confirmed_no_fill_or_cancellation_releases_unfilled_slot(self):
        # SUBMITTED + confirmed no-fill/cancellation -> reservation RELEASED, position CANCELLED, slot freed.
        auth=make_submitted(self.gov, "p1", "BTC-USD")
        self.gov.release_unfilled_reservation(auth.authorization_id, confirmed_no_fill=True)
        self.assertEqual(self.gov.snapshot()["open_positions"],0)
        self.assertEqual(self.gov.list_positions(),[])
        self.assertEqual(self.gov.list_pending_authorizations(),[])
        conn=sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT status FROM positions WHERE position_id='p1'").fetchone()[0],"CANCELLED")
        self.assertEqual(conn.execute("SELECT status FROM reservations WHERE reservation_id=?",(auth.authorization_id,)).fetchone()[0],ReservationStatus.RELEASED.value)
        conn.close()
        # slot reusable for same asset after confirmed no-fill; not before
        reserve(self.gov, "p2", "BTC-USD")
        self.assertEqual(self.gov.snapshot()["open_positions"],1)

    def test_partial_fill_cannot_release_capacity(self):
        auth=reserve(self.gov, "p1", "BTC-USD")
        self.gov.consume_authorization(auth.authorization_id, idempotency_key=auth.idempotency_key, connector=CONNECTOR)
        self.gov.bind_order_id(auth.authorization_id, order_id="order-p1")
        # Partial buy fill: only 0.15 of 0.3 reconciled; reservation SETTLED; position OPEN.
        self.gov.settle_fill(
            auth.authorization_id,
            actual_gross_notional=Decimal("15"),
            actual_fee=Decimal("0.06"),
            actual_quantity=Decimal("0.15"),
            average_price=Decimal("100"),
        )
        self.assertEqual(self.gov.snapshot()["open_positions"],1)
        self.assertEqual(self.gov.list_positions()[0]["status"],"OPEN")
        state=self.gov.get_position_state("p1")
        self.assertEqual(state["total_quantity"],Decimal("0.15"))
        self.assertEqual(state["gross_committed"],Decimal("15"))
        # Cannot release an unfilled reservation after partial fill; cannot close with remaining lot.
        with self.assertRaises(CapitalDenied):
            self.gov.release_unfilled_reservation(auth.authorization_id, confirmed_no_fill=True)
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p1", confirmed_all_exits_reconciled=True)
        self.assertEqual(self.gov.snapshot()["open_positions"],1)
        conn=sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT status FROM reservations WHERE reservation_id=?",(auth.authorization_id,)).fetchone()[0],ReservationStatus.SETTLED.value)
        self.assertGreater(Decimal(conn.execute("SELECT remaining_quantity FROM position_lots WHERE reservation_id=?",(auth.authorization_id,)).fetchone()[0]),Decimal("0"))
        conn.close()

    def test_restart_reconciliation_cannot_temporarily_free_potentially_live_slot(self):
        auth=make_submitted(self.gov, "p1", "BTC-USD")
        # Simulate restart: reconstruct Governor over same durable db; no temporary slot free.
        restarted=CapitalGovernor(self.db, environment=SAFE_ENV)
        self.assertEqual(restarted.snapshot()["open_positions"],1)
        self.assertEqual(len(restarted.list_positions()),1)
        pending=restarted.list_pending_authorizations()
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]["kind"],"BUY")
        self.assertEqual(pending[0]["authorization_id"],auth.authorization_id)
        self.assertEqual(pending[0]["order_id"],"order-p1")
        with self.assertRaises(CapitalDenied):
            reserve(restarted, "p2", "BTC-USD")
        self.assertEqual(restarted.snapshot()["open_positions"],1)

    def test_closed_position_releases_capacity_only_after_confirmed_exit_reconciliation(self):
        auth=make_open(self.gov, "p1", "BTC-USD")
        self.assertEqual(self.gov.snapshot()["open_positions"],1)
        lot_id=f"p1:INITIAL"
        exit_auth=self.gov.authorize_exit(
            lot_id=lot_id, quantity=Decimal("0.3"), limit_price=Decimal("103"), idempotency_key="exit-p1"
        )
        self.gov.consume_exit_authorization(exit_auth.authorization_id, idempotency_key=exit_auth.idempotency_key, connector=CONNECTOR)
        self.gov.bind_exit_order_id(exit_auth.authorization_id, order_id="sell-p1")
        # before exit reconciliation, slot not reusable; close requires confirmed reconciliation
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p1")  # no confirmed_all_exits_reconciled
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p1", confirmed_all_exits_reconciled=True)  # remaining lot not zero
        self.assertEqual(self.gov.snapshot()["open_positions"],1)
        # reconcile full exit
        gross=Decimal("0.3")*Decimal("103")
        self.gov.settle_exit_fill(exit_auth.authorization_id, actual_quantity=Decimal("0.3"), average_price=Decimal("103"), actual_fee=gross*Decimal("0.004"))
        self.assertEqual(self.gov.snapshot()["open_positions"],1)  # still OPEN until confirmed close
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p1")  # closure requires confirmation flag
        self.gov.close_position("p1", confirmed_all_exits_reconciled=True)
        self.assertEqual(self.gov.snapshot()["open_positions"],0)
        self.assertEqual(self.gov.list_positions(),[])
        # slot reusable only after confirmed exit reconciliation + closure
        reserve(self.gov, "p2", "BTC-USD")

    def test_concurrent_admission_capped_at_74_across_reserved_open_and_submitted_mix(self):
        # Seed 73 capacity-consuming positions in a mix; then race for remaining slots.
        n_open=24; n_submitted=24; n_reserved=25
        for i in range(n_open):
            make_open(self.gov, f"open{i}", f"OPEN{i}-USD")
        for i in range(n_submitted):
            make_submitted(self.gov, f"sub{i}", f"SUB{i}-USD")
        for i in range(n_reserved):
            reserve(self.gov, f"res{i}", f"RES{i}-USD")
        self.assertEqual(self.gov.snapshot()["open_positions"],73)
        self.assertEqual(len(self.gov.list_positions()),73)
        def attempt(i):
            try:
                reserve(self.gov, f"race{i}", f"RACE{i}-USD")
                return True
            except CapitalDenied:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(attempt, range(20)))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.gov.snapshot()["open_positions"],74)
        self.assertEqual(len(self.gov.list_positions()),74)
        with self.assertRaises(CapitalDenied):
            reserve(self.gov, "overflow", "OVERFLOW-USD")


if __name__=="__main__":
    unittest.main()
