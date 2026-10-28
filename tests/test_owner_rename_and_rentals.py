"""Regression tests for the Owner rename and the Sale/Rent model (28 Oct 2026).

Covers:
* Requirement policy: Deal Type, rent budget, and Unit Listing Purpose matching.
* Unit controller: rent only for Resale, sale price / rent amount rules,
  Owner Intent -> default Listing Purpose.
* Migration patch: Seller -> Owner, Rental -> Resale + For Rent, backfills,
  registered post-model-sync and non-destructive.
* No runtime code compares a role to the legacy "Seller" value.
"""

import ast
import configparser
import importlib.util
import json
import re
import sys
import types
import unittest
from pathlib import Path

from real_estate_crm_customs import requirement_policy as policy

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "real_estate_crm_customs"
DOCTYPES = APP / "real_estate_crm_customs" / "doctype"
UNIT_CONTROLLER = DOCTYPES / "real_estate_unit" / "real_estate_unit.py"
INTEREST_JSON = DOCTYPES / "lead_interest" / "lead_interest.json"
PATCH = APP / "patches" / "v16_0" / "rename_seller_to_owner_and_rentals.py"
PATCH_MODULE = "real_estate_crm_customs.patches.v16_0.rename_seller_to_owner_and_rentals"


class FakeError(Exception):
    pass


class Row(dict):
    __getattr__ = dict.get


class RequirementDealTypeTests(unittest.TestCase):
    def test_deal_type_defaults_to_buy_and_rejects_unknown_values(self):
        self.assertEqual(policy.deal_type(None), "Buy")
        self.assertEqual(policy.deal_type("Rent", "Resale"), "Rent")
        with self.assertRaises(policy.RequirementError):
            policy.deal_type("Lease")

    def test_rent_is_only_for_resale_or_brokerage_requests(self):
        self.assertEqual(policy.deal_type("Rent", "Brokerage Request"), "Rent")
        for category in ("Primary", "International", "Outsource"):
            with self.subTest(category=category):
                with self.assertRaises(policy.RequirementError):
                    policy.deal_type("Rent", category)

    def test_rent_search_is_resale_only(self):
        self.assertEqual(policy.inventory_categories("Both", "Resale", "Rent"), ["Resale"])
        self.assertEqual(policy.inventory_categories("Both", "Resale", "Buy"), ["Resale", "Primary"])

    def test_listing_purposes_per_deal(self):
        self.assertEqual(policy.listing_purposes_for_deal("Rent"), ["For Rent", "For Sale or Rent"])
        self.assertEqual(policy.listing_purposes_for_deal("Buy"), ["For Sale", "For Sale or Rent"])
        self.assertEqual(policy.listing_purposes_for_deal(None), ["For Sale", "For Sale or Rent"])

    def test_rent_snapshot_requires_monthly_rent_and_clears_purchase_budgets(self):
        payload = {
            "interest_category": "Resale",
            "deal_type": "Rent",
            "preferred_unit_category": "Residential",
            "rent_budget": "25000",
            "buyer_budget": "9000000",
            "down_payment_budget": "100",
        }
        snapshot = policy.requirement_snapshot(payload, currency="EGP", required_total=True)
        self.assertEqual(snapshot["deal_type"], "Rent")
        self.assertEqual(snapshot["requested_rent_budget"], 25000.0)
        self.assertEqual(snapshot["requested_rent_budget_set"], 1)
        self.assertIsNone(snapshot["requested_budget"])
        self.assertEqual(snapshot["requested_budget_set"], 0)
        self.assertIsNone(snapshot["requested_down_payment_budget"])
        self.assertEqual(snapshot["inventory_search_scope"], "Resale")
        self.assertEqual(snapshot["requested_unit_category"], "Residential")
        with self.assertRaises(policy.RequirementError):
            policy.requirement_snapshot({**payload, "rent_budget": None}, currency="EGP", required_total=True)

    def test_buy_snapshot_keeps_total_budget_and_ignores_rent_budget(self):
        snapshot = policy.requirement_snapshot(
            {"interest_category": "Primary", "buyer_budget": "0", "rent_budget": "5000"},
            currency="EGP",
            required_total=True,
        )
        self.assertEqual(snapshot["deal_type"], "Buy")
        self.assertEqual(snapshot["requested_budget"], 0.0)
        self.assertEqual(snapshot["requested_budget_set"], 1)
        self.assertIsNone(snapshot["requested_rent_budget"])
        self.assertEqual(snapshot["requested_rent_budget_set"], 0)


def load_unit_rules(owner_intents=None):
    """Compile the Unit controller's module-level rules against a tiny frappe double."""
    owner_intents = owner_intents or {}
    module = ast.parse(UNIT_CONTROLLER.read_text())
    nodes = [
        node
        for node in module.body
        if isinstance(node, (ast.Assign, ast.FunctionDef))
    ]
    compiled = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(compiled)

    class Meta:
        def has_field(self, fieldname):
            return fieldname == "owner_intent"

    class DB:
        def get_value(self, doctype, name, fieldname=None, *args, **kwargs):
            if doctype == "CRM Lead" and fieldname == "owner_intent":
                return owner_intents.get(name)
            return None

    def throw(message, exc=None):
        raise FakeError(message)

    frappe = types.SimpleNamespace(
        db=DB(),
        throw=throw,
        ValidationError=FakeError,
        get_meta=lambda doctype: Meta(),
    )
    namespace = {"frappe": frappe, "_": lambda text: text}
    exec(compile(compiled, str(UNIT_CONTROLLER), "exec"), namespace)
    return namespace


class Doc(dict):
    __getattr__ = dict.get


class UnitListingRulesTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_unit_rules({"OWNER-RENT": "Rent", "OWNER-BOTH": "Sell or Rent", "OWNER-SELL": "Sell"})

    def test_rental_is_no_longer_an_inventory_type(self):
        self.assertEqual(self.ns["INVENTORY_TYPES"], {"Resale", "Primary", "International"})

    def test_only_resale_units_can_be_listed_for_rent(self):
        validate = self.ns["validate_listing_purpose"]
        for inventory_type in ("Primary", "International"):
            with self.subTest(inventory_type=inventory_type):
                with self.assertRaises(FakeError):
                    validate(Doc(inventory_type=inventory_type, listing_purpose="For Rent"))
        validate(Doc(inventory_type="Resale", listing_purpose="For Rent"))

    def test_units_for_sale_need_a_sale_price(self):
        validate = self.ns["validate_listing_purpose"]
        with self.assertRaises(FakeError):
            validate(Doc(inventory_type="Resale", listing_purpose="For Sale", total_price_net=0))
        with self.assertRaises(FakeError):
            validate(Doc(inventory_type="Resale", listing_purpose="For Sale or Rent", total_price_net=0))
        validate(Doc(inventory_type="Resale", listing_purpose="For Sale", total_price_net=1500000))

    def test_units_for_rent_need_a_monthly_or_daily_rent(self):
        validate = self.ns["validate_rental_rates"]
        with self.assertRaises(FakeError):
            validate(Doc(listing_purpose="For Rent"))
        validate(Doc(listing_purpose="For Rent", rental_monthly_rate=20000))
        validate(Doc(listing_purpose="For Rent", rental_daily_rate=1500))

    def test_rent_amounts_are_rejected_on_sale_only_units_and_when_negative(self):
        validate = self.ns["validate_rental_rates"]
        with self.assertRaises(FakeError):
            validate(Doc(listing_purpose="For Sale", rental_monthly_rate=20000))
        with self.assertRaises(FakeError):
            validate(Doc(listing_purpose="For Rent", rental_monthly_rate=-1))

    def test_owner_intent_sets_the_default_listing_purpose(self):
        default = self.ns["default_listing_purpose"]
        self.assertEqual(default("OWNER-RENT"), "For Rent")
        self.assertEqual(default("OWNER-BOTH"), "For Sale or Rent")
        self.assertEqual(default("OWNER-SELL"), "For Sale")
        self.assertEqual(default("OWNER-UNKNOWN"), "For Sale")
        self.assertEqual(default(None), "For Sale")


class LeadInterestRentSchemaTests(unittest.TestCase):
    def test_interest_has_deal_type_unit_category_and_rent_budget(self):
        fields = {field["fieldname"]: field for field in json.loads(INTEREST_JSON.read_text())["fields"]}
        self.assertEqual(fields["deal_type"]["options"].splitlines(), ["Buy", "Rent"])
        self.assertEqual(fields["deal_type"].get("default"), "Buy")
        self.assertEqual(fields["deal_type"].get("reqd"), 1)
        self.assertEqual(fields["requested_unit_category"]["options"], "Real Estate Unit Category")
        self.assertEqual(fields["requested_rent_budget"]["fieldtype"], "Currency")
        self.assertEqual(fields["requested_rent_budget"]["options"], "budget_currency")
        self.assertEqual(fields["requested_rent_budget_set"]["fieldtype"], "Check")


class FakePatchDB:
    def __init__(self, tables):
        self.tables = tables
        self.updates = []

    def exists(self, doctype, name=None):
        return doctype == "DocType" and name in self.tables

    def has_column(self, doctype, fieldname):
        rows = self.tables.get(doctype, [])
        return any(fieldname in row for row in rows) or fieldname in self.tables.get(doctype + "::columns", ())

    def set_value(self, doctype, name, field, value=None, update_modified=True):
        values = field if isinstance(field, dict) else {field: value}
        self.updates.append((doctype, name, values, update_modified))
        for row in self.tables[doctype]:
            if row["name"] == name:
                row.update(values)


def _matches(row, filters):
    for field, condition in (filters or {}).items():
        value = row.get(field)
        if isinstance(condition, list):
            operator, expected = condition
            if operator == "is":
                is_set = value not in (None, "")
                if (expected == "set") != is_set:
                    return False
            elif operator == "in" and value not in expected:
                return False
        elif value != condition:
            return False
    return True


class FakePatchFrappe(types.ModuleType):
    def __init__(self, db):
        super().__init__("frappe")
        self.db = db
        self.logged = []
        self.cleared = False

    def get_all(self, doctype, filters=None, fields=None, pluck=None, limit_page_length=None):
        rows = [Row(row) for row in self.db.tables.get(doctype, []) if _matches(row, filters)]
        if pluck:
            return [row[pluck] for row in rows]
        return rows

    def log_error(self, title=None, message=None):
        self.logged.append((title, message))

    def clear_cache(self, **_kwargs):
        self.cleared = True


def load_patch(db):
    fake = FakePatchFrappe(db)
    previous = sys.modules.get("frappe")
    sys.modules["frappe"] = fake
    try:
        spec = importlib.util.spec_from_file_location("owner_rent_patch_under_test", PATCH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("frappe", None)
        else:
            sys.modules["frappe"] = previous
    module.frappe = fake
    return module, fake


class OwnerRentPatchTests(unittest.TestCase):
    def tables(self):
        return {
            "CRM Lead": [
                {"name": "L-1", "party_type": "Seller"},
                {"name": "L-2", "party_type": "Buyer"},
                {"name": "L-3", "party_type": "Owner"},
            ],
            "Real Estate Unit": [
                {"name": "U-RENT", "inventory_type": "Rental", "listing_purpose": None, "owner_lead": "L-1",
                 "rental_monthly_rate": 20000, "rental_daily_rate": 0},
                {"name": "U-RENT-BAD", "inventory_type": "Rental", "listing_purpose": None, "owner_lead": None,
                 "rental_monthly_rate": 0, "rental_daily_rate": 0},
                {"name": "U-SALE", "inventory_type": "Resale", "listing_purpose": None, "owner_lead": "L-3",
                 "rental_monthly_rate": 0, "rental_daily_rate": 0},
                {"name": "U-KEEP", "inventory_type": "Resale", "listing_purpose": "For Sale or Rent", "owner_lead": "L-3",
                 "rental_monthly_rate": 1, "rental_daily_rate": 0},
            ],
            "Lead Interest": [
                {"name": "I-1", "deal_type": None, "requested_unit_type": "Apartment", "requested_unit_category": None},
                {"name": "I-2", "deal_type": "Rent", "requested_unit_type": "Office", "requested_unit_category": "Commercial"},
            ],
            "Real Estate Unit Type": [
                {"name": "Apartment", "unit_category": "Residential"},
                {"name": "Office", "unit_category": "Commercial"},
            ],
        }

    def run_patch(self):
        db = FakePatchDB(self.tables())
        module, fake = load_patch(db)
        module.repoint_saved_lead_views = lambda: 0  # covered by lead_role_migration tests
        module.execute()
        return db, fake

    def by_name(self, db, doctype):
        return {row["name"]: row for row in db.tables[doctype]}

    def test_seller_leads_become_owner_leads(self):
        db, _fake = self.run_patch()
        leads = self.by_name(db, "CRM Lead")
        self.assertEqual(leads["L-1"]["party_type"], "Owner")
        self.assertEqual(leads["L-2"]["party_type"], "Buyer")
        self.assertEqual(leads["L-3"]["party_type"], "Owner")

    def test_rental_units_move_to_resale_for_rent_and_problems_are_logged(self):
        db, fake = self.run_patch()
        units = self.by_name(db, "Real Estate Unit")
        self.assertEqual((units["U-RENT"]["inventory_type"], units["U-RENT"]["listing_purpose"]), ("Resale", "For Rent"))
        self.assertEqual((units["U-RENT-BAD"]["inventory_type"], units["U-RENT-BAD"]["listing_purpose"]), ("Resale", "For Rent"))
        self.assertEqual(units["U-SALE"]["listing_purpose"], "For Sale")
        self.assertEqual(units["U-KEEP"]["listing_purpose"], "For Sale or Rent")
        self.assertEqual(len(fake.logged), 1)
        self.assertIn("U-RENT-BAD", fake.logged[0][1])

    def test_interests_get_buy_deal_type_and_unit_category(self):
        db, _fake = self.run_patch()
        interests = self.by_name(db, "Lead Interest")
        self.assertEqual(interests["I-1"]["deal_type"], "Buy")
        self.assertEqual(interests["I-1"]["requested_unit_category"], "Residential")
        self.assertEqual(interests["I-2"]["deal_type"], "Rent")

    def test_patch_keeps_audit_timestamps_and_is_idempotent(self):
        db, _fake = self.run_patch()
        self.assertTrue(db.updates)
        self.assertTrue(all(update_modified is False for *_rest, update_modified in db.updates))
        module, _fake = load_patch(db)
        module.repoint_saved_lead_views = lambda: 0
        db.updates.clear()
        module.execute()
        self.assertEqual(db.updates, [])

    def test_patch_is_registered_after_model_sync_and_non_destructive(self):
        parser = configparser.ConfigParser(allow_no_value=True)
        parser.read(APP / "patches.txt")
        self.assertIn(PATCH_MODULE, parser["post_model_sync"])
        self.assertNotIn(PATCH_MODULE, parser["pre_model_sync"])
        source = PATCH.read_text()
        self.assertIsNone(re.search(r"\bdelete\b\s+from|frappe\.delete_doc|\bdrop\b", source, re.I))
        self.assertNotIn("frappe.db.commit", source)


class NoLegacySellerComparisonsTests(unittest.TestCase):
    """Runtime role checks must go through party_roles so legacy and new values both work."""

    RUNTIME_FILES = (
        APP / "api.py",
        APP / "interest_workflow.py",
        APP / "install.py",
        APP / "lead_role_migration.py",
        DOCTYPES / "real_estate_unit" / "real_estate_unit.py",
        DOCTYPES / "lead_interest" / "lead_interest.py",
    )

    def test_no_direct_seller_value_comparisons(self):
        pattern = re.compile(r"""(==|!=)\s*["']Seller["']|["']Seller["']\s*(==|!=)|party_type:\s*'Seller'""")
        for path in self.RUNTIME_FILES:
            with self.subTest(path=path.name):
                self.assertIsNone(pattern.search(path.read_text()), path)

    def test_legacy_assign_endpoint_is_kept_as_an_alias(self):
        api = (APP / "api.py").read_text()
        self.assertIn("def assign_property_unit_to_owner(", api)
        self.assertIn("def assign_property_unit_to_seller(", api)
        self.assertIn("def release_rented_unit(", api)


if __name__ == "__main__":
    unittest.main()
