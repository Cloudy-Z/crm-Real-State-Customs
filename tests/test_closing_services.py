"""Behavior contracts for contract-closing services without a Frappe Bench.

The production functions are AST-extracted on each run.  The doubles only replace
Frappe I/O/locks, so the assertions execute the current lifecycle service bodies.
"""

import ast
import copy
import datetime as dt
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "real_estate_crm_customs" / "interest_workflow.py"
CONTEXT = ROOT / "real_estate_crm_customs" / "workflow_context.py"
PATCH = ROOT / "real_estate_crm_customs" / "patches" / "v16_0" / "closing_requirements_and_meetings.py"
POLICY = ROOT / "real_estate_crm_customs" / "requirement_policy.py"
UNIT_CONTROLLER = (
    ROOT
    / "real_estate_crm_customs"
    / "real_estate_crm_customs"
    / "doctype"
    / "real_estate_unit"
    / "real_estate_unit.py"
)


class FrappeError(Exception):
    pass


class AttrDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value

    def reload(self):
        return self

    def save(self, **_kwargs):
        self.save_calls = self.get("save_calls", 0) + 1
        return self


class FakeFrappe:
    ValidationError = FrappeError
    PermissionError = FrappeError
    DoesNotExistError = FrappeError

    def __init__(self, db=None):
        self.db = db or FakeDB()
        self.flags = AttrDict()
        self.session = AttrDict(user="closer@example.test")

    @staticmethod
    def _dict(value=None):
        return AttrDict(value or {})

    @staticmethod
    def throw(message, *_args, **_kwargs):
        raise FrappeError(str(message))

    def get_all(self, doctype, **kwargs):
        return self.db.get_all(doctype, **kwargs)


class FakeDB:
    def __init__(self):
        self.values = {}
        self.sql_calls = []

    def sql(self, query, params=(), **_kwargs):
        self.sql_calls.append((query, params))
        return []

    def get_value(self, doctype, name, field=None, as_dict=False):
        value = self.values.get((doctype, name, field))
        if value is None and doctype == "CRM Lead" and field == "party_type":
            return "Buyer"
        return AttrDict(value) if as_dict and isinstance(value, dict) else value

    def get_all(self, _doctype, **_kwargs):
        return []


class StrictDateError(ValueError):
    pass


def extract(path, names, namespace):
    module = ast.parse(Path(path).read_text())
    wanted = set(names)
    nodes = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            clean = copy.deepcopy(node)
            clean.decorator_list = []
            nodes.append(clean)
    missing = wanted - {node.name for node in nodes}
    if missing:
        raise AssertionError(f"Missing production functions: {sorted(missing)}")
    compiled = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(compiled)
    exec(compile(compiled, str(path), "exec"), namespace)
    return {name: namespace[name] for name in names}


def actual_delivery_date():
    namespace = {
        "date": dt.date,
        "datetime": dt.datetime,
        "RequirementError": StrictDateError,
    }
    return extract(POLICY, {"delivery_date"}, namespace)["delivery_date"]


def base_service_namespace(**overrides):
    frappe = FakeFrappe()
    namespace = {
        "frappe": frappe,
        "_": lambda text: text,
        "RequirementError": StrictDateError,
        "delivery_date": actual_delivery_date(),
        "getdate": lambda value: dt.date.fromisoformat(str(value)),
        "nowdate": lambda: "2026-10-20",
        "now_datetime": lambda: "2026-10-20 12:00:00",
        "TERMINAL_INTEREST_STATUSES": {"Rejected", "Superseded", "Fulfilled", "Cancelled", "Closed Won"},
        "interest_mutation_context": lambda _operation: nullcontext(),
        "unit_mutation_context": lambda _operation: nullcontext(),
        "create_transition": Mock(),
        "mirror_interest_to_legacy": Mock(),
    }
    namespace.update(overrides)
    return namespace


class ClosingServiceBehaviorTests(unittest.TestCase):
    def service(self, namespace, *names):
        return extract(WORKFLOW, set(names), namespace)

    def test_begin_reserves_exact_available_unit_and_replay_is_idempotent(self):
        unit = AttrDict(
            name="UNIT-1", status="Available", inventory_type="Primary", owner_lead=None,
            closing_interest=None, sold_interest=None,
        )
        interest = AttrDict(
            name="INT-1", lead="BUYER-1", record_type="Inventory Unit", category="Primary",
            unit="UNIT-1", workflow_status="Negotiating", is_active=1, latest_action=None,
        )
        set_unit = Mock(side_effect=lambda _name, **values: unit.update(values))
        namespace = base_service_namespace(
            _interest_unit_name=lambda _name: "UNIT-1",
            _lock_unit=lambda _name: unit,
            _lock_interest=lambda _name: interest,
            _validate_closing_binding=Mock(),
            _set_unit_closing_state=set_unit,
            _touch_closing_interest=lambda doc, action: doc.update(latest_action=action),
            _has_closed_won_evidence=lambda _interest: False,
        )
        begin = self.service(namespace, "begin_interest_closing")["begin_interest_closing"]

        begun = begin("INT-1", action="ACTION-1")
        self.assertIs(begun, interest)
        self.assertEqual(interest.workflow_status, "Closing")
        self.assertEqual(interest.is_active, 1)
        self.assertEqual(unit, {
            "name": "UNIT-1", "status": "Reserved", "inventory_type": "Primary", "owner_lead": None,
            "closing_interest": "INT-1", "sold_interest": None,
        })
        self.assertEqual(namespace["create_transition"].call_count, 1)

        snapshot = dict(interest)
        replay = begin("INT-1", action="ACTION-1")
        self.assertIs(replay, interest)
        self.assertEqual(dict(interest), snapshot)
        self.assertEqual(namespace["create_transition"].call_count, 1)
        self.assertEqual(set_unit.call_count, 1)

    def test_begin_rejects_competing_buyer_without_touching_their_reservation(self):
        unit = AttrDict(
            name="UNIT-1", status="Reserved", inventory_type="Primary", owner_lead=None,
            closing_interest="OTHER-INTEREST", sold_interest=None,
        )
        interest = AttrDict(
            name="INT-1", lead="BUYER-2", record_type="Inventory Unit", category="Primary",
            unit="UNIT-1", workflow_status="Negotiating", is_active=1,
        )
        set_unit = Mock()
        namespace = base_service_namespace(
            _interest_unit_name=lambda _name: "UNIT-1",
            _lock_unit=lambda _name: unit,
            _lock_interest=lambda _name: interest,
            _validate_closing_binding=Mock(),
            _set_unit_closing_state=set_unit,
            _has_closed_won_evidence=lambda _interest: False,
        )
        begin = self.service(namespace, "begin_interest_closing")["begin_interest_closing"]
        with self.assertRaisesRegex(FrappeError, "sold or reserved"):
            begin("INT-1")
        self.assertEqual(interest.workflow_status, "Negotiating")
        self.assertEqual(unit.closing_interest, "OTHER-INTEREST")
        set_unit.assert_not_called()

    def test_finalization_requires_reference_and_nonfuture_signed_date_but_upload_is_optional(self):
        namespace = base_service_namespace()
        extracted = self.service(namespace, "_normalized_contract_evidence")
        normalize = extracted["_normalized_contract_evidence"]
        self.assertEqual(normalize("C-100", "2026-10-20"), ("C-100", "2026-10-20"))
        for reference, signed, message in (
            ("", "2026-10-20", "reference"),
            ("C-100", "2026-10-21", "future"),
            ("C-100", "2026/10/20", "valid YYYY-MM-DD"),
        ):
            with self.subTest(reference=reference, signed=signed):
                with self.assertRaisesRegex(FrappeError, message):
                    normalize(reference, signed)

        unit = AttrDict(name="UNIT-1", status="Reserved", inventory_type="Primary", owner_lead=None,
                         closing_interest="INT-1", sold_interest=None)
        interest = AttrDict(name="INT-1", lead="BUYER", record_type="Inventory Unit", category="Primary",
                            unit="UNIT-1", workflow_status="Closing", is_active=1, signed_contract=None)
        set_unit = Mock(side_effect=lambda _name, **values: unit.update(values))
        namespace.update(
            _interest_unit_name=lambda _name: "UNIT-1",
            _lock_unit=lambda _name: unit,
            _lock_interest=lambda _name: interest,
            _validate_closing_binding=Mock(),
            _set_unit_closing_state=set_unit,
        )
        extracted = self.service(
            namespace,
            "_normalized_contract_evidence", "_has_closed_won_evidence",
            "_assert_contract_replay_matches", "finalize_interest_contract",
        )
        closed = extracted["finalize_interest_contract"]("INT-1", "C-100", "2026-10-20")
        self.assertIs(closed, interest)
        self.assertEqual(interest.workflow_status, "Closed Won")
        self.assertEqual(interest.contract_reference, "C-100")
        self.assertEqual(interest.contract_signed_date, "2026-10-20")
        self.assertIsNone(interest.signed_contract)
        self.assertEqual(interest.contract_recorded_by, "closer@example.test")
        self.assertEqual(unit.status, "Sold")
        self.assertEqual(unit.sold_interest, "INT-1")

        immutable_snapshot = dict(interest)
        replay = extracted["finalize_interest_contract"]("INT-1", "C-100", "2026-10-20")
        self.assertIs(replay, interest)
        self.assertEqual(dict(interest), immutable_snapshot)
        with self.assertRaisesRegex(FrappeError, "conflicts"):
            extracted["finalize_interest_contract"]("INT-1", "C-OTHER", "2026-10-20")

    def test_abandon_releases_only_own_hold_and_validates_idempotent_replay(self):
        unit = AttrDict(name="UNIT-1", status="Reserved", inventory_type="Primary", owner_lead=None,
                         closing_interest="INT-1", sold_interest=None)
        interest = AttrDict(name="INT-1", lead="BUYER", record_type="Inventory Unit", category="Primary",
                            unit="UNIT-1", workflow_status="Closing", is_active=1)
        set_unit = Mock(side_effect=lambda _name, **values: unit.update(values))
        namespace = base_service_namespace(
            _interest_unit_name=lambda _name: "UNIT-1",
            _lock_unit=lambda _name: unit,
            _lock_interest=lambda _name: interest,
            _validate_closing_binding=Mock(),
            _set_unit_closing_state=set_unit,
        )
        abandon = self.service(namespace, "abandon_interest_closing")["abandon_interest_closing"]
        abandoned = abandon("INT-1", target="Negotiating", action="ACTION-1")
        self.assertIs(abandoned, interest)
        self.assertEqual(interest.workflow_status, "Negotiating")
        self.assertEqual(unit.status, "Available")
        self.assertIsNone(unit.closing_interest)
        self.assertEqual(set_unit.call_count, 1)
        self.assertIs(abandon("INT-1", target="Negotiating"), interest)
        self.assertEqual(set_unit.call_count, 1)

        unit.update(status="Reserved", closing_interest="OTHER-INTEREST")
        with self.assertRaisesRegex(FrappeError, "inconsistent"):
            abandon("INT-1", target="Negotiating")
        self.assertEqual(unit.closing_interest, "OTHER-INTEREST")


class ContextAndMigrationBehaviorTests(unittest.TestCase):
    def test_server_context_stacks_authorize_only_scoped_internal_mutations(self):
        frappe = FakeFrappe()
        namespace = {
            "frappe": frappe,
            "_": lambda text: text,
            "contextmanager": __import__("contextlib").contextmanager,
            "_CREATION_MODES": {"workflow", "migration"},
            "_CREATION_STACK_FLAG": "_real_estate_interest_creation_context_stack",
            "_MUTATION_STACK_FLAG": "_real_estate_interest_mutation_context_stack",
            "_UNIT_MUTATION_STACK_FLAG": "_real_estate_unit_mutation_context_stack",
        }
        functions = extract(
            CONTEXT,
            {
                "_flags", "_stack", "interest_creation_context", "creation_context_active",
                "creation_context_mode", "require_interest_creation_context", "interest_mutation_context",
                "interest_mutation_active", "unit_mutation_context", "unit_mutation_active",
            },
            namespace,
        )
        with self.assertRaises(FrappeError):
            functions["require_interest_creation_context"]()
        self.assertFalse(functions["interest_mutation_active"]())
        self.assertFalse(functions["unit_mutation_active"]())
        creation_context = __import__("contextlib").contextmanager(functions["interest_creation_context"])
        mutation_context = __import__("contextlib").contextmanager(functions["interest_mutation_context"])
        unit_context = __import__("contextlib").contextmanager(functions["unit_mutation_context"])
        with creation_context("workflow"):
            functions["require_interest_creation_context"]()
            self.assertEqual(functions["creation_context_mode"](), "workflow")
            with creation_context("migration"):
                self.assertEqual(functions["creation_context_mode"](), "migration")
            self.assertEqual(functions["creation_context_mode"](), "workflow")
        self.assertFalse(functions["creation_context_active"]())
        with mutation_context("contract_closing"):
            self.assertTrue(functions["interest_mutation_active"]())
        with unit_context("contract_closing"):
            self.assertTrue(functions["unit_mutation_active"]())

    def test_unit_closing_fields_are_rejected_without_context_guard(self):
        doc = AttrDict(is_new=lambda: False, has_value_changed=lambda name: name == "status")
        namespace = {
            "frappe": FakeFrappe(), "_": lambda text: text,
            "unit_mutation_active": lambda: False,
            "_changed": lambda current, *names: any(current.has_value_changed(name) for name in names),
        }
        validate = extract(UNIT_CONTROLLER, {"validate_closing_state_mutation"}, namespace)["validate_closing_state_mutation"]
        with self.assertRaisesRegex(FrappeError, "contract-closing workflow"):
            validate(doc)
        namespace["unit_mutation_active"] = lambda: True
        self.assertIsNone(validate(doc))

    def test_delivery_backfill_accepts_only_literal_iso_text_and_preserves_everything_else(self):
        class DateDB(FakeDB):
            def __init__(self):
                super().__init__()
                self.set_calls = []
                self.rows = [
                    AttrDict(name="ISO", preferred_delivery_time="2027-02-28", preferred_delivery_date=None),
                    AttrDict(name="YEAR", preferred_delivery_time="2027", preferred_delivery_date=None),
                    AttrDict(name="WORDS", preferred_delivery_time="soon", preferred_delivery_date=None),
                    AttrDict(name="OBJECT", preferred_delivery_time=dt.date(2027, 2, 28), preferred_delivery_date=None),
                ]

            def exists(self, doctype, _name):
                return doctype == "DocType"

            def has_column(self, _doctype, _field):
                return True

            def get_all(self, doctype, **_kwargs):
                return self.rows if doctype == "CRM Lead" else []

            def set_value(self, doctype, name, field, value, **_kwargs):
                self.set_calls.append((doctype, name, field, value))

            def savepoint(self, _name):
                pass

            def rollback(self, **_kwargs):
                pass

            def release_savepoint(self, _name):
                pass

        db = DateDB()
        namespace = {
            "frappe": FakeFrappe(db),
            "RequirementError": StrictDateError,
            "delivery_date": actual_delivery_date(),
            "_log_issue": Mock(),
        }
        functions = extract(PATCH, {"_has_columns", "_unambiguous_iso_date", "migrate_canonical_delivery_dates"}, namespace)
        functions["migrate_canonical_delivery_dates"]()
        self.assertEqual(db.set_calls, [("CRM Lead", "ISO", "preferred_delivery_date", "2027-02-28")])
        self.assertEqual(namespace["_log_issue"].call_count, 3)


if __name__ == "__main__":
    unittest.main()
