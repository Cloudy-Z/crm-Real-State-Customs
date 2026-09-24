import unittest

from real_estate_crm_customs.party_roles import (
    normalize_lead_view_columns,
    normalize_lead_view_filters,
    normalize_lead_view_rows,
    normalize_party_role,
    party_role_from_view_filters,
    remove_fields_from_layout,
    remove_fields_from_quick_filters,
    resolve_party_role,
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

    def test_relationship_evidence_has_priority(self):
        self.assertEqual(
            resolve_party_role("Buyer", (), owns_unit=True),
            ("Seller", "unit_owner"),
        )
        self.assertEqual(
            resolve_party_role("Seller", (), has_interest=True),
            ("Buyer", "lead_interest"),
        )
        self.assertEqual(
            resolve_party_role("Buyer", (), owns_unit=True, has_interest=True),
            ("Seller", "relationship_conflict"),
        )

    def test_unambiguous_legacy_alias_repairs_stale_canonical_default(self):
        self.assertEqual(
            resolve_party_role("Buyer", ("Seller",)),
            ("Seller", "legacy_alias_conflict"),
        )
        self.assertEqual(
            resolve_party_role("", ("buyer lead",)),
            ("Buyer", "legacy_alias"),
        )

    def test_ambiguous_or_missing_evidence_requires_explicit_default_policy(self):
        self.assertEqual(
            resolve_party_role("", ("Buyer", "Seller")),
            (None, "legacy_alias_conflict"),
        )
        self.assertEqual(resolve_party_role("", ()), (None, "missing"))

    def test_normalizes_canonical_and_legacy_saved_view_role_filters(self):
        self.assertEqual(
            party_role_from_view_filters('{"party_type": "Buyer"}'),
            "Buyer",
        )
        self.assertEqual(
            party_role_from_view_filters({"party_type": ["=", "Seller"]}),
            "Seller",
        )
        cleaned, role, changed = normalize_lead_view_filters(
            '{"custom_type": "Buyer"}'
        )
        self.assertEqual(cleaned, {"party_type": "Buyer"})
        self.assertEqual(role, "Buyer")
        self.assertTrue(changed)
        cleaned, role, changed = normalize_lead_view_filters(
            {"lead_type": "Seller", "status": "New"}
        )
        self.assertEqual(
            cleaned,
            {"party_type": "Seller", "status": "New"},
        )
        self.assertEqual(role, "Seller")
        self.assertTrue(changed)
        self.assertIsNone(party_role_from_view_filters({"party_type": "Owner"}))
        self.assertIsNone(party_role_from_view_filters("not-json"))

    def test_normalizes_legacy_saved_view_columns_and_rows(self):
        columns = normalize_lead_view_columns(
            [
                {"label": "Name", "key": "lead_name", "type": "Data"},
                {"label": "Type", "key": "custom_type", "type": "Select"},
                {"label": "Duplicate", "key": "party_type", "type": "Data"},
            ]
        )
        self.assertEqual([column["key"] for column in columns], ["lead_name", "party_type"])
        self.assertEqual(columns[1]["label"], "Party Role")
        self.assertEqual(
            normalize_lead_view_rows(
                ["name", None, "custom_type", "party_type", "lead_type"]
            ),
            ["name", "party_type"],
        )

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
