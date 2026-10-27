"""Regression tests for the Property Developer Founded In change.

Covers the Frappe Cloud migrate failure of 27 Oct 2026 (an invalid
``property_developer.json`` aborted ``bench migrate``) and the Data Import
failure "Founded In is mandatory for a new Developer.".
"""

import ast
import configparser
import datetime as dt
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "real_estate_crm_customs"
DOCTYPE_ROOT = APP / "real_estate_crm_customs" / "doctype"
DEVELOPER_JSON = DOCTYPE_ROOT / "property_developer" / "property_developer.json"
DEVELOPER_CONTROLLER = DOCTYPE_ROOT / "property_developer" / "property_developer.py"
CONVERSION_PATCH = APP / "patches" / "v16_0" / "convert_developer_founded_year_to_date.py"
PATCH_MODULE = "real_estate_crm_customs.patches.v16_0.convert_developer_founded_year_to_date"


def strict_json_load(path):
    """Parse like Frappe's orjson: reject NaN/Infinity and duplicate keys."""

    def reject_constant(value):
        raise ValueError(f"{path}: non-standard JSON constant {value}")

    def reject_duplicates(pairs):
        keys = [key for key, _value in pairs]
        duplicates = {key for key in keys if keys.count(key) > 1}
        if duplicates:
            raise ValueError(f"{path}: duplicate keys {sorted(duplicates)}")
        return dict(pairs)

    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicates,
    )


def load_function(path, name, namespace):
    module = ast.parse(Path(path).read_text())
    nodes = [
        node
        for node in module.body
        if (isinstance(node, ast.FunctionDef) and node.name == name)
        or (
            isinstance(node, ast.Assign)
            and all(isinstance(target, ast.Name) and target.id.isupper() for target in node.targets)
        )
    ]
    compiled = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(compiled)
    exec(compile(compiled, str(path), "exec"), namespace)
    return namespace[name]


class FakeValidationError(Exception):
    pass


class FakeFrappe:
    ValidationError = FakeValidationError

    def throw(self, message, exc=None):
        raise (exc or FakeValidationError)(message)


def load_developer_class():
    module = ast.parse(DEVELOPER_CONTROLLER.read_text())
    nodes = [
        node
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.Assign))
    ]
    compiled = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(compiled)

    class Document:
        def __init__(self, **values):
            self._new = values.pop("_new", True)
            self.__dict__.update(values)

        def is_new(self):
            return self._new

    def getdate(value=None):
        if value is None:
            return dt.date(2026, 10, 27)
        if isinstance(value, dt.date):
            return value
        return dt.date.fromisoformat(str(value))

    namespace = {
        "date": dt.date,
        "frappe": FakeFrappe(),
        "_": lambda text: text,
        "Document": Document,
        "getdate": getdate,
    }
    exec(compile(compiled, str(DEVELOPER_CONTROLLER), "exec"), namespace)
    return namespace["PropertyDeveloper"]


class Founder:
    def __init__(self, founder_name):
        self.founder_name = founder_name


class DoctypeJsonStrictParseTests(unittest.TestCase):
    def test_every_app_json_file_parses_strictly(self):
        files = sorted(APP.rglob("*.json"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=str(path.relative_to(ROOT))):
                strict_json_load(path)


class DeveloperFoundedInSchemaTests(unittest.TestCase):
    def setUp(self):
        schema = strict_json_load(DEVELOPER_JSON)
        self.schema = schema
        self.field = next(f for f in schema["fields"] if f["fieldname"] == "founded_year")

    def test_founded_in_is_an_optional_date(self):
        self.assertEqual(self.field["fieldtype"], "Date")
        self.assertEqual(self.field["label"], "Founded In")
        self.assertFalse(self.field.get("reqd"))

    def test_conversion_patch_runs_before_model_sync(self):
        parser = configparser.ConfigParser(allow_no_value=True, delimiters="\n")
        parser.optionxform = str
        parser.read(APP / "patches.txt")
        self.assertIn(PATCH_MODULE, parser["pre_model_sync"])
        self.assertNotIn(PATCH_MODULE, parser["post_model_sync"])

    def test_standard_developer_view_uses_date_column(self):
        module = ast.parse((APP / "install.py").read_text())
        views = next(
            ast.literal_eval(node.value)
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "REAL_ESTATE_STANDARD_VIEWS" for t in node.targets)
        )
        developer = next(view for view in views if view["dt"] == "Property Developer")
        column = next(col for col in developer["columns"] if col["key"] == "founded_year")
        self.assertEqual(column["type"], "Date")


class DeveloperControllerTests(unittest.TestCase):
    def setUp(self):
        self.Developer = load_developer_class()

    def test_new_developer_without_founded_in_is_accepted(self):
        doc = self.Developer(founded_year=None, founders=[Founder("A. Founder")])
        doc.validate()

    def test_valid_founded_date_is_accepted(self):
        doc = self.Developer(founded_year="2005-01-01", founders=[Founder("A. Founder")])
        doc.validate()

    def test_future_and_ancient_dates_are_rejected(self):
        for value in ("2027-01-01", "1699-12-31"):
            with self.subTest(value=value):
                doc = self.Developer(founded_year=value, founders=[Founder("A. Founder")])
                with self.assertRaises(FakeValidationError):
                    doc.validate()

    def test_founder_rules_are_unchanged(self):
        with self.assertRaises(FakeValidationError):
            self.Developer(founded_year=None, founders=[]).validate()
        with self.assertRaises(FakeValidationError):
            self.Developer(
                founded_year=None,
                founders=[Founder("Ali  Hassan"), Founder("ali hassan")],
            ).validate()


class FoundedYearConversionTests(unittest.TestCase):
    def setUp(self):
        self.convert = load_function(
            CONVERSION_PATCH, "year_to_date_text", {"re": re, "date": dt.date}
        )
        self.today = dt.date(2026, 10, 27)

    def test_whole_years_become_first_of_january(self):
        self.assertEqual(self.convert(2005, self.today), "2005-01-01")
        self.assertEqual(self.convert("1998", self.today), "1998-01-01")

    def test_existing_iso_dates_are_kept(self):
        self.assertEqual(self.convert("2010-06-15", self.today), "2010-06-15")

    def test_empty_zero_and_invalid_values_become_null(self):
        for value in (None, "", 0, "0", 1500, 2099, "abc", "2010-13-40", "2099-01-01"):
            with self.subTest(value=value):
                self.assertIsNone(self.convert(value, self.today))

    def test_patch_never_deletes_and_skips_date_columns(self):
        source = CONVERSION_PATCH.read_text()
        lowered = source.lower()
        for statement in ("delete from", "delete_doc", "drop column", "truncate"):
            self.assertNotIn(statement, lowered)
        self.assertNotIn("frappe.db.commit", source)
        self.assertIn('if "date" in column_type:', source)


if __name__ == "__main__":
    unittest.main()
