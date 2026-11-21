from decimal import Decimal
import unittest

from spectra_alpha2.strategy import calculate_drawdown


class PositionStrategyTests(unittest.TestCase):
    def test_drawdown_uses_immutable_reference_price(self):
        self.assertEqual(calculate_drawdown(Decimal("100"),Decimal("92.23")),Decimal("-0.0777"))

    def test_drawdown_rejects_non_positive_or_non_decimal_prices(self):
        for reference,current in (
            (Decimal("0"),Decimal("100")),
            (Decimal("100"),Decimal("0")),
            (100,Decimal("90")),
        ):
            with self.assertRaises(ValueError):
                calculate_drawdown(reference,current)


if __name__ == "__main__":
    unittest.main()
