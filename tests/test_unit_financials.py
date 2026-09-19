from decimal import Decimal
import unittest

from real_estate_crm_customs.unit_financials import UnitFinancialError, calculate_unit_financials


class UnitFinancialTests(unittest.TestCase):
    def test_net_over_formula(self):
        values = calculate_unit_financials(
            paid=100,
            over_price=50,
            remaining=1000,
            over_is_gross=False,
        )
        self.assertEqual(values["over_net"], Decimal("50.00"))
        self.assertEqual(values["total_price_net"], Decimal("1150.00"))
        self.assertEqual(values["property_tax"], Decimal("2.50"))
        self.assertEqual(values["commission"], Decimal("28.75"))
        self.assertEqual(values["total_gross"], Decimal("1181.25"))
        self.assertEqual(values["down_payment"], Decimal("150.00"))

    def test_gross_over_is_reversed_to_net(self):
        values = calculate_unit_financials(
            paid=100,
            over_price=81.25,
            remaining=1000,
            over_is_gross=True,
        )
        self.assertEqual(values["over_net"], Decimal("50.00"))
        self.assertEqual(values["property_tax"], Decimal("2.50"))
        self.assertEqual(values["commission"], Decimal("28.75"))
        self.assertEqual(values["total_gross"], Decimal("1181.25"))
        self.assertEqual(
            values["total_gross"],
            values["paid"] + values["remaining"] + values["over_price"],
        )

    def test_negative_input_is_rejected(self):
        with self.assertRaises(UnitFinancialError):
            calculate_unit_financials(paid=-1)

    def test_impossible_gross_over_is_rejected(self):
        with self.assertRaises(UnitFinancialError):
            calculate_unit_financials(
                paid=100,
                over_price=1,
                remaining=1000,
                over_is_gross=True,
            )

    def test_gross_rounding_boundary_reconciles_displayed_components(self):
        values = calculate_unit_financials(
            paid=0.01,
            over_price=0.19,
            remaining=0,
            over_is_gross=True,
        )
        self.assertEqual(
            values["total_price_net"],
            values["paid"] + values["over_net"] + values["remaining"],
        )
        self.assertEqual(
            values["total_gross"],
            values["paid"]
            + values["over_net"]
            + values["property_tax"]
            + values["commission"]
            + values["remaining"],
        )


if __name__ == "__main__":
    unittest.main()
