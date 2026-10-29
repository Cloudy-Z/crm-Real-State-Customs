"""Regression contract for the Owner Lead / Unit listing workflow (29 Oct 2026)."""

import ast
import datetime as dt
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "real_estate_crm_customs"
DOCTYPES = APP / "real_estate_crm_customs" / "doctype"
WORKFLOW = APP / "owner_workflow.py"
UNIT_JSON = DOCTYPES / "real_estate_unit" / "real_estate_unit.json"
ACTION_JSON = DOCTYPES / "lead_action_execution" / "lead_action_execution.json"
DOCUMENT_JSON = DOCTYPES / "real_estate_unit_document" / "real_estate_unit_document.json"
HOOKS = APP / "hooks.py"
PATCHES = APP / "patches.txt"
PATCH = APP / "patches" / "v16_0" / "owner_unit_listing_workflow.py"
FIXTURE = APP / "fixtures" / "custom_field.json"


class Row(dict):
    __getattr__ = dict.get


class FakeValidationError(Exception):
    pass


class FakeDB:
    def __init__(self):
        self.values = {}

    def get_value(self, doctype, filters, fields=None, as_dict=False):
        if doctype == "File":
            if isinstance(filters, dict) and str(filters.get("file_url", "")).startswith("/private/files/"):
                return Row(name="FILE-1", is_private=1)
            return None
        if doctype == "Real Estate Unit" and isinstance(filters, str):
            return self.values.get(filters)
        return None


class FakeFrappe:
    ValidationError = FakeValidationError

    def __init__(self):
        self.db = FakeDB()

    def throw(self, message, exc=None):
        raise (exc or FakeValidationError)(message)

    @staticmethod
    def whitelist():
        return lambda fn: fn


def owner_rules():
    """Compile the workflow's pure rules with a deliberately tiny Frappe double."""
    tree = ast.parse(WORKFLOW.read_text())
    keep_constants = {
        "OWNER_LEAD_STATUS_PRICING", "OWNER_LEAD_STATUS_DOCUMENTS", "OWNER_LEAD_STATUS_LISTED",
        "LISTING_PRICING_REQUESTED", "LISTING_PRICE_PROPOSED", "LISTING_DOCUMENTS_PENDING",
        "LISTING_LISTED", "LISTING_VERIFICATION_DUE", "LISTING_OFF_MARKET", "LISTING_WITHDRAWN",
        "OWNER_LISTING_STATUSES", "REQUIRED_OWNERSHIP_DOCUMENTS", "REQUIRED_OWNER_ID_DOCUMENT",
        "ACCEPTED_DOCUMENT_STATUSES", "NEGOTIATING_INTEREST_STATUSES",
    }
    keep_functions = {
        "_is_private_file", "_listing_price_present", "_listing_gate_errors", "_initial_listing_status",
        "_automatic_unit_successor", "unit_is_buyer_eligible",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in keep_constants for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in keep_functions:
            node.decorator_list = []
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    frappe = FakeFrappe()
    today = dt.date(2026, 10, 29)

    def getdate(value):
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        return dt.date.fromisoformat(str(value))

    def add_to_date(value, days=0, months=0, **_kwargs):
        date_value = getdate(value)
        if months:
            month = date_value.month - 1 + months
            year = date_value.year + month // 12
            month = month % 12 + 1
            day = min(date_value.day, [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
            date_value = dt.date(year, month, day)
        return date_value + dt.timedelta(days=days)

    namespace = {
        "frappe": frappe,
        "_": lambda text: text,
        "getdate": getdate,
        "add_to_date": add_to_date,
        "nowdate": lambda: today.isoformat(),
        "now_datetime": lambda: dt.datetime(2026, 10, 29, 12, 0),
    }
    exec(compile(module, str(WORKFLOW), "exec"), namespace)
    return namespace, frappe


class OwnerListingSchemaTests(unittest.TestCase):
    def test_unit_is_tracked_and_exposes_workflow_documents_and_buyer_counts(self):
        doc = json.loads(UNIT_JSON.read_text())
        fields = {field["fieldname"]: field for field in doc["fields"]}
        self.assertEqual(doc["track_changes"], 1)
        self.assertEqual(doc["track_views"], 1)
        for fieldname in (
            "pricing_method", "listing_status", "owner_documents", "listing_agreement_type",
            "listing_agreement_signed_on", "last_verified_on", "next_verification_due",
            "active_buyer_interest_count", "total_buyer_interest_count",
        ):
            self.assertIn(fieldname, fields)
        self.assertEqual(fields["owner_documents"]["options"], "Real Estate Unit Document")
        self.assertTrue(DOCUMENT_JSON.exists())

    def test_owner_actions_are_scoped_to_lead_or_unit_and_not_interest_rows(self):
        action = json.loads(ACTION_JSON.read_text())
        fields = {field["fieldname"]: field for field in action["fields"]}
        self.assertIn("Unit", fields["scope_type"]["options"])
        for action_type in ("Add Property", "Agent Pricing", "Price Decision", "Listing Documents"):
            self.assertIn(action_type, fields["action_type"]["options"])
        for purpose in ("Listing Intake", "Price Proposal", "Price Decision", "Documents Collection", "Availability Verification"):
            self.assertIn(purpose, fields["purpose"]["options"])

    def test_scheduler_patch_and_owner_call_fields_are_registered(self):
        self.assertIn("owner_workflow.run_daily_listing_verification", HOOKS.read_text())
        self.assertIn("owner_unit_listing_workflow", PATCHES.read_text())
        self.assertTrue(PATCH.exists())
        fixture = json.loads(FIXTURE.read_text())
        for fieldname in ("no_answer_consecutive_count", "no_answer_total_count", "last_call_outcome", "last_call_at"):
            field = next(row for row in fixture if row.get("dt") == "CRM Lead" and row.get("fieldname") == fieldname)
            self.assertIn("Owner", field["depends_on"])


class OwnerListingPureRuleTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.frappe = owner_rules()

    def test_pricing_states_allow_intake_before_price_but_owner_price_requires_documents_next(self):
        initial = self.ns["_initial_listing_status"]
        self.assertEqual(initial(Row(pricing_method="Agent Pricing", listing_purpose="For Sale", total_price_net=0)), "Pricing Requested")
        self.assertEqual(initial(Row(pricing_method="Owner Price", listing_purpose="For Sale", total_price_net=100)), "Documents Pending")

    def test_buyer_visibility_requires_listed_owner_unit_and_stops_stale_verification(self):
        eligible = self.ns["unit_is_buyer_eligible"]
        self.assertTrue(eligible(Row(owner_lead=None)))
        self.assertFalse(eligible(Row(owner_lead="OWNER-1", listing_status="Documents Pending")))
        self.assertTrue(eligible(Row(owner_lead="OWNER-1", listing_status="Listed")))
        self.assertTrue(eligible(Row(owner_lead="OWNER-1", listing_status="Verification Due", next_verification_due="2026-10-20")))
        self.assertFalse(eligible(Row(owner_lead="OWNER-1", listing_status="Verification Due", next_verification_due="2026-10-01")))

    def test_listing_gate_demands_price_documents_and_agreement(self):
        gate = self.ns["_listing_gate_errors"]
        incomplete = Row(listing_purpose="For Sale", total_price_net=0, owner_documents=[])
        errors = gate(incomplete)
        self.assertGreaterEqual(len(errors), 4)
        complete = Row(
            listing_purpose="For Sale",
            total_price_net=100,
            owner_documents=[
                Row(document_type="Ownership Contract", document_status="Verified", file="/private/files/ownership.pdf"),
                Row(document_type="Owner National ID / Passport", document_status="Verified", file="/private/files/owner-id.pdf"),
            ],
            listing_agreement_type="Exclusive",
            listing_agreement_signed_on="2026-10-01",
            listing_agreement_file="/private/files/listing-agreement.pdf",
        )
        self.assertEqual(gate(complete), [])
        complete.owner_documents[1]["file"] = None
        self.assertTrue(any("privately upload" in message for message in gate(complete)))

    def test_successor_is_one_unit_scoped_action_per_listing_state(self):
        successor = self.ns["_automatic_unit_successor"]
        lead = Row(name="OWNER-1")
        unit = Row(name="UNIT-1", listing_status="Pricing Requested")
        result = successor(lead, unit)
        self.assertEqual((result["action_type"], result["purpose"], result["unit"]), ("Agent Pricing", "Price Proposal", "UNIT-1"))
        unit["listing_status"] = "Listed"
        unit["next_verification_due"] = "2027-01-01"
        result = successor(lead, unit)
        self.assertEqual((result["action_type"], result["purpose"]), ("Call", "Availability Verification"))


if __name__ == "__main__":
    unittest.main()
