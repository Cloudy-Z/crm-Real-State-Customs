"""Presence flags protect unknown budgets from Frappe Currency NOT NULL defaults."""
import ast
from pathlib import Path
import unittest
from types import SimpleNamespace
from real_estate_crm_customs.requirement_policy import requirement_snapshot, requirement_amount

ROOT = Path(__file__).resolve().parents[1]


class BudgetPresenceTests(unittest.TestCase):
    def test_unspecified_optional_limits_are_absent_even_when_database_returns_zero(self):
        snapshot = requirement_snapshot({"interest_category": "Resale", "buyer_budget": 100}, "EGP")
        self.assertEqual(snapshot["requested_budget_set"], 1)
        self.assertEqual(snapshot["requested_down_payment_budget_set"], 0)
        snapshot["requested_down_payment_budget"] = 0  # Frappe database representation
        self.assertIsNone(requirement_amount(snapshot, "requested_down_payment_budget"))
        self.assertEqual(requirement_amount(snapshot, "requested_budget"), 100)

    def test_explicit_zero_is_a_real_limit(self):
        snapshot = requirement_snapshot({"interest_category": "Primary", "buyer_budget": 0,
            "down_payment_budget": 0, "monthly_installment_budget": "0.00"}, "EGP")
        for field in ("requested_budget", "requested_down_payment_budget", "requested_monthly_installment_budget"):
            self.assertEqual(snapshot[field + "_set"], 1)
            self.assertEqual(requirement_amount(snapshot, field), 0)

    def test_backward_compatible_amount_without_presence_metadata(self):
        self.assertEqual(requirement_amount({"requested_budget": 100}, "requested_budget"), 100)
        self.assertIsNone(requirement_amount({}, "requested_budget"))

    def test_presence_migration_preserves_positive_values_and_is_idempotent(self):
        amounts = ("requested_budget", "requested_down_payment_budget", "requested_monthly_installment_budget")
        records = {
            "Lead Interest": [{"name": "I", "requested_budget": 100, "requested_down_payment_budget": 0,
                "requested_monthly_installment_budget": 0, "requirement_budget_presence_version": 0}],
            "Lead Interested Unit": [{"name": "C", "requested_budget": 200, "requested_down_payment_budget": 0,
                "requested_monthly_installment_budget": 0, "requirement_budget_presence_version": 0}],
        }
        writes = []
        def get_all(doctype, **_kwargs):
            return [SimpleRow(row) for row in records[doctype] if row["requirement_budget_presence_version"] == 0]
        def set_value(doctype, name, values, **_kwargs):
            writes.append((doctype, name, values))
            next(row for row in records[doctype] if row["name"] == name).update(values)
        namespace = {"frappe": SimpleNamespace(get_all=get_all, db=SimpleNamespace(set_value=set_value)),
            "_has_columns": lambda *_args: True}
        source = ROOT / "real_estate_crm_customs/patches/v16_0/closing_requirements_and_meetings.py"
        tree = ast.parse(source.read_text())
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "migrate_budget_presence")
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        namespace["migrate_budget_presence"]()
        self.assertEqual(len(writes), 2)
        for doctype, rows in records.items():
            row = rows[0]
            self.assertEqual(row["requested_budget_set"], 1)
            self.assertEqual(row["requested_down_payment_budget_set"], 0)
            self.assertIsNone(requirement_amount(row, amounts[1]))
        namespace["migrate_budget_presence"]()
        self.assertEqual(len(writes), 2)

    def test_installer_repeats_data_patch_after_managed_field_creation(self):
        source = (ROOT / "real_estate_crm_customs/install.py").read_text()
        tree = ast.parse(source)
        sync = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "sync_real_estate_crm_defaults")
        body = ast.get_source_segment(source, sync)
        self.assertLess(body.index("setup_crm_lead_custom_fields()"), body.index("closing_requirements_and_meetings import execute"))
        self.assertLess(body.index("migrate_standalone_lead_interests()"), body.index("closing_requirements_and_meetings import execute"))


class SimpleRow(dict):
    @property
    def name(self):
        return self["name"]
