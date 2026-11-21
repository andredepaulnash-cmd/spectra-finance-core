from decimal import Decimal
import unittest

from spectra_alpha2.policy import VCAPPhase
from spectra_alpha2.strategy import calculate_drawdown, next_vcap_shard


class PositionStrategyTests(unittest.TestCase):
    def test_drawdown_uses_immutable_reference_price(self):
        self.assertEqual(calculate_drawdown(Decimal("100"),Decimal("92.23")),Decimal("-0.0777"))

    def test_vcap_selects_independent_sequential_shards_without_dca(self):
        first=next_vcap_shard(Decimal("-0.0777"),set())
        self.assertEqual(first.step_id,"VCAP_FAT1_1")
        self.assertEqual(first.phase,VCAPPhase.FAT_1)
        second=next_vcap_shard(Decimal("-0.0974"),{"VCAP_FAT1_1"})
        self.assertEqual(second.step_id,"VCAP_FAT1_2")

    def test_minus_24_boundary_stops_automatic_vcap_selection(self):
        self.assertIsNone(next_vcap_shard(Decimal("-0.24"),set()))

    def test_no_vcap_shard_before_first_threshold(self):
        self.assertIsNone(next_vcap_shard(Decimal("-0.01"),set()))


if __name__ == "__main__":
    unittest.main()
