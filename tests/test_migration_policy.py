import unittest

from real_estate_crm_customs.migration_policy import (
    LegacyInterestDataError,
    normalize_legacy_interest_binding,
    parse_legacy_scope_identifiers,
)


class LegacyInterestMigrationPolicyTests(unittest.TestCase):
    def test_inventory_category_follows_canonical_unit_type(self):
        result = normalize_legacy_interest_binding(
            record_type="Inventory Unit",
            category="Resale",
            unit="UNIT-1",
            unit_inventory_type="Primary",
        )
        self.assertEqual(result["record_type"], "Inventory Unit")
        self.assertEqual(result["category"], "Primary")
        self.assertTrue(result["notes"])

    def test_inverse_inventory_mismatch_is_also_normalized(self):
        result = normalize_legacy_interest_binding(
            record_type="Inventory Unit",
            category="Primary",
            unit="UNIT-2",
            unit_inventory_type="Resale",
        )
        self.assertEqual(result["category"], "Resale")

    def test_missing_generic_request_category_becomes_brokerage_request(self):
        result = normalize_legacy_interest_binding(
            record_type="Request",
            category=None,
            unit=None,
            unit_inventory_type=None,
        )
        self.assertEqual(result["category"], "Brokerage Request")

    def test_record_type_controls_outsource_and_international_categories(self):
        outsource = normalize_legacy_interest_binding(
            record_type="Outsource",
            category="Resale",
            unit=None,
            unit_inventory_type=None,
        )
        international = normalize_legacy_interest_binding(
            record_type="International",
            category=None,
            unit=None,
            unit_inventory_type=None,
        )
        self.assertEqual(outsource["category"], "Outsource")
        self.assertEqual(international["category"], "International")

    def test_invalid_historical_shapes_are_quarantined(self):
        cases = (
            dict(record_type="Inventory Unit", category="Resale", unit=None, unit_inventory_type=None),
            dict(record_type="Inventory Unit", category="Resale", unit="UNIT-3", unit_inventory_type="Rental"),
            dict(record_type="Request", category="Resale", unit="UNIT-4", unit_inventory_type="Resale"),
            dict(record_type="Unknown", category="Resale", unit=None, unit_inventory_type=None),
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(LegacyInterestDataError):
                    normalize_legacy_interest_binding(**values)

    def test_action_scope_parser_preserves_supported_legacy_shapes(self):
        self.assertEqual(parse_legacy_scope_identifiers(None), [])
        self.assertEqual(parse_legacy_scope_identifiers('["ROW-1", "ROW-2"]'), ["ROW-1", "ROW-2"])
        self.assertEqual(parse_legacy_scope_identifiers("ROW-1"), ["ROW-1"])

    def test_action_scope_parser_rejects_objects_instead_of_broadening_to_lead(self):
        for value in ({"row": "ROW-1"}, '{"row": "ROW-1"}'):
            with self.subTest(value=value):
                with self.assertRaises(LegacyInterestDataError):
                    parse_legacy_scope_identifiers(value)


if __name__ == "__main__":
    unittest.main()
