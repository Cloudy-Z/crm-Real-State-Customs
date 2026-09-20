import configparser
import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCTYPE_ROOT = ROOT / "real_estate_crm_customs" / "real_estate_crm_customs" / "doctype"
FIXTURE = ROOT / "real_estate_crm_customs" / "fixtures" / "custom_field.json"
HOOKS = ROOT / "real_estate_crm_customs" / "hooks.py"
INSTALL = ROOT / "real_estate_crm_customs" / "install.py"


def load_doctype(folder):
    return json.loads((DOCTYPE_ROOT / folder / f"{folder}.json").read_text())


def literal_assignment(path, name):
    module = ast.parse(path.read_text())
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} was not found in {path}")


class SchemaContractTests(unittest.TestCase):
    def test_every_custom_doctype_has_a_python_controller(self):
        for schema_path in DOCTYPE_ROOT.glob("*/*.json"):
            controller_path = schema_path.with_suffix(".py")
            self.assertTrue(
                controller_path.exists(),
                f"Missing Frappe controller: {controller_path.relative_to(ROOT)}",
            )

    def test_patches_file_uses_complete_frappe_v16_ini_format(self):
        parser = configparser.ConfigParser(allow_no_value=True, delimiters="\n")
        parser.optionxform = str
        parser.read(ROOT / "real_estate_crm_customs" / "patches.txt")
        self.assertEqual(
            parser.sections(),
            ["pre_model_sync", "post_model_sync"],
        )
        self.assertIn(
            "real_estate_crm_customs.patches.v0_0_1.setup_crm_lead_customizations",
            parser["pre_model_sync"],
        )
        self.assertIn(
            "real_estate_crm_customs.patches.v16_0.unify_real_estate_fields",
            parser["post_model_sync"],
        )

    def test_every_custom_doctype_field_order_is_valid(self):
        for path in DOCTYPE_ROOT.glob("*/*.json"):
            schema = json.loads(path.read_text())
            fields = [field["fieldname"] for field in schema.get("fields", [])]
            self.assertEqual(len(fields), len(set(fields)), f"Duplicate fieldname in {path}")
            self.assertEqual(
                set(schema.get("field_order", [])),
                set(fields),
                f"field_order mismatch in {path}",
            )

    def test_unit_uses_canonical_classification_and_financial_outputs(self):
        schema = load_doctype("real_estate_unit")
        fields = {field["fieldname"]: field for field in schema["fields"]}
        self.assertEqual(fields["inventory_type"]["options"].splitlines(), [
            "Rental",
            "Resale",
            "Primary",
            "International",
        ])
        self.assertEqual(fields["physical_unit_type"]["options"], "Real Estate Unit Type")
        self.assertEqual(fields["destination"]["options"], "Real Estate Destination")
        self.assertEqual(json.loads(fields["owner_lead"]["link_filters"]), {"party_type": "Seller"})
        for output in (
            "over_net",
            "down_payment",
            "property_tax",
            "commission",
            "total_price_net",
            "total_gross",
            "delivery_status",
        ):
            self.assertEqual(fields[output].get("read_only"), 1, output)
        self.assertEqual(fields["price"].get("hidden"), 1)
        self.assertEqual(fields["unit_type"].get("hidden"), 1)
        self.assertEqual(fields["legacy_property_code"].get("hidden"), 1)
        self.assertEqual(fields["property_location_note"].get("hidden"), 1)

    def test_compound_and_interest_use_destination_links(self):
        compound_fields = {
            field["fieldname"]: field for field in load_doctype("real_estate_project")["fields"]
        }
        self.assertEqual(compound_fields["destination"]["options"], "Real Estate Destination")
        self.assertEqual(compound_fields["location"].get("hidden"), 1)
        interest_fields = {
            field["fieldname"]: field for field in load_doctype("lead_interest")["fields"]
        }
        self.assertEqual(
            interest_fields["requested_destination"]["options"],
            "Real Estate Destination",
        )
        self.assertEqual(interest_fields["requested_area"].get("hidden"), 1)

    def test_lead_fixture_has_one_role_and_no_seller_property_shadows(self):
        records = json.loads(FIXTURE.read_text())
        lead_fields = {
            record["fieldname"]: record
            for record in records
            if record.get("dt") == "CRM Lead"
        }
        self.assertIn("party_type", lead_fields)
        self.assertEqual(lead_fields["party_type"]["options"].splitlines(), ["Buyer", "Seller"])
        self.assertEqual(lead_fields["preferred_destination"]["options"], "Real Estate Destination")
        self.assertEqual(lead_fields["preferred_unit_type"]["options"], "Real Estate Unit Type")
        stale = {
            "custom_type",
            "lead_type",
            "area_unit",
            "preferred_area",
            "seller_property_section",
            "property_title",
            "target_asking_price",
            "property_code",
            "location_reference",
            "seller_compound",
            "seller_developer",
            "seller_unit_type",
            "unit_area",
            "seller_finishing_type",
            "property_documents",
            "no_answer_first_call",
            "no_answer_second_call",
        }
        self.assertFalse(stale & set(lead_fields), stale & set(lead_fields))
        for field in lead_fields.values():
            self.assertNotIn(field.get("insert_after"), stale, field["fieldname"])

    def test_hook_fixture_filter_matches_custom_field_fixture(self):
        fixtures = literal_assignment(HOOKS, "fixtures")
        exported_names = {
            record["name"] for record in json.loads(FIXTURE.read_text())
        }
        requested_names = set()
        for fixture in fixtures:
            if not isinstance(fixture, dict) or fixture.get("dt") != "Custom Field":
                continue
            requested_names.update(fixture["filters"][0][2])
        self.assertEqual(requested_names, exported_names)

    def test_portal_real_estate_layout_fields_exist(self):
        layouts = literal_assignment(INSTALL, "REAL_ESTATE_FIELD_LAYOUTS")
        folder_by_doctype = {
            "Property Developer": "property_developer",
            "Real Estate Destination": "real_estate_destination",
            "Real Estate Project": "real_estate_project",
            "Real Estate Unit": "real_estate_unit",
        }
        for layout_name, settings in layouts.items():
            schema = load_doctype(folder_by_doctype[settings["doctype"]])
            valid = {field["fieldname"] for field in schema["fields"]}
            configured = {
                fieldname
                for section in settings["layout"]
                for column in section["columns"]
                for fieldname in column["fields"]
            }
            self.assertFalse(configured - valid, f"{layout_name}: {configured - valid}")


if __name__ == "__main__":
    unittest.main()
