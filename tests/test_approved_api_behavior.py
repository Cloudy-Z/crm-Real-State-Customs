"""Focused executable contracts for the approved real-estate workflow API.

This module deliberately does not import ``api.py``: a Frappe Bench is not available in
unit-test CI.  Instead, it compiles selected production function ASTs after removing
Frappe decorators and runs their real bodies against deliberately small Frappe doubles.
That makes these behavioral tests useful without turning them into source-string tests.
"""

import ast
import copy
import datetime as dt
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "real_estate_crm_customs" / "api.py"
POLICY = ROOT / "real_estate_crm_customs" / "requirement_policy.py"
WORKFLOW_CONTEXT = ROOT / "real_estate_crm_customs" / "workflow_context.py"


class FrappeError(Exception):
    """One exception type is sufficient for the isolated API behavior tests."""


class AttrDict(dict):
    """Tiny stand-in for Frappe's attribute-addressable ``_dict``."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


class FakeDB:
    def __init__(self, units=None, existing=None, countries=None, values=None):
        self.units = units or {}
        self.existing = existing or []
        self.countries = countries or {}
        self.values = values or {}
        self.calls = []
        self.last_interest_filters = None

    def exists(self, doctype, value):
        self.calls.append(("exists", doctype, value))
        if doctype == "Lead Interest":
            return False
        if doctype in {"Currency", "Property Developer", "CRM Lead", "DocType"}:
            return True
        if doctype == "Real Estate Unit":
            return value in self.units
        return True

    def get_value(self, doctype, name, fields=None, as_dict=False):
        self.calls.append(("get_value", doctype, name, fields, as_dict))
        if doctype == "Real Estate Unit":
            value = self.units.get(name)
        elif doctype == "Real Estate Destination" and fields == "country":
            value = self.countries.get(name)
        else:
            value = self.values.get((doctype, name))
        if isinstance(value, dict):
            value = AttrDict(value)
        return value

    def sql(self, query, params=()):
        self.calls.append(("sql", query, params))
        return []


class FakeFrappe:
    ValidationError = FrappeError
    PermissionError = FrappeError
    DoesNotExistError = FrappeError

    def __init__(self, db=None):
        self.db = db or FakeDB()
        self.flags = AttrDict()
        self.session = AttrDict(user="Administrator")

    @staticmethod
    def _dict(value=None):
        return AttrDict(value or {})

    @staticmethod
    def throw(message, *_args, **_kwargs):
        raise FrappeError(str(message))

    def get_all(self, doctype, filters=None, fields=None, **_kwargs):
        if doctype == "Lead Interest":
            self.db.last_interest_filters = filters
            return [AttrDict(row) for row in self.db.existing]
        return []


def extract_functions(path, names, namespace):
    """Compile selected real function definitions with decorators stripped.

    Functions are intentionally extracted from the file under test every run rather
    than copied here, so assertions execute the current production implementation.
    """
    module = ast.parse(Path(path).read_text())
    wanted = set(names)
    nodes = []
    found = set()
    for node in module.body:
        if isinstance(node, ast.Assign) and all(isinstance(target, ast.Name) and target.id.isupper() for target in node.targets):
            nodes.append(copy.deepcopy(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            clean = copy.deepcopy(node)
            clean.decorator_list = []
            nodes.append(clean)
            found.add(node.name)
    missing = wanted - found
    if missing:
        raise AssertionError(f"Functions missing from {path}: {sorted(missing)}")
    compiled = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(compiled)
    exec(compile(compiled, str(path), "exec"), namespace)
    return {name: namespace[name] for name in names}


def policy_namespace():
    class RequirementError(ValueError):
        pass

    namespace = {
        "Decimal": Decimal,
        "InvalidOperation": InvalidOperation,
        "date": dt.date,
        "datetime": dt.datetime,
        "RequirementError": RequirementError,
    }
    extract_functions(
        POLICY,
        {"deal_type", "listing_purposes_for_deal", "money", "delivery_date", "inventory_categories", "is_property_requirement", "requirement_snapshot", "requirement_amount"},
        namespace,
    )
    return namespace


def api_namespace(db=None, **overrides):
    """Return only the globals needed by selected extracted API functions."""
    policy = policy_namespace()
    frappe = FakeFrappe(db)
    namespace = {
        "frappe": frappe,
        "_": lambda text: text,
        "RequirementError": policy["RequirementError"],
        "is_property_requirement": policy["is_property_requirement"],
        "requirement_snapshot": policy["requirement_snapshot"],
        "requirement_amount": policy["requirement_amount"],
        "_inventory_categories": policy["inventory_categories"],
        "listing_purposes_for_deal": policy["listing_purposes_for_deal"],
        "_is_owner_role": lambda value: str(value or "").strip().lower() in {"owner", "seller", "owner lead", "seller lead"},
        "_site_currency": lambda: "EGP",
        "_to_int": lambda value: int(value or 0),
        "_parse_json_object": lambda value, _label: value or {},
        "_parse_json_list": lambda value: list(value or []),
        "_validate_requirement_relations": lambda snapshot: snapshot,
        "_validate_inventory_interest": lambda _category, _units: None,
    }
    namespace.update(overrides)
    return namespace


class RequirementPolicyBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy_namespace()
        self.RequirementError = self.policy["RequirementError"]

    def test_money_handles_zero_empty_invalid_negative_nonfinite_and_extreme_values(self):
        money = self.policy["money"]
        self.assertEqual(money(0), 0.0)
        self.assertEqual(money("0.00"), 0.0)
        self.assertIsNone(money(None))
        self.assertIsNone(money(""))
        for value in (None, "", "not-a-number", True, -0.01, "NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(value=value):
                with self.assertRaises(self.RequirementError):
                    money(value, required=True)

    def test_delivery_date_requires_exact_iso_calendar_date(self):
        delivery_date = self.policy["delivery_date"]
        self.assertEqual(delivery_date("2026-02-28"), "2026-02-28")
        self.assertIsNone(delivery_date(""))
        for value in (
            "2026-2-28",
            "28-02-2026",
            "2026/02/28",
            "2026-02-30",
            "2026-02-28T10:00:00",
            dt.datetime(2026, 2, 28, 10, 0),
        ):
            with self.subTest(value=value):
                with self.assertRaises(self.RequirementError):
                    delivery_date(value)

    def test_snapshot_keeps_zero_payment_values_and_normalizes_both_scope(self):
        snapshot = self.policy["requirement_snapshot"](
            {
                "interest_category": "Resale",
                "inventory_search_scope": "Both",
                "preferred_destination": "New Cairo",
                "preferred_unit_type": "Apartment",
                "buyer_budget": 0,
                "down_payment_budget": "0.00",
                "monthly_installment_budget": 12000,
                "preferred_delivery_date": "2027-01-01",
                "preferred_compound": "P-1",
                "preferred_developer": "D-1",
                "preferred_finishing_type": "Core & Shell",
            },
            currency="EGP",
            required_total=True,
        )
        self.assertEqual(snapshot["inventory_search_scope"], "Both")
        self.assertEqual(snapshot["requested_budget"], 0.0)
        self.assertEqual(snapshot["requested_down_payment_budget"], 0.0)
        self.assertEqual(snapshot["requested_monthly_installment_budget"], 12000.0)
        self.assertEqual(snapshot["requested_delivery_date"], "2027-01-01")
        self.assertEqual(snapshot["budget_currency"], "EGP")


class InterestPayloadBehaviorTests(unittest.TestCase):
    def payload_function(self, db, **overrides):
        namespace = api_namespace(db, **overrides)
        return extract_functions(API, {"_apply_interest_payload"}, namespace)["_apply_interest_payload"], namespace

    def test_mixed_inventory_uses_actual_unit_categories_and_independent_snapshots(self):
        db = FakeDB(
            units={
                "R-100": {"inventory_type": "Resale", "project": "P-1", "destination": "D-1", "developer": "DEV-1"},
                "P-200": {"inventory_type": "Primary", "project": "P-1", "destination": "D-1", "developer": "DEV-1"},
            },
            # It is deliberately a Primary old row for a Resale unit.  Dedup must
            # be by active (lead, unit), not by a caller's category assumption.
            existing=[{"name": "OLD-R", "unit": "R-100", "category": "Primary"}],
        )
        apply_payload, _namespace = self.payload_function(db)
        lead = AttrDict(
            name="LEAD-1",
            buyer_budget=9999999,
            preferred_destination="Old destination",
            preferred_unit_type="Villa",
            preferred_compound="OLD-P",
            preferred_developer="OLD-DEV",
            preferred_finishing_type="Finished",
        )
        original_defaults = {key: lead.get(key) for key in lead}
        result = apply_payload(
            lead,
            {
                "interest_category": "Resale",
                "inventory_search_scope": "Both",
                "units": ["R-100", "P-200"],
                "preferred_destination": "D-1",
                "preferred_unit_type": "Apartment",
                "preferred_compound": "P-1",
                "preferred_developer": "DEV-1",
                "preferred_finishing_type": "Core & Shell",
                "buyer_budget": "1500000",
                "down_payment_budget": "250000",
                "monthly_installment_budget": "12000",
                "preferred_delivery_date": "2027-06-30",
            },
            require_inventory_requirements=True,
        )
        self.assertEqual(result["existing_interest_names"], ["OLD-R"])
        self.assertEqual(len(result["interest_values"]), 1)
        row = result["interest_values"][0]
        self.assertEqual(row["unit"], "P-200")
        self.assertEqual(row["category"], "Primary")
        self.assertEqual(row["record_type"], "Inventory Unit")
        self.assertEqual(row["inventory_search_scope"], "Both")
        self.assertEqual(row["requested_budget"], 1500000.0)
        self.assertEqual(row["requested_down_payment_budget"], 250000.0)
        self.assertEqual(row["requested_monthly_installment_budget"], 12000.0)
        self.assertEqual(row["requested_delivery_date"], "2027-06-30")
        self.assertEqual({key: lead.get(key) for key in original_defaults}, original_defaults)
        self.assertEqual(lead.interest_status, "Interested")
        self.assertNotIn("category", db.last_interest_filters)

        # The non-inventory Request shape receives the same independent,
        # immutable snapshot fields as Inventory Unit rows.
        requested = apply_payload(
            AttrDict(name="LEAD-2"),
            {
                "interest_category": "Resale",
                "requested_unit": 1,
                "preferred_destination": "D-1",
                "preferred_unit_type": "Apartment",
                "buyer_budget": "1500000",
                "down_payment_budget": "250000",
                "monthly_installment_budget": "12000",
                "preferred_delivery_date": "2027-06-30",
            },
            require_inventory_requirements=True,
        )["interest_values"][0]
        self.assertEqual(requested["record_type"], "Request")
        self.assertEqual(
            (requested["requested_budget"], requested["requested_down_payment_budget"],
             requested["requested_monthly_installment_budget"], requested["requested_delivery_date"]),
            (1500000.0, 250000.0, 12000.0, "2027-06-30"),
        )

    def test_inventory_unit_relation_mismatches_are_rejected(self):
        payload = {
            "interest_category": "Resale",
            "units": ["U-1"],
            "preferred_destination": "D-1",
            "preferred_unit_type": "Apartment",
            "buyer_budget": 1,
        }
        mismatches = {
            "Destination": {"inventory_type": "Resale", "destination": "D-OTHER", "project": "P-1", "developer": "DEV-1"},
            "Compound": {"inventory_type": "Resale", "destination": "D-1", "project": "P-OTHER", "developer": "DEV-1"},
            "Developer": {"inventory_type": "Resale", "destination": "D-1", "project": "P-1", "developer": "DEV-OTHER"},
        }
        configured = {
            "Destination": {},
            "Compound": {"preferred_compound": "P-1"},
            "Developer": {"preferred_developer": "DEV-1"},
        }
        for label, unit in mismatches.items():
            with self.subTest(relation=label):
                db = FakeDB(units={"U-1": unit})
                apply_payload, _ = self.payload_function(db)
                with self.assertRaisesRegex(FrappeError, label):
                    apply_payload(AttrDict(name="LEAD"), {**payload, **configured[label]}, require_inventory_requirements=True)

    def test_destination_is_mandatory_for_property_flows_and_country_for_all_international_requests(self):
        apply_payload, _ = self.payload_function(FakeDB())
        for category in ("Resale", "Primary", "Brokerage Request"):
            with self.subTest(category=category):
                data = {
                    "interest_category": category,
                    "requested_unit": 1,
                    "preferred_unit_type": "Apartment",
                    "buyer_budget": 1,
                    "request_notes": "Need a broker" if category == "Brokerage Request" else None,
                }
                with self.assertRaisesRegex(FrappeError, "Destination"):
                    apply_payload(AttrDict(name="L"), data, require_inventory_requirements=True)

        with self.assertRaisesRegex(FrappeError, "Destination"):
            apply_payload(
                AttrDict(name="L"),
                {"interest_category": "International", "international_type": "Real Estate", "international_country": "EG", "requested_unit": 1},
            )

        # Country remains mandatory for every International category. Real
        # Estate additionally requires canonical Destination; Stocks/Charity do not.
        real_estate = {"interest_category": "International", "international_type": "Real Estate", "preferred_destination": "Dubai", "requested_unit": 1}
        with self.assertRaisesRegex(FrappeError, "Country"):
            apply_payload(AttrDict(name="L"), real_estate)
        result = apply_payload(AttrDict(name="L"), {**real_estate, "international_country": "AE"})
        self.assertEqual(result["interest_values"][0]["requested_destination"], "Dubai")
        for kind in ("Stocks", "Charity Work"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(FrappeError, "Country"):
                    apply_payload(AttrDict(name="L"), {"interest_category": "International", "international_type": kind})


class ProfileAndActionPolicyBehaviorTests(unittest.TestCase):
    def test_scoped_profile_never_backfills_nullable_snapshot_from_lead_defaults(self):
        scoped = AttrDict(
            name="INT-1",
            requested_destination=None,
            requested_unit_type=None,
            requested_developer=None,
            requested_project=None,
            requested_finishing_type=None,
            requested_budget=None,
            requested_down_payment_budget=None,
            requested_monthly_installment_budget=None,
            requested_delivery_date=None,
            inventory_search_scope="Both",
            budget_currency=None,
        )
        namespace = api_namespace(
            _get_standalone_interest=lambda _lead, _name: scoped,
            _site_currency=lambda: "EGP",
        )
        profile = extract_functions(API, {"_lead_property_match_profile"}, namespace)["_lead_property_match_profile"](
            AttrDict(
                name="LEAD-1",
                buyer_budget=9000000,
                preferred_destination="Lead destination",
                preferred_unit_type="Villa",
                preferred_developer="Lead developer",
                preferred_compound="Lead project",
            ),
            "Resale",
            "INT-1",
        )
        self.assertEqual(profile.source, "Scoped Lead Interest")
        self.assertIsNone(profile.budget)
        self.assertIsNone(profile.destination)
        self.assertIsNone(profile.unit_type)
        self.assertEqual(profile.inventory_search_scope, "Both")
        self.assertEqual(profile.budget_currency, "EGP")

        # A populated snapshot is equally authoritative: neither a unit price nor
        # the Lead default may overwrite the amount used for an existing row.
        scoped.requested_budget = 1250000
        priced_profile = extract_functions(API, {"_lead_property_match_profile"}, namespace)["_lead_property_match_profile"](
            AttrDict(name="LEAD-1", buyer_budget=9000000), "Resale", "INT-1"
        )
        self.assertEqual(priced_profile.budget, 1250000)

    def test_action_type_meeting_and_closing_purposes_are_exact(self):
        namespace = api_namespace()
        namespace.update(
            ACTION_TYPES={"Meeting", "Closing", "Showing", "Offer Decision", "Call"},
            ACTION_PURPOSES={
                "Explore Meeting", "Owner Meeting", "Developer Meeting", "Contract Signing",
                "General Follow-up", "Offer Decision",
            },
        )
        validate = extract_functions(API, {"_validate_action_type"}, namespace)["_validate_action_type"]
        for purpose in ("Explore Meeting", "Owner Meeting", "Developer Meeting"):
            validate("Meeting", purpose)
        validate("Closing", "Contract Signing")
        for action_type, purpose in (("Meeting", "Contract Signing"), ("Meeting", "Other"), ("Closing", "Explore Meeting"), ("Closing", None)):
            with self.subTest(action_type=action_type, purpose=purpose):
                with self.assertRaises(FrappeError):
                    validate(action_type, purpose)

    def test_interest_actions_enforce_role_specific_meetings_closing_states_and_unit_guards(self):
        db = FakeDB(
            units={
                "R": AttrDict(status="Available", owner_lead="SELLER", developer="DEV", closing_interest=None),
                "P": AttrDict(status="Available", owner_lead=None, developer="DEV", closing_interest=None),
                "P-OWNER": AttrDict(status="Available", owner_lead="SELLER", developer="DEV", closing_interest=None),
                "P-NO-DEV": AttrDict(status="Available", owner_lead=None, developer=None, closing_interest=None),
                "SOLD": AttrDict(status="Sold", owner_lead="SELLER", developer="DEV", closing_interest=None),
                "HELD": AttrDict(status="Reserved", owner_lead="SELLER", developer="DEV", closing_interest="OTHER"),
            },
            values={("CRM Lead", "SELLER"): "Seller"},
        )
        namespace = api_namespace(db)
        allowed = extract_functions(API, {"_allowed_interest_action_definitions"}, namespace)["_allowed_interest_action_definitions"]

        def action_pairs(row):
            return {(entry["action_type"], entry["purpose"]) for entry in allowed(row)}

        resale = AttrDict(name="I-R", is_active=1, workflow_status="Negotiating", category="Resale", unit="R")
        primary = AttrDict(name="I-P", is_active=1, workflow_status="Negotiating", category="Primary", unit="P")
        resale_pairs = action_pairs(resale)
        primary_pairs = action_pairs(primary)
        self.assertIn(("Meeting", "Owner Meeting"), resale_pairs)
        self.assertNotIn(("Meeting", "Developer Meeting"), resale_pairs)
        self.assertIn(("Meeting", "Developer Meeting"), primary_pairs)
        self.assertNotIn(("Meeting", "Owner Meeting"), primary_pairs)
        self.assertNotIn(
            ("Meeting", "Owner Meeting"),
            action_pairs(AttrDict(name="I-PO", is_active=1, workflow_status="Negotiating", category="Primary", unit="P-OWNER")),
        )
        self.assertNotIn(
            ("Meeting", "Developer Meeting"),
            action_pairs(AttrDict(name="I-PN", is_active=1, workflow_status="Negotiating", category="Primary", unit="P-NO-DEV")),
        )

        for state in ("Offer Accepted", "Negotiating", "Shown - Interested"):
            with self.subTest(closing_state=state):
                pairs = action_pairs(AttrDict(name="I-P", is_active=1, workflow_status=state, category="Primary", unit="P"))
                self.assertIn(("Closing", "Contract Signing"), pairs)
        self.assertNotIn(("Closing", "Contract Signing"), action_pairs(AttrDict(name="I-R", is_active=1, workflow_status="Offer Sent", category="Resale", unit="R")))
        self.assertEqual(action_pairs(AttrDict(name="I-R", is_active=1, workflow_status="Closed Won", category="Resale", unit="R")), set())
        self.assertEqual(action_pairs(AttrDict(name="I-S", is_active=1, workflow_status="Negotiating", category="Resale", unit="SOLD")), set())
        self.assertEqual(action_pairs(AttrDict(name="I-H", is_active=1, workflow_status="Negotiating", category="Resale", unit="HELD")), set())


class ClosingAndConcurrencyBehaviorTests(unittest.TestCase):
    def action_facts(self, **overrides):
        namespace = api_namespace(
            _result_interest_rows=lambda _lead, _action, _payload, required=False: ["INT-1"],
            _standalone_interests_available=lambda: False,
            _apply_pipeline_from_facts=lambda *_args: False,
            now_datetime=lambda: "NOW",
            **overrides,
        )
        return extract_functions(API, {"_apply_action_result_facts"}, namespace)["_apply_action_result_facts"], namespace

    def test_closing_signed_passes_reference_date_and_optional_upload_to_finalizer(self):
        closed = AttrDict(name="INT-1", contract_reference="C-77", contract_signed_date="2026-08-11", signed_contract="/files/contract.pdf")
        finalizer = Mock(return_value=closed)
        apply_facts, _ = self.action_facts(finalize_interest_contract=finalizer)
        result = apply_facts(
            AttrDict(name="LEAD", status="Closing"),
            AttrDict(name="ACTION-1", action_type="Closing", purpose="Contract Signing"),
            {"outcome": "Contract Signed", "contract_reference": "C-77", "contract_signed_date": "2026-08-11", "signed_contract": "/files/contract.pdf"},
        )
        finalizer.assert_called_once_with("INT-1", "C-77", "2026-08-11", signed_contract="/files/contract.pdf", action="ACTION-1")
        self.assertEqual(result[2]["contract"], {"interest": "INT-1", "reference": "C-77", "signed_date": "2026-08-11", "signed_contract": "/files/contract.pdf"})

    def test_closing_pending_requires_successor_and_return_or_withdrawal_abandons(self):
        begin = Mock()
        abandon = Mock()
        apply_facts, _ = self.action_facts(begin_interest_closing=begin, abandon_interest_closing=abandon)
        lead = AttrDict(name="LEAD", status="Closing")
        action = AttrDict(name="ACT", action_type="Closing", purpose="Contract Signing")
        apply_facts(lead, action, {"outcome": "Pending Signing"})
        begin.assert_called_once_with("INT-1", action="ACT")
        for outcome, target in (("Return to Negotiation", "Negotiating"), ("Buyer Withdrew", "Rejected")):
            with self.subTest(outcome=outcome):
                abandon.reset_mock()
                apply_facts(lead, action, {"outcome": outcome, "result_note": "customer decision"})
                abandon.assert_called_once_with("INT-1", target=target, action="ACT", reason="customer decision")

        active_interest = AttrDict(is_active=1)
        namespace = api_namespace(
            _standalone_interests_available=lambda: True,
            _action_interest_rows=lambda _action: ["INT-1"],
            _get_standalone_interest=lambda _lead, _name: active_interest,
        )
        required = extract_functions(API, {"_next_action_is_required"}, namespace)["_next_action_is_required"]
        self.assertTrue(required(lead, action, {"outcome": "Pending Signing"}))
        active_interest.is_active = 0
        self.assertFalse(required(lead, action, {"outcome": "Contract Signed"}))

    def test_unit_commitment_locks_before_evaluating_sold_or_other_reserved_state(self):
        db = FakeDB(units={"U-1": AttrDict(status="Sold", closing_interest=None)})
        namespace = api_namespace(db)
        validate = extract_functions(API, {"_validate_interest_unit_commitment"}, namespace)["_validate_interest_unit_commitment"]
        with self.assertRaisesRegex(FrappeError, "sold, rented, or reserved"):
            validate(AttrDict(name="INT-1", unit="U-1"))
        self.assertEqual(db.calls[0][0], "sql")
        self.assertIn("FOR UPDATE", db.calls[0][1])
        self.assertEqual(db.calls[1][0], "get_value")


class BundleAndEndpointGateBehaviorTests(unittest.TestCase):
    def test_bundle_allows_interest_only_for_completed_explore_meeting(self):
        action = AttrDict(name="A-1", action_type="Meeting", purpose="Explore Meeting", workflow_status="Due", modified="M-1")
        action.reload = Mock()
        applied = Mock(return_value={"category": "Resale", "requested_unit": False, "interest_values": [], "existing_interest_names": []})
        namespace = api_namespace(
            _get_lead_doc=lambda _lead: AttrDict(name="LEAD"),
            _validate_buyer_lead=lambda _lead: None,
            _require_action_doctype=lambda: None,
            _lock_lead_workflow=lambda _lead: None,
            _bundle_idempotent_result=lambda *_args: None,
            _get_action_doc=lambda *_args: action,
            _assert_action_is_current=lambda _action: None,
            _apply_interest_payload=applied,
            _save_workflow_doc=lambda _lead: None,
            interest_creation_context=lambda _mode: nullcontext(),
            _promote_applied_interest_rows=lambda *_args: [],
            complete_lead_action=lambda **_kwargs: {"ok": True},
            ACTION_TERMINAL_STATUSES=set(),
        )
        submit = extract_functions(API, {"submit_lead_action_bundle"}, namespace)["submit_lead_action_bundle"]
        bundle = {"action": {"name": "A-1"}, "result": {"outcome": "Done"}, "interest": {"interest_category": "Resale"}}
        self.assertTrue(submit("LEAD", bundle)["ok"])
        applied.assert_called_once()

        applied.reset_mock()
        with self.assertRaisesRegex(FrappeError, "cannot change Lead interests"):
            submit("LEAD", {"action": {"name": "A-1"}, "result": {"outcome": "No Show"}, "interest": {"interest_category": "Resale"}})
        applied.assert_not_called()

    def test_public_creation_endpoints_reject_without_server_creation_context(self):
        blocked = Mock(side_effect=FrappeError("Lead Interests may only be created by the approved workflow or migration."))
        get_lead = Mock(side_effect=AssertionError("context guard must run before database access"))
        namespace = api_namespace(
            require_interest_creation_context=blocked,
            _get_lead_doc=get_lead,
        )
        endpoints = extract_functions(
            API,
            {"record_interest_determination", "add_interest_request", "link_interested_units", "add_outsource_interest"},
            namespace,
        )
        calls = (
            (endpoints["record_interest_determination"], ("LEAD", 1)),
            (endpoints["add_interest_request"], ("LEAD", "Need a broker")),
            (endpoints["link_interested_units"], ("LEAD", ["U-1"])),
            (endpoints["add_outsource_interest"], ("LEAD", "Broker Co")),
        )
        for endpoint, args in calls:
            with self.subTest(endpoint=endpoint.__name__):
                with self.assertRaises(FrappeError):
                    endpoint(*args)
        self.assertEqual(blocked.call_count, len(calls))
        get_lead.assert_not_called()

    def test_workflow_context_guard_rejects_when_backend_context_is_disabled(self):
        frappe = FakeFrappe()
        namespace = {"frappe": frappe, "_": lambda text: text, "creation_context_active": lambda: False}
        guard = extract_functions(WORKFLOW_CONTEXT, {"require_interest_creation_context"}, namespace)["require_interest_creation_context"]
        with self.assertRaisesRegex(FrappeError, "approved workflow or migration"):
            guard()


class SmartMatchFinancialAndDateBehaviorTests(unittest.TestCase):
    def smart_match_function(self, units, profile):
        namespace = api_namespace(
            _get_lead_doc=lambda _lead: AttrDict(name="LEAD", party_type="Buyer"),
            _validate_buyer_lead=lambda _lead: None,
            _apply_match_profile_overrides=lambda current, **_kwargs: current,
            _lead_property_match_profile=lambda *_args: profile,
            _requirement_money=lambda value, _label: None if value in (None, "") else float(value),
            _requirement_date=lambda value: value,
            _validate_requirement_relations=lambda values: values,
            get_available_units_for_selection=lambda **_kwargs: units,
            _score_property_match=lambda unit, current: (
                90 if unit.get("delivery_date") and unit.get("delivery_date") <= current.get("delivery_date") else 20,
                "Excellent",
                [],
                [],
            ),
            _site_currency=lambda: "EGP",
        )
        return extract_functions(API, {"get_smart_matched_units"}, namespace)["get_smart_matched_units"]

    def test_financing_is_explicitly_unknown_and_exact_filters_exclude_unverified_financing(self):
        unit = AttrDict(name="U-1", budget_currency="EGP", effective_price=100, delivery_date="2026-01-01")
        profile = AttrDict(
            budget=1000,
            budget_min=None,
            down_payment_budget=None,
            monthly_installment_budget=None,
            delivery_date=None,
            inventory_search_scope="Both",
            budget_currency="EGP",
            destination=None,
            project=None,
            developer=None,
            unit_type=None,
            finishing_type=None,
            criteria_count=0,
        )
        smart_match = self.smart_match_function([unit], profile)
        response = smart_match("LEAD", inventory_search_scope="Both", strict_filters=1, down_payment_budget=100)
        self.assertEqual(response["match_mode"], "Exact Filters")
        self.assertFalse(response["financing_available"])
        self.assertEqual(response["units"], [])
        self.assertEqual(unit.affordability["down_payment"], "Unknown financing")

    def test_delivery_before_deadline_ranks_ahead_of_later_delivery(self):
        before = AttrDict(name="BEFORE", budget_currency="EGP", effective_price=600, delivery_date="2026-01-01")
        after = AttrDict(name="AFTER", budget_currency="EGP", effective_price=500, delivery_date="2026-03-01")
        profile = AttrDict(
            budget=None,
            budget_min=None,
            down_payment_budget=None,
            monthly_installment_budget=None,
            delivery_date=None,
            inventory_search_scope="Both",
            budget_currency="EGP",
            destination=None,
            project=None,
            developer=None,
            unit_type=None,
            finishing_type=None,
            criteria_count=0,
        )
        smart_match = self.smart_match_function([after, before], profile)
        response = smart_match("LEAD", inventory_search_scope="Both", delivery_date="2026-02-01")
        self.assertEqual([unit.name for unit in response["units"]], ["BEFORE", "AFTER"])
        self.assertEqual(before.affordability["delivery"], "Within deadline")
        self.assertEqual(after.affordability["delivery"], "Later than requested")


if __name__ == "__main__":
    unittest.main()
