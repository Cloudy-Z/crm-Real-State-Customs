"""Regression tests for Compound Phases and Unit Categories (28 Oct 2026).

Covers:
* Compounds import without phase rows or Unit Types (the old import blockers).
* A Phase is a Compound record with a Parent Compound, inherits Developer and
  Destination, and cannot nest or point to itself.
* Unit Category taxonomy is consistent and seeded without overwriting edits.
* The upgrade patch is registered post-model-sync and is non-destructive.
"""

import ast
import configparser
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "real_estate_crm_customs"
DOCTYPES = APP / "real_estate_crm_customs" / "doctype"
PROJECT_CONTROLLER = DOCTYPES / "real_estate_project" / "real_estate_project.py"
PROJECT_JSON = DOCTYPES / "real_estate_project" / "real_estate_project.json"
UNIT_JSON = DOCTYPES / "real_estate_unit" / "real_estate_unit.json"
UNIT_TYPE_JSON = DOCTYPES / "real_estate_unit_type" / "real_estate_unit_type.json"
CATEGORY_JSON = DOCTYPES / "real_estate_unit_category" / "real_estate_unit_category.json"
TAXONOMY = APP / "unit_taxonomy.py"
PATCH = APP / "patches" / "v16_0" / "compound_phases_and_unit_categories.py"
PATCH_MODULE = "real_estate_crm_customs.patches.v16_0.compound_phases_and_unit_categories"


def fields_of(path):
    return {field["fieldname"]: field for field in json.loads(path.read_text())["fields"]}


class FakeError(Exception):
    pass


class FakeDoesNotExist(FakeError):
    pass


class FakeLinkExists(FakeError):
    pass


class FakeDB:
    def __init__(self, projects=None, destinations=None):
        self.projects = projects or {}
        self.destinations = destinations or {}
        self.updates = []

    def get_value(self, doctype, name, fields=None, as_dict=False):
        if doctype == "Real Estate Project":
            row = self.projects.get(name)
            return Row(row) if row else None
        if doctype == "Real Estate Destination":
            return self.destinations.get(name, 1)
        return None

    def exists(self, doctype, filters=None):
        if doctype == "Real Estate Project" and isinstance(filters, dict):
            parent = filters.get("parent_compound")
            return any(row.get("parent_compound") == parent for row in self.projects.values())
        return False

    def set_value(self, doctype, name, values, update_modified=True):
        self.updates.append((doctype, name, values))


class Row(dict):
    __getattr__ = dict.get


class FakeFrappe:
    ValidationError = FakeError
    DoesNotExistError = FakeDoesNotExist
    LinkExistsError = FakeLinkExists

    def __init__(self, db):
        self.db = db

    def throw(self, message, exc=None):
        raise (exc or FakeError)(message)

    def get_all(self, doctype, filters=None, pluck=None, fields=None, limit_page_length=None):
        if doctype == "Real Estate Project" and filters and "parent_compound" in filters:
            names = [
                name
                for name, row in self.db.projects.items()
                if row.get("parent_compound") == filters["parent_compound"]
            ]
            if pluck:
                return names
            return [Row(self.db.projects[name], name=name) for name in names]
        return []


def load_project_class(db):
    module = ast.parse(PROJECT_CONTROLLER.read_text())
    nodes = [
        node
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.Assign, ast.FunctionDef))
    ]
    compiled = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(compiled)

    class Document:
        def __init__(self, **values):
            self._new = values.pop("_new", True)
            self._changed = set(values.pop("_changed", ()))
            self.__dict__.update(values)

        def is_new(self):
            return self._new

        def has_value_changed(self, fieldname):
            return fieldname in self._changed

        def get(self, key, default=None):
            return self.__dict__.get(key, default)

    namespace = {"frappe": FakeFrappe(db), "_": lambda text: text, "Document": Document}
    exec(compile(compiled, str(PROJECT_CONTROLLER), "exec"), namespace)
    return namespace


def compound(namespace, **values):
    defaults = {
        "name": values.get("project_name", "Compound A"),
        "project_name": "Compound A",
        "record_type": "Compound",
        "parent_compound": None,
        "developer": "Dev A",
        "destination": "New Cairo",
        "compound_area": None,
        "phases": [],
        "available_unit_types": [],
        "amenities": [],
    }
    defaults.update(values)
    return namespace["RealEstateProject"](**defaults)


class CompoundImportRulesTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB(
            projects={
                "Mountain View 1.1": {
                    "name": "Mountain View 1.1",
                    "record_type": "Compound",
                    "developer": "Mountain View",
                    "destination": "New Cairo",
                },
                "Phase X": {
                    "name": "Phase X",
                    "record_type": "Phase",
                    "parent_compound": "Mountain View 1.1",
                    "developer": "Mountain View",
                    "destination": "New Cairo",
                },
            }
        )
        self.ns = load_project_class(self.db)

    def test_compound_imports_without_phases_or_unit_types(self):
        doc = compound(self.ns)
        doc.validate()
        self.assertEqual(doc.record_type, "Compound")
        self.assertIsNone(doc.parent_compound)

    def test_record_type_is_normalized_from_import_casing(self):
        doc = compound(self.ns, record_type="compound")
        doc.validate()
        self.assertEqual(doc.record_type, "Compound")
        phase = compound(
            self.ns, project_name="P2", name="P2", record_type="phase", parent_compound="Mountain View 1.1"
        )
        phase.validate()
        self.assertEqual(phase.record_type, "Phase")

    def test_phase_inherits_developer_and_destination_from_parent(self):
        phase = compound(
            self.ns,
            project_name="Mountain View 1.1-Phase 2",
            name="Mountain View 1.1-Phase 2",
            record_type="Phase",
            parent_compound="Mountain View 1.1",
            developer=None,
            destination=None,
        )
        phase.validate()
        self.assertEqual(phase.developer, "Mountain View")
        self.assertEqual(phase.destination, "New Cairo")

    def test_phase_requires_existing_parent_compound(self):
        missing = compound(self.ns, project_name="P", name="P", record_type="Phase", parent_compound=None)
        with self.assertRaisesRegex(FakeError, "Parent Compound is mandatory"):
            missing.validate()
        unknown = compound(self.ns, project_name="P", name="P", record_type="Phase", parent_compound="Nope")
        with self.assertRaises(FakeDoesNotExist):
            unknown.validate()

    def test_phase_cannot_nest_or_reference_itself(self):
        nested = compound(self.ns, project_name="P", name="P", record_type="Phase", parent_compound="Phase X")
        with self.assertRaisesRegex(FakeError, "not another Phase"):
            nested.validate()
        own = compound(self.ns, project_name="P", name="P", record_type="Phase", parent_compound="P")
        with self.assertRaisesRegex(FakeError, "own Parent Compound"):
            own.validate()

    def test_compound_with_phases_cannot_become_phase(self):
        doc = compound(
            self.ns,
            project_name="Mountain View 1.1",
            name="Mountain View 1.1",
            record_type="Phase",
            parent_compound="Other",
            _new=False,
        )
        self.db.projects["Other"] = {"name": "Other", "record_type": "Compound", "developer": "D", "destination": "X"}
        with self.assertRaisesRegex(FakeError, "has Phases"):
            doc.validate()

    def test_compound_still_requires_developer_and_destination(self):
        with self.assertRaisesRegex(FakeError, "Developer is mandatory"):
            compound(self.ns, developer=None).validate()
        with self.assertRaisesRegex(FakeError, "Destination is mandatory"):
            compound(self.ns, destination=None).validate()

    def test_duplicate_amenity_rows_are_rejected(self):
        doc = compound(self.ns, amenities=[Row(amenity="Clubhouse"), Row(amenity="Clubhouse")])
        with self.assertRaisesRegex(FakeError, "duplicates"):
            doc.validate()

    def test_deleting_compound_with_phases_is_blocked(self):
        doc = compound(self.ns, project_name="Mountain View 1.1", name="Mountain View 1.1", _new=False)
        with self.assertRaises(FakeLinkExists):
            doc.on_trash()

    def test_compound_family_covers_phases_but_phase_matches_only_itself(self):
        family = self.ns["get_compound_family"]
        self.ns["frappe"].db = self.db
        self.assertEqual(family("Mountain View 1.1"), ["Mountain View 1.1", "Phase X"])
        self.assertEqual(family("Phase X"), ["Phase X"])

    def test_source_has_no_import_blocking_phase_or_unit_type_rule(self):
        source = PROJECT_CONTROLLER.read_text()
        self.assertNotIn("requires at least one phase", source)
        self.assertNotIn("requires at least one available Unit Type", source)


class CompoundPhaseSchemaTests(unittest.TestCase):
    def test_compound_schema_supports_phase_records(self):
        fields = fields_of(PROJECT_JSON)
        self.assertEqual(fields["record_type"]["options"], "Compound\nPhase")
        self.assertEqual(fields["record_type"].get("default"), "Compound")
        self.assertEqual(fields["parent_compound"]["options"], "Real Estate Project")
        self.assertIn("Phase", fields["parent_compound"]["mandatory_depends_on"])
        for fieldname in ("developer", "destination"):
            with self.subTest(field=fieldname):
                self.assertFalse(fields[fieldname].get("reqd"))
                self.assertIn("Phase", fields[fieldname]["mandatory_depends_on"])
                self.assertIn("Phase", fields[fieldname]["read_only_depends_on"])
        self.assertFalse(fields["status"].get("reqd"))
        self.assertFalse(fields["status"].get("default"))
        self.assertEqual(fields["phases"].get("read_only"), 1)

    def test_unit_has_master_compound_and_category_filter(self):
        fields = fields_of(UNIT_JSON)
        self.assertEqual(fields["master_compound"]["options"], "Real Estate Project")
        self.assertEqual(fields["master_compound"].get("read_only"), 1)
        self.assertEqual(fields["unit_category"]["options"], "Real Estate Unit Category")
        self.assertEqual(fields["unit_category"]["fetch_from"], "physical_unit_type.unit_category")
        self.assertEqual(fields_of(UNIT_TYPE_JSON)["unit_category"]["options"], "Real Estate Unit Category")

    def test_unit_category_doctype_is_an_importable_named_master(self):
        doctype = json.loads(CATEGORY_JSON.read_text())
        self.assertEqual(doctype["autoname"], "field:category_name")
        self.assertEqual(doctype.get("allow_import"), 1)
        self.assertEqual(doctype.get("istable", 0), 0)
        importers = {perm["role"] for perm in doctype["permissions"] if perm.get("import")}
        self.assertEqual(importers, {"System Manager", "Sales Manager"})


def load_taxonomy():
    spec = importlib.util.spec_from_file_location("unit_taxonomy", TAXONOMY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DEFAULT_UNIT_CATEGORIES


class UnitTaxonomyTests(unittest.TestCase):
    def test_taxonomy_has_sixteen_categories_with_unique_types(self):
        taxonomy = load_taxonomy()
        self.assertEqual(len(taxonomy), 16)
        type_names = [name.lower() for _category, _abbr, types in taxonomy for name, _code in types]
        self.assertEqual(len(type_names), len(set(type_names)))
        category_codes = [abbr for _category, abbr, _types in taxonomy]
        self.assertEqual(len(category_codes), len(set(category_codes)))

    def test_type_abbreviations_are_unique_and_valid_for_sku(self):
        codes = [code for _c, _a, types in load_taxonomy() for _name, code in types]
        self.assertEqual(len(codes), len(set(codes)))
        for code in codes:
            self.assertRegex(code, r"^[A-Z0-9]{1,4}$")

    def test_existing_default_types_keep_their_abbreviations(self):
        by_name = {name: code for _c, _a, types in load_taxonomy() for name, code in types}
        for name, code in {"Villa": "VIL", "Apartment": "APT", "Duplex": "DUP", "Penthouse": "PEN", "Studio": "STD", "Townhouse": "TWN", "Chalet": "CHL"}.items():
            self.assertEqual(by_name.get(name), code)

    def test_seed_never_overwrites_existing_records(self):
        source = (APP / "master_data.py").read_text()
        body = source.split("def ensure_default_unit_categories")[1]
        self.assertNotIn(".save(", body)
        self.assertIsNone(re.search(r"delete_doc|\.delete\(|delete\s+from", body, re.I))
        self.assertIn("if not existing.unit_category", body)

    def test_seed_runs_once_not_every_migrate(self):
        install = (APP / "install.py").read_text()
        sync_body = install.split("def sync_real_estate_crm_defaults")[1].split("\ndef ")[0]
        self.assertNotIn("ensure_default_unit_categories", sync_body)
        after_install = install.split("def after_install")[1].split("\ndef ")[0]
        self.assertIn("ensure_default_unit_categories()", after_install)


class CompoundPhasePatchTests(unittest.TestCase):
    def test_patch_is_registered_after_model_sync(self):
        parser = configparser.ConfigParser(allow_no_value=True)
        parser.read(APP / "patches.txt")
        self.assertIn(PATCH_MODULE, parser["post_model_sync"])
        self.assertNotIn(PATCH_MODULE, parser["pre_model_sync"])

    def test_patch_is_non_destructive(self):
        source = PATCH.read_text()
        self.assertIsNone(re.search(r"\bdelete\b\s+from|frappe\.delete_doc|\bdrop\b", source, re.I))
        self.assertNotIn("frappe.db.commit", source)
        self.assertIn("update_modified=False", source)

    def test_api_compound_filter_includes_phase_units(self):
        api = (APP / "api.py").read_text()
        self.assertIn('filters["project"] = ["in", _compound_family(project)]', api)
        self.assertIn('"Real Estate Unit Category"', api.split("REAL_ESTATE_FORM_DOCTYPES")[1][:400])


if __name__ == "__main__":
    unittest.main()
