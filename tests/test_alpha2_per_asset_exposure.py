"""Phase 3B: atomic per-asset $1,000 outstanding gross exposure; no lifetime cap; no double count."""
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from spectra_alpha2.governor import CapitalDenied, CapitalGovernor
from spectra_alpha2.policy import MAX_ASSET_GROSS_NOTIONAL, validate_asset_exposure


SAFE_ENV={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false"}
CONNECTOR="kraken_paper_trade"
ZERO=Decimal("0")


def make_gov(tmp, name="exposure.sqlite3"):
    return CapitalGovernor(Path(tmp)/name, environment=SAFE_ENV)


def reserve(gov, position_id, asset_id="BTC-USD"):
    return gov.reserve_initial_position(
        position_id=position_id, asset_id=asset_id, reference_price=Decimal("100"),
        idempotency_key=f"idem-{position_id}",
    )


def make_submitted(gov, position_id, asset_id="BTC-USD"):
    auth=reserve(gov, position_id, asset_id)
    gov.consume_authorization(auth.authorization_id, idempotency_key=auth.idempotency_key, connector=CONNECTOR)
    gov.bind_order_id(auth.authorization_id, order_id=f"order-{position_id}")
    return auth


def make_open(gov, position_id, asset_id="BTC-USD", *, gross=Decimal("30"), quantity=Decimal("0.3"), fee=Decimal("0.12")):
    auth=reserve(gov, position_id, asset_id)
    gov.consume_authorization(auth.authorization_id, idempotency_key=auth.idempotency_key, connector=CONNECTOR)
    gov.bind_order_id(auth.authorization_id, order_id=f"order-{position_id}")
    gov.settle_fill(
        auth.authorization_id, actual_gross_notional=gross, actual_fee=fee,
        actual_quantity=quantity, average_price=gross/quantity,
    )
    return auth


def outstanding(gov, asset_id="BTC-USD"):
    conn=sqlite3.connect(gov.database_path)
    conn.row_factory=sqlite3.Row
    value=gov._asset_outstanding_gross(conn, asset_id)
    conn.close()
    return value


class PerAssetExposureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.gov=make_gov(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_max_asset_gross_is_1000_and_validate_asset_exposure(self):
        self.assertEqual(MAX_ASSET_GROSS_NOTIONAL, Decimal("1000"))
        self.assertEqual(validate_asset_exposure(Decimal("970"), Decimal("30")), Decimal("1000"))
        with self.assertRaises(ValueError):
            validate_asset_exposure(Decimal("970"), Decimal("30.01"))

    def test_initial_30_counts_against_ceiling_without_double_count_and_fee_excluded(self):
        auth=reserve(self.gov, "p1")
        self.assertEqual(outstanding(self.gov), Decimal("30"))  # not 30 + reservation + lot + 0.12 fee
        conn=sqlite3.connect(self.gov.database_path)
        self.assertEqual(Decimal(conn.execute("SELECT fee_reserve FROM reservations WHERE reservation_id=?",(auth.authorization_id,)).fetchone()[0]), Decimal("0.12"))
        conn.close()
        # RESERVED not submitted: still 30.
        self.assertEqual(self.gov.snapshot()["open_positions"],1)
        self.assertEqual(outstanding(self.gov), Decimal("30"))

    def test_reserved_and_submitted_and_uncertain_retain_exposure(self):
        auth=make_submitted(self.gov, "p1")
        self.assertEqual(outstanding(self.gov), Decimal("30"))  # SUBMITTED reservation; RESERVED position; no lot double count
        self.assertEqual(self.gov.list_pending_authorizations()[0]["kind"], "BUY")
        # Uncertain submission: not settled, not released; capacity retained.
        self.assertEqual(outstanding(self.gov), Decimal("30"))
        self.assertEqual(self.gov.snapshot()["open_positions"],1)

    def test_proposed_authorization_exceeding_1000_rejected(self):
        # Use shared validator on actual outstanding + proposed gross; fee reserve excluded.
        with self.assertRaises(ValueError):
            validate_asset_exposure(Decimal("999"), Decimal("30"))  # 1029 > 1000
        # one active per asset still fails closed; per-asset check does not weaken it
        make_open(self.gov, "p1")
        with self.assertRaises(CapitalDenied):
            reserve(self.gov, "p2", "BTC-USD")

    def test_partial_fill_retains_only_actual_committed_gross_reconciled_against_reservation(self):
        auth=reserve(self.gov, "p1")
        self.gov.consume_authorization(auth.authorization_id, idempotency_key=auth.idempotency_key, connector=CONNECTOR)
        self.gov.bind_order_id(auth.authorization_id, order_id="order-p1")
        self.gov.settle_fill(
            auth.authorization_id, actual_gross_notional=Decimal("15"), actual_fee=Decimal("0.06"),
            actual_quantity=Decimal("0.15"), average_price=Decimal("100"),
        )
        self.assertEqual(outstanding(self.gov), Decimal("15"))  # not 30 reserved, not +fee, not double counted
        state=self.gov.get_position_state("p1")
        self.assertEqual(state["gross_committed"], Decimal("15"))
        self.assertEqual(state["total_quantity"], Decimal("0.15"))
        self.assertEqual(state["total_cost_basis_quote"], Decimal("15.06"))  # fee in ledger, not gross exposure

    def test_confirmed_no_fill_cancellation_releases_reserved_exposure(self):
        auth=make_submitted(self.gov, "p1")
        self.assertEqual(outstanding(self.gov), Decimal("30"))
        self.gov.release_unfilled_reservation(auth.authorization_id, confirmed_no_fill=True)
        self.assertEqual(outstanding(self.gov), Decimal("0"))
        self.assertEqual(self.gov.snapshot()["open_positions"],0)
        self.assertEqual(self.gov.list_pending_authorizations(),[])

    def test_partial_sell_reduces_only_remaining_outstanding_gross(self):
        auth=make_open(self.gov, "p1", gross=Decimal("30"), quantity=Decimal("0.3"), fee=Decimal("0.12"))
        self.assertEqual(outstanding(self.gov), Decimal("30"))
        lot_id="p1:INITIAL"
        exit_auth=self.gov.authorize_exit(lot_id=lot_id, quantity=Decimal("0.15"), limit_price=Decimal("103"), idempotency_key="exit-partial")
        self.gov.consume_exit_authorization(exit_auth.authorization_id, idempotency_key=exit_auth.idempotency_key, connector=CONNECTOR)
        self.gov.bind_exit_order_id(exit_auth.authorization_id, order_id="sell-partial")
        gross=Decimal("0.15")*Decimal("103")
        self.gov.settle_exit_fill(exit_auth.authorization_id, actual_quantity=Decimal("0.15"), average_price=Decimal("103"), actual_fee=gross*Decimal("0.004"))
        self.assertEqual(outstanding(self.gov), Decimal("15"))  # remaining 0.15 * actual gross-per-unit 100
        self.assertEqual(self.gov.snapshot()["open_positions"],1)  # still OPEN; no slot release

    def test_fully_reconciled_closure_releases_and_reentry_allowed_no_lifetime_cap(self):
        auth=make_open(self.gov, "p1", gross=Decimal("30"), quantity=Decimal("0.3"), fee=Decimal("0.12"))
        self.assertEqual(outstanding(self.gov), Decimal("30"))
        lot_id="p1:INITIAL"
        exit_auth=self.gov.authorize_exit(lot_id=lot_id, quantity=Decimal("0.3"), limit_price=Decimal("103"), idempotency_key="exit-full")
        self.gov.consume_exit_authorization(exit_auth.authorization_id, idempotency_key=exit_auth.idempotency_key, connector=CONNECTOR)
        self.gov.bind_exit_order_id(exit_auth.authorization_id, order_id="sell-full")
        gross=Decimal("0.3")*Decimal("103")
        self.gov.settle_exit_fill(exit_auth.authorization_id, actual_quantity=Decimal("0.3"), average_price=Decimal("103"), actual_fee=gross*Decimal("0.004"))
        self.assertEqual(outstanding(self.gov), Decimal("0"))  # no remaining lot, though OPEN until close
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p1", confirmed_all_exits_reconciled=False)
        self.gov.close_position("p1", confirmed_all_exits_reconciled=True)
        self.assertEqual(outstanding(self.gov), Decimal("0"))
        self.assertEqual(self.gov.snapshot()["open_positions"],0)
        # Re-entry after confirmed closure allowed; not a lifetime $1,000 cap; historical turnover ignored.
        reserve(self.gov, "p2", "BTC-USD")
        self.assertEqual(outstanding(self.gov), Decimal("30"))

    def test_closed_and_cancelled_and_historical_events_do_not_count(self):
        # CANCELLED no-fill not counted; CLOSED not counted; events/settled history not counted.
        auth=make_submitted(self.gov, "p1")
        self.gov.release_unfilled_reservation(auth.authorization_id, confirmed_no_fill=True)
        self.assertEqual(outstanding(self.gov), Decimal("0"))
        self.assertEqual(self.gov.snapshot()["open_positions"],0)
        conn=sqlite3.connect(self.gov.database_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0, True)
        conn.close()
        self.assertEqual(outstanding(self.gov), Decimal("0"))

    def test_concurrent_authorization_attempts_cannot_exceed_1000_same_asset(self):
        barrier=threading.Barrier(16)
        def attempt(i):
            barrier.wait()
            try:
                reserve(self.gov, f"race{i}", "BTC-USD")
                return True
            except CapitalDenied:
                return False
        with ThreadPoolExecutor(max_workers=16) as pool:
            results=list(pool.map(attempt, range(16)))
        self.assertEqual(sum(results),1)  # one active per asset + BEGIN IMMEDIATE; no race double count
        self.assertEqual(outstanding(self.gov), Decimal("30"))
        self.assertLessEqual(outstanding(self.gov), MAX_ASSET_GROSS_NOTIONAL)
        self.assertEqual(self.gov.snapshot()["open_positions"],1)

    def test_fail_closed_if_exposure_cannot_be_calculated(self):
        make_open(self.gov, "p1")
        conn=sqlite3.connect(self.gov.database_path)
        conn.row_factory=sqlite3.Row
        conn.execute("UPDATE position_lots SET remaining_quantity='not-a-number' WHERE lot_id='p1:INITIAL'")
        conn.commit()
        # Fail closed: cannot reliably calculate exposure.
        with self.assertRaises(CapitalDenied):
            self.gov._asset_outstanding_gross(conn, "BTC-USD")
        conn.close()
        # Governor still rejects (one active per asset + exposure calculation cannot succeed).
        with self.assertRaises(CapitalDenied):
            reserve(self.gov, "p3", "BTC-USD")


if __name__=="__main__":
    unittest.main()
