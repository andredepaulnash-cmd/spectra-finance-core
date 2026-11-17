import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from spectra_alpha2.governor import CapitalDenied, CapitalGovernor, CapitalPool


class CapitalGovernorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "governor.sqlite3"
        self.gov = CapitalGovernor(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def open_position(self, asset="BTC-USDT", position="p1"):
        auth = self.gov.reserve_initial_position(
            position_id=position,
            asset_id=asset,
            reference_price=Decimal("100"),
            idempotency_key=f"initial-{position}",
        )
        self.gov.consume_authorization(auth.authorization_id, idempotency_key=auth.idempotency_key, connector="kraken_paper_trade")
        self.gov.settle_fill(auth.authorization_id, actual_gross_notional=Decimal("20"), actual_fee=Decimal("0.08"))
        return position

    def test_pool_budgets_are_exact(self):
        snap = self.gov.snapshot()
        self.assertEqual(snap["pools"][CapitalPool.INITIAL.value]["budget"], Decimal("6250"))
        self.assertEqual(snap["pools"][CapitalPool.RECOVERY.value]["budget"], Decimal("18750"))

    def test_initial_reservation_requires_paper_authorization_before_open(self):
        auth = self.gov.reserve_initial_position(
            position_id="p1", asset_id="BTC-USDT", reference_price=Decimal("100"), idempotency_key="i1"
        )
        self.assertEqual(self.gov.snapshot()["open_positions"], 1)
        with self.assertRaises(CapitalDenied):
            self.gov.consume_authorization(auth.authorization_id, idempotency_key="i1", connector="kraken")
        self.gov.consume_authorization(auth.authorization_id, idempotency_key="i1", connector="kraken_paper_trade")
        self.gov.settle_fill(auth.authorization_id, actual_gross_notional=Decimal("20"), actual_fee=Decimal("0.08"))
        self.assertEqual(self.gov.snapshot()["open_positions"], 1)

    def test_74_position_limit_is_atomic_under_concurrent_admission(self):
        def reserve(i):
            try:
                self.gov.reserve_initial_position(
                    position_id=f"p{i}", asset_id=f"ASSET{i}-USDT",
                    reference_price=Decimal("1"), idempotency_key=f"key-{i}"
                )
                return True
            except CapitalDenied:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(reserve,range(90)))
        self.assertEqual(sum(results),74)
        self.assertEqual(self.gov.snapshot()["open_positions"],74)

    def test_dca_uses_initial_pool_and_asset_cap_includes_reservations(self):
        self.open_position()
        for i in range(3):
            auth=self.gov.reserve_addition(
                position_id="p1",action_id=f"DCA_{i+1}",gross_notional=Decimal("20"),
                idempotency_key=f"dca-{i}",action_type="DCA"
            )
            self.gov.consume_authorization(auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade")
            self.gov.settle_fill(auth.authorization_id,actual_gross_notional=Decimal("20"),actual_fee=Decimal("0.08"))
        self.assertEqual(self.gov.snapshot()["pools"]["INITIAL"]["spent"],Decimal("80.32"))
        self.assertEqual(self.gov.snapshot()["pools"]["RECOVERY"]["spent"],Decimal("0"))

    def test_vcap_uses_recovery_pool_and_enforces_two_shards_per_asset(self):
        self.open_position()
        shards=[]
        for i,amount in enumerate((Decimal("20"),Decimal("40"))):
            auth=self.gov.reserve_addition(
                position_id="p1",action_id=f"VCAP_{i+1}",gross_notional=amount,
                idempotency_key=f"vcap-{i}",action_type="VCAP",shard_id=f"shard-{i}"
            )
            self.gov.consume_authorization(auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade")
            self.gov.settle_fill(auth.authorization_id,actual_gross_notional=amount,actual_fee=amount*Decimal("0.004"))
            shards.append(f"shard-{i}")
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_addition(
                position_id="p1",action_id="VCAP_3",gross_notional=Decimal("80"),
                idempotency_key="vcap-3",action_type="VCAP",shard_id="shard-2"
            )
        self.gov.resolve_vcap_shard(shards[0])
        allowed=self.gov.reserve_addition(
            position_id="p1",action_id="VCAP_3",gross_notional=Decimal("80"),
            idempotency_key="vcap-3",action_type="VCAP",shard_id="shard-2"
        )
        self.assertEqual(allowed.pool,CapitalPool.RECOVERY)

    def test_asset_cap_includes_unfilled_reservations(self):
        self.open_position()
        auth=self.gov.reserve_addition(
            position_id="p1",action_id="DCA_1",gross_notional=Decimal("20"),
            idempotency_key="dca-1",action_type="DCA"
        )
        self.assertIsNotNone(auth.authorization_id)
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_addition(
                position_id="p1",action_id="too-much",gross_notional=Decimal("961"),
                idempotency_key="too-much",action_type="DCA"
            )

    def test_submitted_reservation_cannot_be_released_without_cancel_confirmation(self):
        auth=self.gov.reserve_initial_position(
            position_id="p1",asset_id="BTC-USDT",reference_price=Decimal("100"),idempotency_key="i1"
        )
        self.gov.consume_authorization(auth.authorization_id,idempotency_key="i1",connector="kraken_paper_trade")
        with self.assertRaises(CapitalDenied):
            self.gov.release_unfilled_reservation(auth.authorization_id)
        self.gov.release_unfilled_reservation(auth.authorization_id,confirmed_no_fill=True)
        self.assertEqual(self.gov.snapshot()["open_positions"],0)


if __name__ == "__main__":
    unittest.main()
