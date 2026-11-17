from decimal import Decimal
import unittest

from spectra_alpha2.shards import (
    ShardStatus, VCAPShard, assert_shard_slot_available,
)


def make_shard(shard_id: str, asset: str = "BTC-USDT") -> VCAPShard:
    return VCAPShard(
        shard_id=shard_id,
        position_id="position-1",
        asset_id=asset,
        step_id="VCAP_FAT1_1",
        generation=1,
        authorized_gross_notional=Decimal("20"),
    )


class VCAPShardTests(unittest.TestCase):
    def test_full_shard_lifecycle_is_explicit(self):
        shard = make_shard("s1")
        self.assertEqual(shard.status, ShardStatus.AVAILABLE)
        shard = shard.transition(ShardStatus.RESERVED, reservation_id="r1")
        shard = shard.transition(ShardStatus.SUBMITTED, order_id="o1")
        shard = shard.record_buy_fill(Decimal("20"), Decimal("1"), Decimal("0.08"))
        self.assertEqual(shard.status, ShardStatus.FILLED)
        shard = shard.activate()
        shard = shard.record_sell_fill(Decimal("20"), Decimal("1.03"), Decimal("0.0824"))
        self.assertEqual(shard.status, ShardStatus.RESOLVED)
        shard = shard.transition(ShardStatus.RECYCLED)
        self.assertEqual(shard.status, ShardStatus.RECYCLED)
        self.assertEqual(shard.gross_buy_notional, Decimal("20"))
        self.assertEqual(shard.buy_fees_quote, Decimal("0.08"))
        self.assertEqual(shard.gross_sell_proceeds, Decimal("20.60"))
        self.assertEqual(shard.sell_fees_quote, Decimal("0.0824"))
        self.assertEqual(shard.realized_pnl_quote, Decimal("0.4376"))

    def test_partial_sell_keeps_shard_active_and_tracks_remaining_basis(self):
        shard = make_shard("s1").transition(ShardStatus.RESERVED, reservation_id="r1")
        shard = shard.transition(ShardStatus.SUBMITTED, order_id="o1")
        shard = shard.record_buy_fill(Decimal("20"), Decimal("1"), Decimal("0.08")).activate()
        shard = shard.record_sell_fill(Decimal("10"), Decimal("1.03"), Decimal("0.0412"))
        self.assertEqual(shard.status, ShardStatus.ACTIVE)
        self.assertEqual(shard.remaining_quantity, Decimal("10"))
        self.assertEqual(shard.remaining_cost_basis_quote, Decimal("10.04"))
        shard = shard.record_sell_fill(Decimal("10"), Decimal("1.03"), Decimal("0.0412"))
        self.assertEqual(shard.status, ShardStatus.RESOLVED)
        self.assertEqual(shard.realized_pnl_quote, Decimal("0.4376"))

    def test_shard_cannot_exceed_its_authorized_notional(self):
        shard = make_shard("s1").transition(ShardStatus.RESERVED, reservation_id="r1")
        shard = shard.transition(ShardStatus.SUBMITTED, order_id="o1")
        with self.assertRaises(ValueError):
            shard.record_buy_fill(Decimal("20.01"), Decimal("1"), Decimal("0.08"))

    def test_invalid_lifecycle_transition_is_rejected(self):
        with self.assertRaises(ValueError):
            make_shard("s1").transition(ShardStatus.ACTIVE)

    def test_two_shard_limit_counts_reserved_submitted_filled_and_active(self):
        first = make_shard("s1").transition(ShardStatus.RESERVED, reservation_id="r1")
        second = make_shard("s2").transition(ShardStatus.RESERVED, reservation_id="r2")
        with self.assertRaises(ValueError):
            assert_shard_slot_available("BTC-USDT", [first, second])
        assert_shard_slot_available("ETH-USDT", [first, second])

    def test_resolved_shard_releases_concurrency_slot(self):
        resolved = make_shard("s1")
        resolved = resolved.transition(ShardStatus.RESERVED, reservation_id="r1")
        resolved = resolved.transition(ShardStatus.SUBMITTED, order_id="o1")
        resolved = resolved.record_buy_fill(Decimal("20"), Decimal("1"), Decimal("0.08")).activate()
        resolved = resolved.record_sell_fill(Decimal("20"), Decimal("1.03"), Decimal("0.0824"))
        assert_shard_slot_available("BTC-USDT", [resolved, make_shard("s2")])

if __name__ == "__main__":
    unittest.main()
