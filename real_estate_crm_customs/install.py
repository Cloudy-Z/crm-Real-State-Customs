import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from real_estate_crm_customs.master_data import ensure_default_unit_types


def _fixture_custom_field_definitions(*doctypes):
    """Load stable Custom Field metadata from the exported fixture source of truth."""
    fixture_path = frappe.get_app_path("real_estate_crm_customs", "fixtures", "custom_field.json")
    with open(fixture_path, encoding="utf-8") as fixture_file:
        records = json.load(fixture_file)
    requested = set(doctypes)
    grouped = {}
    for record in records:
        doctype = record.get("dt")
        if doctype not in requested:
            continue
        field = {
            key: value
            for key, value in record.items()
            if key not in {"doctype", "dt", "name", "owner", "creation", "modified", "modified_by"}
        }
        grouped.setdefault(doctype, []).append(field)
    return grouped


LEAD_CONTACT_LAYOUT_FIELDS = [
    "lead_name",
    "mobile_no",
    "whatsapp_number",
    "job_title",
    "selection_tier",
]

LEAD_EVENT_LAYOUT_FIELDS = []  # Deprecated: event tracking now uses native Events

LEAD_REAL_ESTATE_LAYOUT_FIELDS = [
    "party_type",
    "buyer_budget",
    "preferred_unit_type",
    "preferred_destination",
    "preferred_developer",
    "preferred_compound",
    "preferred_finishing_type",
    "preferred_delivery_time",
]

LEAD_CALL_FLAGS_LAYOUT_FIELDS = [
    "no_answer_consecutive_count",
    "no_answer_total_count",
    "last_call_outcome",
    "last_call_at",
]

DEFAULT_CRM_LEAD_SIDE_PANEL_LAYOUT = [
    {
        "label": "Contact & Details",
        "name": "contact_details_section",
        "opened": True,
        "columns": [
            {
                "name": "column_contact_details",
                "fields": [
                    "first_name",
                    "last_name",
                    "email",
                    "mobile_no",
                    "whatsapp_number",
                    "lead_owner",
                    "source",
                    "job_title",
                    "party_type",
                    "selection_tier",
                ],
            }
        ],
    },
]

REAL_ESTATE_FIELD_LAYOUTS = {
    "Property Developer-Quick Entry": {
        "doctype": "Property Developer",
        "type": "Quick Entry",
        "layout": [
            {
                "label": "Developer Information",
                "name": "developer_section",
                "columns": [
                    {"name": "column_developer_a", "fields": ["developer_name", "founded_year"]},
                    {"name": "column_developer_b", "fields": ["company_registration", "founders"]},
                ],
            }
        ],
    },
    "Real Estate Destination-Quick Entry": {
        "doctype": "Real Estate Destination",
        "type": "Quick Entry",
        "layout": [
            {
                "label": "Destination Information",
                "name": "destination_section",
                "columns": [
                    {"name": "column_destination_a", "fields": ["destination_name", "destination_code"]},
                    {"name": "column_destination_b", "fields": ["is_active", "description"]},
                ],
            }
        ],
    },
    "Real Estate Project-Quick Entry": {
        "doctype": "Real Estate Project",
        "type": "Quick Entry",
        "layout": [
            {
                "label": "Compound Information",
                "name": "compound_section",
                "columns": [
                    {"name": "column_compound_a", "fields": ["project_name", "developer", "destination"]},
                    {"name": "column_compound_b", "fields": ["status", "compound_area", "compound_area_unit"]},
                    {"name": "column_compound_c", "fields": ["available_unit_types", "phases", "amenities", "master_plan", "description"]},
                ],
            }
        ],
    },
    "Real Estate Unit-Quick Entry": {
        "doctype": "Real Estate Unit",
        "type": "Quick Entry",
        "layout": [
            {
                "label": "General Information",
                "name": "unit_general_section",
                "columns": [
                    {"name": "column_unit_a", "fields": ["unit_number", "project", "destination", "developer", "inventory_type", "physical_unit_type"]},
                    {"name": "column_unit_b", "fields": ["floor", "number_of_floors", "bua", "area_uom", "land_area", "garden_area", "roof_area", "terrace_area"]},
                    {"name": "column_unit_c", "fields": ["bedrooms", "bathrooms", "has_nanny_room", "has_driver_room", "has_storage_room", "has_parking", "finishing_type", "delivery_date", "delivery_status", "status", "owner_lead"]},
                ],
            },
            {
                "label": "Financial Information",
                "name": "unit_financial_section",
                "columns": [
                    {"name": "column_unit_financial_a", "fields": ["paid", "over_price", "over_is_gross", "over_net", "down_payment", "remaining"]},
                    {"name": "column_unit_financial_b", "fields": ["property_tax", "commission", "total_price_net", "total_gross", "maintenance", "rental_monthly_rate", "rental_daily_rate"]},
                ],
            },
        ],
    },
    "Real Estate Unit-Side Panel": {
        "doctype": "Real Estate Unit",
        "type": "Side Panel",
        "layout": [
            {
                "label": "Unit Summary",
                "name": "unit_summary_section",
                "opened": True,
                "columns": [
                    {"name": "column_unit_summary", "fields": ["unit_number", "sku", "project", "destination", "developer", "inventory_type", "physical_unit_type", "status", "delivery_status", "total_gross", "owner_lead"]}
                ],
            }
        ],
    },
    "Real Estate Unit-Data Fields": {
        "doctype": "Real Estate Unit",
        "type": "Data Fields",
        "layout": [
            {
                "label": "General Information",
                "name": "unit_general_data_section",
                "opened": True,
                "columns": [
                    {"name": "column_unit_data_a", "fields": ["unit_number", "sku", "project", "destination", "developer", "inventory_type", "physical_unit_type"]},
                    {"name": "column_unit_data_b", "fields": ["floor", "number_of_floors", "bua", "area_uom", "land_area", "garden_area", "roof_area", "terrace_area"]},
                    {"name": "column_unit_data_c", "fields": ["bedrooms", "bathrooms", "has_nanny_room", "has_driver_room", "has_storage_room", "has_parking", "finishing_type", "delivery_date", "delivery_status", "status", "owner_lead"]},
                ],
            },
            {
                "label": "Financial Information",
                "name": "unit_financial_data_section",
                "opened": True,
                "columns": [
                    {"name": "column_unit_financial_data_a", "fields": ["paid", "over_price", "over_is_gross", "over_net", "down_payment", "remaining", "maintenance"]},
                    {"name": "column_unit_financial_data_b", "fields": ["property_tax", "commission", "total_price_net", "total_gross", "rental_monthly_rate", "rental_daily_rate"]},
                ],
            },
        ],
    },
    "Real Estate Project-Side Panel": {
        "doctype": "Real Estate Project",
        "type": "Side Panel",
        "layout": [
            {
                "label": "Compound Summary",
                "name": "compound_summary_section",
                "opened": True,
                "columns": [
                    {"name": "column_compound_summary", "fields": ["project_name", "developer", "destination", "status", "compound_area", "compound_area_unit"]}
                ],
            }
        ],
    },
    "Real Estate Project-Data Fields": {
        "doctype": "Real Estate Project",
        "type": "Data Fields",
        "layout": [
            {
                "label": "Compound Information",
                "name": "compound_data_section",
                "opened": True,
                "columns": [
                    {"name": "column_compound_data_a", "fields": ["project_name", "developer", "destination", "status"]},
                    {"name": "column_compound_data_b", "fields": ["compound_area", "compound_area_unit", "master_plan", "description"]},
                    {"name": "column_compound_data_c", "fields": ["phases", "available_unit_types", "amenities"]},
                ],
            }
        ],
    },
}

REAL_ESTATE_STANDARD_VIEWS = [
    {
        "label": "Property Developers",
        "dt": "Property Developer",
        "route_name": "Property Developers",
        "icon": "building",
        "columns": [
            {"label": "Developer Name", "type": "Data", "key": "developer_name", "width": "16rem"},
            {"label": "Founded In", "type": "Int", "key": "founded_year", "width": "10rem"},
            {"label": "Company Registration", "type": "Data", "key": "company_registration", "width": "16rem"},
            {"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
        ],
        "rows": ["name", "developer_name", "founded_year", "company_registration", "modified"],
    },
    {
        "label": "Destinations",
        "dt": "Real Estate Destination",
        "route_name": "Real Estate Destinations",
        "icon": "map-pin",
        "columns": [
            {"label": "Destination", "type": "Data", "key": "destination_name", "width": "16rem"},
            {"label": "Code", "type": "Data", "key": "destination_code", "width": "10rem"},
            {"label": "Active", "type": "Check", "key": "is_active", "width": "8rem"},
            {"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
        ],
        "rows": ["name", "destination_name", "destination_code", "is_active", "modified"],
    },
    {
        "label": "Compounds",
        "dt": "Real Estate Project",
        "route_name": "Real Estate Projects",
        "icon": "building-2",
        "columns": [
            {"label": "Compound Name", "type": "Data", "key": "project_name", "width": "14rem"},
            {"label": "Destination", "type": "Link", "key": "destination", "width": "14rem"},
            {"label": "Developer", "type": "Link", "key": "developer", "width": "14rem"},
            {"label": "Status", "type": "Select", "key": "status", "width": "10rem"},
            {"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
        ],
        "rows": ["name", "project_name", "destination", "developer", "status", "modified"],
    },
    {
        "label": "Real Estate Units",
        "dt": "Real Estate Unit",
        "route_name": "Real Estate Units",
        "icon": "home",
        "columns": [
            {"label": "Unit Number", "type": "Data", "key": "unit_number", "width": "10rem"},
            {"label": "Compound", "type": "Link", "key": "project", "width": "14rem"},
            {"label": "Destination", "type": "Link", "key": "destination", "width": "14rem"},
            {"label": "Inventory Type", "type": "Select", "key": "inventory_type", "width": "10rem"},
            {"label": "Unit Type", "type": "Link", "key": "physical_unit_type", "width": "10rem"},
            {"label": "Availability", "type": "Select", "key": "status", "width": "10rem"},
            {"label": "Total Gross", "type": "Currency", "key": "total_gross", "width": "10rem"},
            {"label": "Seller Owner", "type": "Link", "key": "owner_lead", "width": "14rem"},
            {"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
        ],
        "rows": ["name", "unit_number", "sku", "project", "destination", "developer", "inventory_type", "physical_unit_type", "status", "total_gross", "owner_lead", "modified"],
    },
]

REAL_ESTATE_QUICK_FILTERS = {
    "CRM Lead": ["lead_name", "email", "status", "source", "party_type", "lead_owner", "buyer_budget", "preferred_destination", "preferred_unit_type", "preferred_developer", "preferred_compound", "selection_tier"],
    "Real Estate Unit": ["unit_number", "sku", "project", "destination", "developer", "inventory_type", "physical_unit_type", "floor", "finishing_type", "status", "delivery_status", "owner_lead"],
    "Property Developer": ["developer_name", "founded_year", "company_registration"],
    "Real Estate Project": ["project_name", "destination", "developer", "status"],
    "Real Estate Destination": ["destination_name", "destination_code", "is_active"],
}


def after_install():
    sync_real_estate_crm_defaults()


def after_migrate():
    sync_real_estate_crm_defaults()


def sync_real_estate_crm_defaults():
    ensure_module_def()
    ensure_default_unit_types()
    setup_crm_lead_custom_fields()
    setup_user_agent_custom_fields()
    ensure_real_estate_lead_statuses()
    enforce_crm_lead_phone_mandatory()
    enforce_crm_lead_status_read_only()
    setup_real_estate_client_scripts()
    setup_crm_portal_defaults()
    migrate_standalone_lead_interests()
    frappe.db.commit()


def migrate_standalone_lead_interests():
    """Idempotently promote legacy child rows and action JSON scopes after schema sync."""
    if not frappe.db.exists("DocType", "Lead Interest"):
        return
    from real_estate_crm_customs.interest_workflow import run_full_migration

    run_full_migration()


def ensure_module_def():
    if not frappe.db.exists("Module Def", "Real Estate CRM Customs"):
        doc = frappe.get_doc(
            {
                "doctype": "Module Def",
                "module_name": "Real Estate CRM Customs",
                "app_name": "real_estate_crm_customs",
                "custom": 0,
            }
        )
        doc.insert(ignore_permissions=True)


def setup_crm_lead_custom_fields():
    if not frappe.db.exists("DocType", "CRM Lead"):
        frappe.throw("CRM Lead DocType was not found. Install Frappe CRM before installing this custom app.")

    custom_fields = _fixture_custom_field_definitions("CRM Lead")
    _promote_varchar_custom_field_to_link(
        "CRM Lead",
        "preferred_unit_type",
        "Real Estate Unit Type",
    )
    create_custom_fields(custom_fields, update=True)
    _deprecate_custom_field_metadata(
        "CRM Lead",
        (
            "custom_type",
            "lead_type",
            "mobile_country_code",
            "whatsapp_country_code",
            "no_answer_first_call",
            "no_answer_second_call",
            "is_interested",
            "is_not_interested",
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
        ),
    )
    frappe.clear_cache(doctype="CRM Lead")


def _promote_varchar_custom_field_to_link(doctype, fieldname, options):
    """Change metadata only for a varchar-backed Data/Select/Link field; preserve every value."""
    custom_field_name = f"{doctype}-{fieldname}"
    if not frappe.db.exists("Custom Field", custom_field_name):
        return
    fieldtype = frappe.db.get_value("Custom Field", custom_field_name, "fieldtype")
    if fieldtype not in {"Data", "Select", "Link"}:
        frappe.throw(
            f"Cannot safely migrate {custom_field_name} from {fieldtype} to Link without an explicit data migration."
        )
    frappe.db.set_value(
        "Custom Field",
        custom_field_name,
        {"fieldtype": "Link", "options": options},
        update_modified=False,
    )


def _deprecate_custom_field_metadata(doctype, fieldnames):
    """Hide legacy metadata without deleting its column or historical values."""
    for fieldname in fieldnames:
        cf_name = f"{doctype}-{fieldname}"
        if not frappe.db.exists("Custom Field", cf_name):
            if frappe.get_meta(doctype).has_field(fieldname):
                make_property_setter(doctype, fieldname, "hidden", "1", "Check")
                make_property_setter(doctype, fieldname, "read_only", "1", "Check")
            continue
        custom_field = frappe.get_doc("Custom Field", cf_name)
        custom_field.hidden = 1
        custom_field.read_only = 1
        custom_field.in_list_view = 0
        custom_field.in_standard_filter = 0
        custom_field.description = "Deprecated compatibility field; canonical data has been migrated where safe."
        custom_field.save(ignore_permissions=True)


def setup_user_agent_custom_fields():
    if not frappe.db.exists("DocType", "User"):
        return

    create_custom_fields(_fixture_custom_field_definitions("User"), update=True)
    frappe.clear_cache(doctype="User")


def ensure_real_estate_lead_statuses():
    if not frappe.db.exists("DocType", "CRM Lead Status"):
        return

    statuses = [
        {"lead_status": "New", "type": "Open", "color": "gray", "position": 5},
        {"lead_status": "Fresh Lead", "type": "Open", "color": "blue", "position": 10},
        {"lead_status": "Requested", "type": "Ongoing", "color": "orange", "position": 20},
        {"lead_status": "Offer Sent", "type": "Ongoing", "color": "blue", "position": 30},
        {"lead_status": "Negotiating", "type": "Ongoing", "color": "yellow", "position": 40},
        {"lead_status": "Offer Selected", "type": "Ongoing", "color": "green", "position": 50},
    ]
    for status in statuses:
        if frappe.db.exists("CRM Lead Status", status["lead_status"]):
            continue
        doc = frappe.get_doc({"doctype": "CRM Lead Status", **status})
        doc.insert(ignore_permissions=True)


def enforce_crm_lead_phone_mandatory():
    """Ensure phone fields are correctly configured.

    This function is safe for both fresh installs and existing site upgrades:
    - Fresh install: only sets reqd=0 Property Setter (cleanup steps are no-ops)
    - Existing site: cleans up legacy Property Setters, indexes, and columns
    """

    # --- Legacy cleanup (no-ops on fresh install) ---
    # Remove any Property Setters that conflict with Phone fieldtype defaults
    legacy_ps_count = frappe.db.sql("""
        SELECT COUNT(*) FROM `tabProperty Setter`
        WHERE doc_type = 'CRM Lead'
        AND field_name IN ('phone', 'mobile_no')
        AND property IN ('reqd', 'hidden', 'unique', 'length')
    """)[0][0]
    if legacy_ps_count:
        frappe.db.sql("""
            DELETE FROM `tabProperty Setter`
            WHERE doc_type = 'CRM Lead'
            AND field_name IN ('phone', 'mobile_no')
            AND property IN ('reqd', 'hidden', 'unique', 'length')
        """)
        frappe.db.commit()

    # Drop unique index if it exists (Phone fieldtype cannot be unique)
    try:
        indexes = frappe.db.sql("""
            SELECT INDEX_NAME FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'tabCRM Lead'
            AND COLUMN_NAME = 'mobile_no'
            AND NON_UNIQUE = 0
        """, as_dict=True)
        for idx in indexes:
            frappe.db.sql_ddl(f"ALTER TABLE `tabCRM Lead` DROP INDEX `{idx['INDEX_NAME']}`")
    except Exception:
        pass

    # Ensure column is varchar(140) — Phone fieldtype default
    # On fresh install, Frappe creates it correctly; on upgrade, it may be varchar(11)
    try:
        col_info = frappe.db.sql("""
            SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'tabCRM Lead'
            AND COLUMN_NAME = 'mobile_no'
        """, as_dict=True)
        if col_info and col_info[0].get('CHARACTER_MAXIMUM_LENGTH', 140) < 140:
            frappe.db.sql_ddl("ALTER TABLE `tabCRM Lead` MODIFY `mobile_no` varchar(140) DEFAULT NULL")
    except Exception:
        pass

    # Same for whatsapp_number
    try:
        if frappe.db.has_column("CRM Lead", "whatsapp_number"):
            col_info = frappe.db.sql("""
                SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = 'tabCRM Lead'
                AND COLUMN_NAME = 'whatsapp_number'
            """, as_dict=True)
            if col_info and col_info[0].get('CHARACTER_MAXIMUM_LENGTH', 140) < 140:
                frappe.db.sql_ddl("ALTER TABLE `tabCRM Lead` MODIFY `whatsapp_number` varchar(140) DEFAULT NULL")
    except Exception:
        pass

    # --- Standard setup (runs on both fresh and upgrade) ---
    # Ensure mobile_no is explicitly not mandatory (client script handles validation)
    make_property_setter("CRM Lead", "mobile_no", "reqd", "0", "Check")
    if frappe.get_meta("CRM Lead").has_field("phone"):
        make_property_setter("CRM Lead", "phone", "hidden", "1", "Check")
        make_property_setter("CRM Lead", "phone", "reqd", "0", "Check")

    # Clear DocType cache so the schema changes take effect immediately
    frappe.clear_cache(doctype="CRM Lead")


def enforce_crm_lead_status_read_only():
    """Lead status is changed only by workflow APIs, never by direct form edits."""
    make_property_setter("CRM Lead", "status", "read_only", "1", "Check")
    frappe.clear_cache(doctype="CRM Lead")


def make_property_setter(doc_type, field_name, property_name, value, property_type):
    filters = {
        "doc_type": doc_type,
        "field_name": field_name,
        "property": property_name,
    }
    existing_name = frappe.db.get_value("Property Setter", filters, "name")
    if existing_name:
        doc = frappe.get_doc("Property Setter", existing_name)
    else:
        doc = frappe.new_doc("Property Setter")
        doc.doc_type = doc_type
        doc.field_name = field_name
        doc.doctype_or_field = "DocField"
        doc.property = property_name
    doc.value = value
    doc.property_type = property_type
    doc.save(ignore_permissions=True)
    frappe.db.commit()


def setup_real_estate_client_scripts():
    if not frappe.db.exists("DocType", "Client Script"):
        return
    ensure_client_script("Real Estate Unit Defaults", "Real Estate Unit", REAL_ESTATE_UNIT_DEFAULTS_SCRIPT)
    ensure_client_script("CRM Lead Real Estate Validation and Unit Assignment", "CRM Lead", CRM_LEAD_PHONE_AND_ASSIGN_SCRIPT)


def ensure_client_script(script_name, dt, script):
    if frappe.db.exists("Client Script", script_name):
        doc = frappe.get_doc("Client Script", script_name)
    else:
        doc = frappe.new_doc("Client Script")
        doc.name = script_name
        doc.dt = dt
    doc.enabled = 1
    doc.script = script
    doc.save(ignore_permissions=True)


REAL_ESTATE_UNIT_DEFAULTS_SCRIPT = r"""
frappe.ui.form.on('Real Estate Unit', {
    setup(frm) {
        frm.set_query('project', () => ({ filters: {} }));
        frm.set_query('owner_lead', () => ({ filters: { party_type: 'Seller' } }));
        frm.set_query('physical_unit_type', () => ({ filters: { is_active: 1 } }));
    },
    onload(frm) {
        if (frm.is_new()) {
            if (!frm.doc.status) {
                frm.set_value('status', 'Available');
            }
            if (!frm.doc.inventory_type) {
                frm.set_value('inventory_type', 'Primary');
            }
        }
    },
    refresh(frm) {
        if (frm.is_new()) {
            frm.set_df_property('status', 'default', 'Available');
        }
    },
});
"""


CRM_LEAD_PHONE_AND_ASSIGN_SCRIPT = r"""
frappe.ui.form.on('CRM Lead', {
    refresh(frm) {
        // Do NOT set mobile_no or phone as individually mandatory.
        // Validation below ensures at least one phone field is filled.

        if (!frm.is_new() && frm.doc.party_type === 'Seller') {
            frm.add_custom_button(__('Add Property'), () => {
                const addDialog = new frappe.ui.Dialog({
                    title: __('Add Seller Property'),
                    fields: [
                        {
                            fieldname: 'project',
                            fieldtype: 'Link',
                            label: __('Compound'),
                            options: 'Real Estate Project',
                            reqd: 1,
                        },
                        {
                            fieldname: 'unit_number',
                            fieldtype: 'Data',
                            label: __('Unit Number'),
                            reqd: 1,
                        },
                        {
                            fieldname: 'physical_unit_type',
                            fieldtype: 'Link',
                            label: __('Unit Type'),
                            options: 'Real Estate Unit Type',
                            reqd: 1,
                        },
                        {
                            fieldname: 'finishing_type',
                            fieldtype: 'Select',
                            label: __('Finishing Type'),
                            options: '\nCore & Shell\nSemi-Finished\nFully Finished\nUltra Super Lux',
                            reqd: 1,
                        },
                        {
                            fieldname: 'delivery_date',
                            fieldtype: 'Date',
                            label: __('Delivery Date'),
                            reqd: 1,
                        },
                        {
                            fieldname: 'bedrooms',
                            fieldtype: 'Int',
                            label: __('Bedrooms'),
                            default: 0,
                        },
                        {
                            fieldname: 'bathrooms',
                            fieldtype: 'Int',
                            label: __('Bathrooms'),
                            default: 0,
                        },
                        {
                            fieldname: 'bua',
                            fieldtype: 'Float',
                            label: __('BUA'),
                        },
                        {
                            fieldname: 'paid',
                            fieldtype: 'Currency',
                            label: __('Paid'),
                        },
                        {
                            fieldname: 'over_price',
                            fieldtype: 'Currency',
                            label: __('Over Price'),
                        },
                        {
                            fieldname: 'over_is_gross',
                            fieldtype: 'Check',
                            label: __('Over Is Gross'),
                        },
                        {
                            fieldname: 'remaining',
                            fieldtype: 'Currency',
                            label: __('Remaining'),
                        },
                    ],
                    primary_action_label: __('Create Property'),
                    primary_action(values) {
                        frappe.call({
                            method: 'real_estate_crm_customs.api.create_resale_unit',
                            args: {
                                owner_lead: frm.doc.name,
                                project: values.project,
                                unit_number: values.unit_number,
                                physical_unit_type: values.physical_unit_type,
                                finishing_type: values.finishing_type,
                                delivery_date: values.delivery_date,
                                bedrooms: values.bedrooms || 0,
                                bathrooms: values.bathrooms || 0,
                                bua: values.bua || null,
                                paid: values.paid,
                                over_price: values.over_price,
                                over_is_gross: values.over_is_gross,
                                remaining: values.remaining,
                            },
                            callback() {
                                frm.reload_doc();
                                frappe.msgprint(__('Seller property created successfully.'));
                                addDialog.hide();
                            },
                        });
                    },
                });
                addDialog.show();
            }, __('Actions'));

            frm.add_custom_button(__('Assign Property Unit'), () => {
                const dialog = new frappe.ui.Dialog({
                    title: __('Assign Property Unit'),
                    fields: [
                        {
                            fieldname: 'unit',
                            fieldtype: 'Link',
                            label: __('Available Unit'),
                            options: 'Real Estate Unit',
                            reqd: 1,
                            get_query() {
                                return { filters: { status: 'Available' } };
                            },
                        },
                    ],
                    primary_action_label: __('Assign'),
                    primary_action(values) {
                        frappe.call({
                            method: 'real_estate_crm_customs.api.assign_property_unit_to_seller',
                            args: {
                                lead: frm.doc.name,
                                unit: values.unit,
                            },
                            callback() {
                                frm.reload_doc();
                                frappe.msgprint(__('Property unit {0} assigned to this seller lead.', [values.unit]));
                                dialog.hide();
                            },
                        });
                    },
                });
                dialog.show();
            }, __('Actions'));
        }
    },
    validate(frm) {
        // At least one of mobile_no or whatsapp_number must be filled
        const mobile = (frm.doc.mobile_no || '').trim();
        const whatsapp = (frm.doc.whatsapp_number || '').trim();
        if (!mobile && !whatsapp) {
            frappe.msgprint(__('At least one contact number is required: Mobile No or WhatsApp Number.'));
            frappe.validated = false;
            return;
        }
    },
});
"""


def setup_crm_portal_defaults():
    if not frappe.db.exists("DocType", "CRM Fields Layout"):
        return

    ensure_lead_layouts_include_real_estate_fields()
    ensure_real_estate_layouts()
    ensure_real_estate_standard_views()
    ensure_real_estate_quick_filters()


# Minimal Quick Entry layout for real estate lead creation
LEAD_QUICK_ENTRY_LAYOUT = json.dumps([
    {
        "name": "basic_info_section",
        "columns": [
            {"name": "col_name", "fields": ["first_name", "last_name"]},
            {"name": "col_type", "fields": ["party_type", "status"]},
        ],
    },
    {
        "name": "contact_section",
        "columns": [
            {"name": "col_phone", "fields": ["mobile_no", "whatsapp_number"]},
            {"name": "col_email", "fields": ["email"]},
        ],
    },
    {
        "name": "lead_details_section",
        "columns": [
            {"name": "col_source", "fields": ["source", "lead_owner"]},
            {"name": "col_tier", "fields": ["selection_tier"]},
        ],
    },
])


def ensure_lead_layouts_include_real_estate_fields():
    # Overwrite Quick Entry with clean real estate layout
    overwrite_quick_entry_layout()
    ensure_crm_lead_main_form_sections()
    reset_crm_lead_side_panel_to_default()
    hide_irrelevant_upstream_fields()


def overwrite_quick_entry_layout():
    """Replace the default B2B Quick Entry layout with a minimal real estate version."""
    layout_name = "CRM Lead-Quick Entry"
    if frappe.db.exists("CRM Fields Layout", layout_name):
        doc = frappe.get_doc("CRM Fields Layout", layout_name)
        doc.layout = LEAD_QUICK_ENTRY_LAYOUT
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.new_doc("CRM Fields Layout")
        doc.type = "Quick Entry"
        doc.dt = "CRM Lead"
        doc.layout = LEAD_QUICK_ENTRY_LAYOUT
        doc.insert(ignore_permissions=True)


def hide_irrelevant_upstream_fields():
    """Hide B2B fields that are irrelevant to real estate workflow."""
    fields_to_hide = [
        "organization", "no_of_employees", "annual_revenue",
        "industry", "website", "territory", "middle_name",
        "gender", "salutation",
        "facebook_lead_id", "facebook_form_id",
    ]
    for fieldname in fields_to_hide:
        if not frappe.db.exists("DocField", {"parent": "CRM Lead", "fieldname": fieldname}):
            continue
        filters = {
            "doc_type": "CRM Lead",
            "field_name": fieldname,
            "property": "hidden",
        }
        if frappe.db.exists("Property Setter", filters):
            doc = frappe.get_doc("Property Setter", filters)
        else:
            doc = frappe.new_doc("Property Setter")
            doc.doc_type = "CRM Lead"
            doc.field_name = fieldname
            doc.doctype_or_field = "DocField"
            doc.property = "hidden"
        doc.value = "1"
        doc.property_type = "Check"
        doc.save(ignore_permissions=True)


def ensure_crm_lead_main_form_sections():
    """Keep the document sections in the main Lead form, not in the right sidebar."""

    # Section 1: Contact & Person merged with Lead Owner, Source, Job Title
    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Data Fields",
        section_name="contact_identity_data_fields_section",
        section_label="Contact & Details",
        column_name="column_contact_identity_data_fields",
        fields=LEAD_CONTACT_LAYOUT_FIELDS,
        section_opened=True,
        section_collapsible=True,
    )
    # Section 2: Interest preferences (buyer/seller details without flags)
    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Data Fields",
        section_name="real_estate_data_fields_section",
        section_label="Interest & Property Details",
        column_name="column_real_estate_data_fields",
        fields=LEAD_REAL_ESTATE_LAYOUT_FIELDS,
        section_opened=False,
        section_collapsible=True,
    )
    # Section 3: Call flags and action outcomes
    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Data Fields",
        section_name="call_flags_data_fields_section",
        section_label="Call Flags & Action Status",
        column_name="column_call_flags_data_fields",
        fields=LEAD_CALL_FLAGS_LAYOUT_FIELDS,
        section_opened=False,
        section_collapsible=True,
    )
    # Section 4: Task execution and milestone deadlines
    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Data Fields",
        section_name="task_execution_data_fields_section",
        section_label="Task Execution & Milestone Deadlines",
        column_name="column_task_execution_data_fields",
        fields=LEAD_EVENT_LAYOUT_FIELDS,
        section_opened=False,
        section_collapsible=True,
    )


def reset_crm_lead_side_panel_to_default():
    """Restore the Lead right sidebar to the upstream CRM default layout."""

    layout_doc = get_or_create_fields_layout("CRM Lead", "Side Panel")
    default_layout = json.dumps(DEFAULT_CRM_LEAD_SIDE_PANEL_LAYOUT)
    if layout_doc.layout != default_layout:
        layout_doc.layout = default_layout
        layout_doc.save(ignore_permissions=True)


def append_fields_to_layout(
    doctype,
    layout_type,
    section_name,
    column_name,
    fields,
    section_label=None,
    section_opened=True,
    section_collapsible=None,
):
    layout_doc = get_or_create_fields_layout(doctype, layout_type)
    layout = parse_layout(layout_doc.layout)

    section = find_layout_section(layout, section_name)
    if not section:
        section = {"name": section_name, "opened": section_opened, "columns": []}
        layout.append(section)

    if section_label:
        section["label"] = section_label
    section["opened"] = section_opened
    if section_collapsible is not None:
        section["collapsible"] = section_collapsible

    columns = section.setdefault("columns", [])
    column = None
    for existing_column in columns:
        if existing_column.get("name") == column_name:
            column = existing_column
            break

    if not column:
        column = {"name": column_name, "fields": []}
        columns.append(column)

    existing_fields = set()
    for existing_section in layout:
        for existing_column in existing_section.get("columns", []):
            existing_fields.update(existing_column.get("fields", []))

    original_layout = parse_layout(layout_doc.layout)
    changed = original_layout != layout
    for field in fields:
        if field not in existing_fields:
            column.setdefault("fields", []).append(field)
            existing_fields.add(field)
            changed = True

    if changed:
        layout_doc.layout = json.dumps(layout)
        layout_doc.save(ignore_permissions=True)


def get_or_create_fields_layout(doctype, layout_type):
    name = f"{doctype}-{layout_type}"
    if frappe.db.exists("CRM Fields Layout", name):
        return frappe.get_doc("CRM Fields Layout", name)

    doc = frappe.new_doc("CRM Fields Layout")
    doc.name = name
    doc.dt = doctype
    doc.type = layout_type
    doc.layout = "[]"
    doc.insert(ignore_permissions=True)
    return doc


def parse_layout(value):
    if not value:
        return []
    parsed = frappe.parse_json(value)
    return parsed if isinstance(parsed, list) else []


def find_layout_section(layout, section_name):
    for section in layout:
        if section.get("name") == section_name:
            return section
    return None


def ensure_real_estate_layouts():
    for layout_name, settings in REAL_ESTATE_FIELD_LAYOUTS.items():
        doc = get_or_create_fields_layout(settings["doctype"], settings["type"])
        doc.layout = json.dumps(settings["layout"])
        doc.save(ignore_permissions=True)


def ensure_real_estate_standard_views():
    if not frappe.db.exists("DocType", "CRM View Settings"):
        return

    for settings in REAL_ESTATE_STANDARD_VIEWS:
        filters = {
            "dt": settings["dt"],
            "type": "list",
            "is_standard": 1,
            "user": "",
        }
        if frappe.db.exists("CRM View Settings", filters):
            doc = frappe.get_doc("CRM View Settings", filters)
        else:
            doc = frappe.new_doc("CRM View Settings")
            doc.dt = settings["dt"]
            doc.type = "list"
            doc.is_standard = 1
            doc.user = ""

        doc.label = settings["label"]
        doc.route_name = settings["route_name"]
        doc.icon = settings["icon"]
        doc.public = 1
        doc.pinned = 1
        doc.columns = json.dumps(settings["columns"])
        doc.rows = json.dumps(settings["rows"])
        doc.order_by = "modified desc"
        doc.is_default = 0

        if doc.is_new():
            doc.insert(ignore_permissions=True)
        else:
            doc.save(ignore_permissions=True)


def ensure_real_estate_quick_filters():
    if not frappe.db.exists("DocType", "CRM Global Settings"):
        return

    for doctype, fields in REAL_ESTATE_QUICK_FILTERS.items():
        if not frappe.db.exists("DocType", doctype):
            continue

        filters = {"dt": doctype}
        if frappe.db.exists("CRM Global Settings", filters):
            doc = frappe.get_doc("CRM Global Settings", filters)
            existing_fields = frappe.parse_json(doc.json) if doc.json else []
            merged_fields = list(dict.fromkeys((existing_fields or []) + fields))
            doc.json = json.dumps(merged_fields)
            doc.save(ignore_permissions=True)
        else:
            doc = frappe.new_doc("CRM Global Settings")
            doc.dt = doctype
            doc.json = json.dumps(fields)
            doc.insert(ignore_permissions=True)
