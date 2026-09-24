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
API = ROOT / "real_estate_crm_customs" / "api.py"
INTEREST_WORKFLOW = ROOT / "real_estate_crm_customs" / "interest_workflow.py"
PARTY_ROLE_MIGRATION = ROOT / "real_estate_crm_customs" / "lead_role_migration.py"
PATCHES = ROOT / "real_estate_crm_customs" / "patches.txt"
LEAD_INTEREST_CONTROLLER = (
    DOCTYPE_ROOT / "lead_interest" / "lead_interest.py"
)
UNIFICATION_PATCH = (
    ROOT
    / "real_estate_crm_customs"
    / "patches"
    / "v16_0"
    / "unify_real_estate_fields.py"
)
LEGACY_PATCH = (
    ROOT
    / "real_estate_crm_customs"
    / "patches"
    / "v0_0_1"
    / "setup_crm_lead_customizations.py"
)


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
    def test_custom_field_insert_after_graph_is_acyclic(self):
        records = json.loads(FIXTURE.read_text())
        for doctype in {record.get("dt") for record in records}:
            fields = {
                record["fieldname"]: record
                for record in records
                if record.get("dt") == doctype
            }
            for start in fields:
                seen = set()
                current = start
                while current in fields:
                    self.assertNotIn(
                        current,
                        seen,
                        f"Circular insert_after chain in {doctype}: {start}",
                    )
                    seen.add(current)
                    current = fields[current].get("insert_after")

    def test_install_hooks_do_not_commit_or_create_untyped_quick_filters(self):
        source = INSTALL.read_text()
        self.assertNotIn("frappe.db.commit()", source)
        self.assertIn('{"dt": doctype, "type": "Quick Filters"}', source)
        self.assertIn('doc.type = "Quick Filters"', source)

    def test_pre_model_patch_does_not_run_schema_dependent_installer(self):
        source = LEGACY_PATCH.read_text()
        self.assertNotIn("after_install", source)
        self.assertNotIn("sync_real_estate_crm_defaults", source)

    def test_post_model_patch_does_not_assume_fixture_column_or_clear_owners(self):
        source = UNIFICATION_PATCH.read_text()
        self.assertIn('frappe.db.has_column("CRM Lead", "party_type")', source)
        self.assertNotIn('values["owner_lead"] = None', source)

    def test_interest_api_is_standalone_first(self):
        source = API.read_text()
        self.assertNotIn('doc.append("interested_in_units"', source)
        self.assertIn("create_interest as _create_standalone_interest", source)

    def test_fact_migration_preserves_legacy_source_rows(self):
        source = INTEREST_WORKFLOW.read_text()
        self.assertGreaterEqual(source.count("mirror_legacy=False"), 2)

    def test_superseded_requests_are_closed_consistently(self):
        workflow_source = INTEREST_WORKFLOW.read_text()
        controller_source = LEAD_INTEREST_CONTROLLER.read_text()
        self.assertIn('elif to_status in {"Cancelled", "Superseded"}', workflow_source)
        self.assertIn('"Superseded": "Cancelled"', controller_source)

    def test_all_link_filters_use_frappe_v16_four_value_rows(self):
        records = json.loads(FIXTURE.read_text())
        for schema_path in DOCTYPE_ROOT.glob("*/*.json"):
            records.extend(json.loads(schema_path.read_text()).get("fields", []))
        for record in records:
            raw_filters = record.get("link_filters")
            if not raw_filters:
                continue
            filters = json.loads(raw_filters)
            self.assertIsInstance(filters, list, record.get("fieldname"))
            self.assertTrue(filters, record.get("fieldname"))
            for filter_row in filters:
                self.assertIsInstance(filter_row, list, record.get("fieldname"))
                self.assertEqual(len(filter_row), 4, record.get("fieldname"))

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
        self.assertEqual(
            json.loads(fields["owner_lead"]["link_filters"]),
            [["CRM Lead", "party_type", "=", "Seller"]],
        )
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
        self.assertEqual(lead_fields["interested_in_units"].get("read_only"), 1)
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

    def test_party_type_is_the_only_runtime_lead_role_source(self):
        api_source = API.read_text()
        self.assertIn("def _enforce_canonical_party_role", api_source)
        self.assertNotIn("custom_type", api_source)
        self.assertNotIn("lead_type", api_source)
        self.assertIn('"Unit Scheduled Showing"', api_source)
        self.assertIn('"Lead Action Execution"', api_source)
        self.assertIn("def get_real_estate_doctype_permissions", api_source)

        migration_source = PARTY_ROLE_MIGRATION.read_text()
        self.assertIn('LEGACY_ROLE_FIELDS = ("custom_type", "lead_type")', migration_source)
        self.assertIn("remove_legacy_role_metadata()", migration_source)
        self.assertIn(
            "real_estate_crm_customs.patches.v16_0.remove_legacy_lead_role_fields",
            PATCHES.read_text(),
        )
        self.assertIn(
            "real_estate_crm_customs.patches.v16_0.repair_historical_lead_roles",
            PATCHES.read_text(),
        )
        self.assertIn('"Real Estate Unit", "owner_lead"', migration_source)
        self.assertIn('"Lead Interest", "lead"', migration_source)
        self.assertIn("resolve_party_role(", migration_source)
        self.assertIn('"CRM View Settings"', migration_source)
        self.assertIn('"Buyers" if role == "Buyer" else "Sellers"', migration_source)
        self.assertIn("def _clear_legacy_role_values", migration_source)
        self.assertIn("_clear_legacy_role_values()", migration_source)

        showing_controller = (
            DOCTYPE_ROOT
            / "unit_scheduled_showing"
            / "unit_scheduled_showing.py"
        ).read_text()
        self.assertIn('party_type != "Buyer"', showing_controller)

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
            "Real Estate Unit Type": "real_estate_unit_type",
            "Real Estate Amenity": "real_estate_amenity",
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

        layout_keys = {
            (settings["doctype"], settings["type"]) for settings in layouts.values()
        }
        expected_masters = set(folder_by_doctype)
        self.assertTrue(
            {(doctype, "Quick Entry") for doctype in expected_masters}
            <= layout_keys
        )
        self.assertTrue(
            {(doctype, "Data Fields") for doctype in expected_masters}
            <= layout_keys
        )

    def test_all_real_estate_masters_have_standard_views_and_filters(self):
        expected = {
            "Real Estate Unit",
            "Real Estate Project",
            "Property Developer",
            "Real Estate Destination",
            "Real Estate Unit Type",
            "Real Estate Amenity",
        }
        views = literal_assignment(INSTALL, "REAL_ESTATE_STANDARD_VIEWS")
        filters = literal_assignment(INSTALL, "REAL_ESTATE_QUICK_FILTERS")
        self.assertTrue(expected <= {view["dt"] for view in views})
        self.assertTrue(expected <= set(filters))


if __name__ == "__main__":
    unittest.main()
