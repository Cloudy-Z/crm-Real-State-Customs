import unittest

from real_estate_crm_customs.party_roles import (
    normalize_party_role,
    remove_fields_from_layout,
    remove_fields_from_quick_filters,
)


class PartyRoleTests(unittest.TestCase):
    def test_seller_aliases(self):
        for value in ("Seller", "seller lead", " Owner ", "SELL"):
            self.assertEqual(normalize_party_role(value), "Seller")

    def test_buyer_aliases(self):
        for value in ("Buyer", "buyer lead", " Purchaser ", "BUY"):
            self.assertEqual(normalize_party_role(value), "Buyer")

    def test_unknown_values_do_not_guess(self):
        self.assertIsNone(normalize_party_role("Investor"))
        self.assertIsNone(normalize_party_role(""))

    def test_removes_aliases_from_flat_layout(self):
        layout = [
            {
                "columns": [
                    {"fields": ["lead_name", "custom_type", "party_type"]},
                    {"fields": [{"fieldname": "lead_type"}, "status"]},
                ]
            }
        ]
        self.assertTrue(
            remove_fields_from_layout(layout, {"custom_type", "lead_type"})
        )
        self.assertEqual(
            layout[0]["columns"],
            [
                {"fields": ["lead_name", "party_type"]},
                {"fields": ["status"]},
            ],
        )

    def test_removes_aliases_from_tabbed_layout(self):
        layout = [
            {
                "label": "Data",
                "sections": [
                    {
                        "columns": [
                            {
                                "fields": [
                                    "custom_type",
                                    {"fieldname": "party_type"},
                                    "lead_type",
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
        self.assertTrue(
            remove_fields_from_layout(layout, {"custom_type", "lead_type"})
        )
        self.assertEqual(
            layout[0]["sections"][0]["columns"][0]["fields"],
            [{"fieldname": "party_type"}],
        )

    def test_rejects_malformed_quick_filter_values(self):
        self.assertIsNone(
            remove_fields_from_quick_filters({"custom_type": 1}, {"custom_type"})
        )
        self.assertIsNone(
            remove_fields_from_quick_filters(["party_type", 3], {"custom_type"})
        )
        self.assertEqual(
            remove_fields_from_quick_filters(
                ["lead_name", "custom_type", "party_type"],
                {"custom_type", "lead_type"},
            ),
            ["lead_name", "party_type"],
        )


if __name__ == "__main__":
    unittest.main()
