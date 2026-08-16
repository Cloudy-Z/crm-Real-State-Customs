import json

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


CRM_LEAD_CUSTOM_FIELDS = {
    "CRM Lead": [
        {
            "fieldname": "real_estate_section",
            "label": "Real Estate",
            "fieldtype": "Section Break",
            "insert_after": "lead_owner",
            "collapsible": 1,
        },
        {
            "fieldname": "party_type",
            "label": "Type",
            "fieldtype": "Select",
            "options": "Buyer\nSeller",
            "default": "Buyer",
            "insert_after": "real_estate_section",
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
        {
            "fieldname": "mobile_country_code",
            "label": "Mobile Country Code",
            "fieldtype": "Select",
            "options": "\n+20\n+966\n+971\n+974\n+973\n+968\n+965\n+962\n+961\n+963\n+964\n+212\n+216\n+213\n+218\n+249\n+1\n+44\n+49\n+33\n+39\n+34\n+31\n+90\n+91\n+92\n+86\n+81\n+82\n+61\n+7",
            "default": "+20",
            "insert_after": "email",
        },
        {
            "fieldname": "whatsapp_country_code",
            "label": "WhatsApp Country Code",
            "fieldtype": "Select",
            "options": "\n+20\n+966\n+971\n+974\n+973\n+968\n+965\n+962\n+961\n+963\n+964\n+212\n+216\n+213\n+218\n+249\n+1\n+44\n+49\n+33\n+39\n+34\n+31\n+90\n+91\n+92\n+86\n+81\n+82\n+61\n+7",
            "default": "+20",
            "insert_after": "mobile_no",
        },
        {
            "fieldname": "whatsapp_number",
            "label": "WhatsApp Number",
            "fieldtype": "Data",
            "insert_after": "whatsapp_country_code",
        },
        {
            "fieldname": "selection_tier",
            "label": "Selection Tier",
            "fieldtype": "Select",
            "options": "\nA - Priority\nB - Qualified\nC - Nurture",
            "insert_after": "whatsapp_number",
        },
        {
            "fieldname": "buyer_requirements_section",
            "label": "Buyer Requirements and Search Filters",
            "fieldtype": "Section Break",
            "insert_after": "selection_tier",
            "depends_on": "eval:doc.party_type == 'Buyer'",
            "collapsible": 1,
        },
        {
            "fieldname": "buyer_budget",
            "label": "Budget",
            "fieldtype": "Currency",
            "insert_after": "buyer_requirements_section",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "area_unit",
            "label": "Area Unit",
            "fieldtype": "Select",
            "options": "Sq M\nSq Ft",
            "default": "Sq M",
            "insert_after": "buyer_budget",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "preferred_unit_type",
            "label": "Unit Type",
            "fieldtype": "Select",
            "options": "\nApartment\nDuplex\nTownhouse\nVilla\nChalet\nStudio\nPenthouse",
            "insert_after": "area_unit",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "preferred_area",
            "label": "Area",
            "fieldtype": "Data",
            "insert_after": "preferred_unit_type",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "preferred_developer",
            "label": "Developer",
            "fieldtype": "Link",
            "options": "Property Developer",
            "insert_after": "preferred_area",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "preferred_compound",
            "label": "Compound / Project",
            "fieldtype": "Link",
            "options": "Real Estate Project",
            "insert_after": "preferred_developer",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "preferred_finishing_type",
            "label": "Finishing Type",
            "fieldtype": "Select",
            "options": "\nCore & Shell\nSemi-Finished\nFully Finished",
            "insert_after": "preferred_compound",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "preferred_delivery_time",
            "label": "Delivery Time",
            "fieldtype": "Data",
            "insert_after": "preferred_finishing_type",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "lead_age",
            "label": "Lead Age",
            "fieldtype": "Data",
            "insert_after": "preferred_delivery_time",
            "read_only": 1,
            "description": "Auto-updated every hour by the system.",
        },
        {
            "fieldname": "is_primary_buyer",
            "label": "Primary Buyer",
            "fieldtype": "Check",
            "insert_after": "lead_age",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "no_answer_first_call",
            "label": "No Answer \u2013 1st Call",
            "fieldtype": "Check",
            "insert_after": "is_primary_buyer",
            "depends_on": "eval:doc.party_type == 'Buyer'",
            "read_only": 1,
        },
        {
            "fieldname": "no_answer_second_call",
            "label": "No Answer \u2013 2nd Call",
            "fieldtype": "Check",
            "insert_after": "no_answer_first_call",
            "depends_on": "eval:doc.party_type == 'Buyer'",
            "read_only": 1,
        },
        {
            "fieldname": "no_answer_consecutive_count",
            "label": "Current No Answer Streak",
            "fieldtype": "Int",
            "insert_after": "no_answer_second_call",
            "depends_on": "eval:doc.party_type == 'Buyer'",
            "read_only": 1,
        },
        {
            "fieldname": "no_answer_total_count",
            "label": "Total No Answer Count",
            "fieldtype": "Int",
            "insert_after": "no_answer_consecutive_count",
            "depends_on": "eval:doc.party_type == 'Buyer'",
            "read_only": 1,
        },
        {
            "fieldname": "last_call_outcome",
            "label": "Last Call Outcome",
            "fieldtype": "Select",
            "options": "\nAnswered\nNo Answer",
            "insert_after": "no_answer_total_count",
            "depends_on": "eval:doc.party_type == 'Buyer'",
            "read_only": 1,
        },
        {
            "fieldname": "last_call_at",
            "label": "Last Call At",
            "fieldtype": "Datetime",
            "insert_after": "last_call_outcome",
            "depends_on": "eval:doc.party_type == 'Buyer'",
            "read_only": 1,
        },
        {
            "fieldname": "interested_in_units",
            "label": "Interested in Units",
            "fieldtype": "Table",
            "options": "Lead Interested Unit",
            "insert_after": "last_call_at",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "seller_property_section",
            "label": "Seller Property Onboarding",
            "fieldtype": "Section Break",
            "insert_after": "interested_in_units",
            "depends_on": "eval:doc.party_type == 'Seller'",
            "collapsible": 1,
        },
        {
            "fieldname": "property_title",
            "label": "Property Title / Designation",
            "fieldtype": "Data",
            "insert_after": "seller_property_section",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
        {
            "fieldname": "target_asking_price",
            "label": "Target Asking Price",
            "fieldtype": "Currency",
            "insert_after": "property_title",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
        {
            "fieldname": "property_code",
            "label": "Property Code / Assigned SKU",
            "fieldtype": "Data",
            "insert_after": "target_asking_price",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
        {
            "fieldname": "location_reference",
            "label": "Location Reference",
            "fieldtype": "Data",
            "insert_after": "property_code",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
        {
            "fieldname": "seller_compound",
            "label": "Compound / Project",
            "fieldtype": "Link",
            "options": "Real Estate Project",
            "insert_after": "location_reference",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
        {
            "fieldname": "seller_developer",
            "label": "Developer",
            "fieldtype": "Link",
            "options": "Property Developer",
            "insert_after": "seller_compound",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
        {
            "fieldname": "seller_unit_type",
            "label": "Unit Type",
            "fieldtype": "Select",
            "options": "\nStandalone Villa\nPenthouse\nStudio\nApartment\nDuplex\nTownhouse\nChalet",
            "insert_after": "seller_developer",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
        {
            "fieldname": "unit_area",
            "label": "Unit Area",
            "fieldtype": "Float",
            "insert_after": "seller_unit_type",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
        {
            "fieldname": "seller_finishing_type",
            "label": "Finishing Type",
            "fieldtype": "Select",
            "options": "\nCore & Shell\nSemi-Finished\nFully Finished",
            "insert_after": "unit_area",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
        {
            "fieldname": "property_documents",
            "label": "Property Documents & Deed Upload",
            "fieldtype": "Attach",
            "insert_after": "seller_finishing_type",
            "depends_on": "eval:doc.party_type == 'Seller'",
        },
    ]
}

CRM_USER_CUSTOM_FIELDS = {
    "User": [
        {
            "fieldname": "real_estate_agent_outreach_section",
            "label": "Real Estate Agent Outreach",
            "fieldtype": "Section Break",
            "insert_after": "user_emails",
            "collapsible": 1,
        },
        {
            "fieldname": "real_estate_agent_whatsapp_number",
            "label": "Agent WhatsApp Number",
            "fieldtype": "Data",
            "insert_after": "real_estate_agent_outreach_section",
        },
        {
            "fieldname": "real_estate_agent_outreach_email",
            "label": "Agent Outreach Email",
            "fieldtype": "Data",
            "options": "Email",
            "insert_after": "real_estate_agent_whatsapp_number",
        },
    ]
}

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
    "area_unit",
    "preferred_unit_type",
    "preferred_area",
    "preferred_developer",
    "preferred_compound",
    "preferred_finishing_type",
    "preferred_delivery_time",
    "interested_in_units",
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
]

LEAD_CALL_FLAGS_LAYOUT_FIELDS = [
    "no_answer_first_call",
    "no_answer_second_call",
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
                    "mobile_country_code",
                    "mobile_no",
                    "whatsapp_country_code",
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
                "name": "developer_section",
                "columns": [
                    {"name": "column_developer_a", "fields": ["developer_name"]},
                    {"name": "column_developer_b", "fields": ["company_registration"]},
                ],
            }
        ],
    },
    "Real Estate Project-Quick Entry": {
        "doctype": "Real Estate Project",
        "type": "Quick Entry",
        "layout": [
            {
                "name": "project_section",
                "columns": [
                    {"name": "column_project_a", "fields": ["project_name", "developer"]},
                    {"name": "column_project_b", "fields": ["location", "status"]},
                ],
            }
        ],
    },
    "Real Estate Unit-Quick Entry": {
        "doctype": "Real Estate Unit",
        "type": "Quick Entry",
        "layout": [
            {
                "name": "unit_section",
                "columns": [
                    {"name": "column_unit_a", "fields": ["project", "developer", "sku"]},
                    {"name": "column_unit_b", "fields": ["unit_type", "floor", "status"]},
                    {"name": "column_unit_c", "fields": ["finishing_type", "price", "owner_lead"]},
                ],
            }
        ],
    },
    "Real Estate Unit-Side Panel": {
        "doctype": "Real Estate Unit",
        "type": "Side Panel",
        "layout": [
            {
                "label": "Inventory Details",
                "name": "unit_details_section",
                "opened": True,
                "columns": [
                    {
                        "name": "column_unit_details",
                        "fields": [
                            "sku",
                            "project",
                            "developer",
                            "unit_type",
                            "floor",
                            "finishing_type",
                            "status",
                            "price",
                            "owner_lead",
                        ],
                    }
                ],
            }
        ],
    },
    "Real Estate Unit-Data Fields": {
        "doctype": "Real Estate Unit",
        "type": "Data Fields",
        "layout": [
            {
                "label": "Inventory Details",
                "name": "unit_details_section",
                "opened": True,
                "columns": [
                    {"name": "column_unit_data_a", "fields": ["sku", "project", "developer"]},
                    {"name": "column_unit_data_b", "fields": ["unit_type", "floor", "finishing_type"]},
                    {"name": "column_unit_data_c", "fields": ["status", "price", "owner_lead"]},
                ],
            }
        ],
    },
    "Real Estate Project-Side Panel": {
        "doctype": "Real Estate Project",
        "type": "Side Panel",
        "layout": [
            {
                "label": "Project Details",
                "name": "project_details_section",
                "opened": True,
                "columns": [
                    {"name": "column_project_details", "fields": ["project_name", "developer", "location", "status"]}
                ],
            }
        ],
    },
    "Real Estate Project-Data Fields": {
        "doctype": "Real Estate Project",
        "type": "Data Fields",
        "layout": [
            {
                "label": "Project Details",
                "name": "project_details_section",
                "opened": True,
                "columns": [
                    {"name": "column_project_data_a", "fields": ["project_name", "developer"]},
                    {"name": "column_project_data_b", "fields": ["location", "status"]},
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
            {"label": "Company Registration", "type": "Data", "key": "company_registration", "width": "16rem"},
            {"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
        ],
        "rows": ["name", "developer_name", "company_registration", "modified"],
    },
    {
        "label": "Real Estate Projects",
        "dt": "Real Estate Project",
        "route_name": "Real Estate Projects",
        "icon": "building-2",
        "columns": [
            {"label": "Project Name", "type": "Data", "key": "project_name", "width": "14rem"},
            {"label": "Developer", "type": "Link", "key": "developer", "width": "14rem"},
            {"label": "Location", "type": "Data", "key": "location", "width": "14rem"},
            {"label": "Status", "type": "Select", "key": "status", "width": "10rem"},
            {"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
        ],
        "rows": ["name", "project_name", "developer", "location", "status", "modified"],
    },
    {
        "label": "Real Estate Units",
        "dt": "Real Estate Unit",
        "route_name": "Real Estate Units",
        "icon": "home",
        "columns": [
            {"label": "SKU", "type": "Data", "key": "sku", "width": "12rem"},
            {"label": "Project", "type": "Link", "key": "project", "width": "14rem"},
            {"label": "Developer", "type": "Link", "key": "developer", "width": "14rem"},
            {"label": "Unit Type", "type": "Select", "key": "unit_type", "width": "10rem"},
            {"label": "Floor", "type": "Int", "key": "floor", "width": "7rem"},
            {"label": "Finishing", "type": "Select", "key": "finishing_type", "width": "12rem"},
            {"label": "Status", "type": "Select", "key": "status", "width": "10rem"},
            {"label": "Price", "type": "Currency", "key": "price", "width": "10rem"},
            {"label": "Owner Lead", "type": "Link", "key": "owner_lead", "width": "14rem"},
            {"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
        ],
        "rows": [
            "name",
            "sku",
            "project",
            "developer",
            "unit_type",
            "floor",
            "finishing_type",
            "status",
            "price",
            "owner_lead",
            "modified",
        ],
    },
]

REAL_ESTATE_QUICK_FILTERS = {
    "CRM Lead": [
        "lead_name",
        "email",
        "organization",
        "status",
        "source",
        "party_type",
        "lead_owner",
        "buyer_budget",
        "preferred_unit_type",
        "preferred_developer",
        "preferred_compound",
        "target_asking_price",
        "seller_unit_type",
        "seller_developer",
        "selection_tier",
    ],
    "Real Estate Unit": ["sku", "project", "developer", "unit_type", "floor", "finishing_type", "status", "owner_lead"],
    "Property Developer": ["developer_name", "company_registration"],
    "Real Estate Project": ["project_name", "developer", "location", "status"],
}


def after_install():
    sync_real_estate_crm_defaults()


def after_migrate():
    sync_real_estate_crm_defaults()


def sync_real_estate_crm_defaults():
    ensure_module_def()
    setup_crm_lead_custom_fields()
    setup_user_agent_custom_fields()
    ensure_real_estate_lead_statuses()
    enforce_crm_lead_phone_mandatory()
    setup_real_estate_client_scripts()
    setup_crm_portal_defaults()
    frappe.db.commit()


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

    # Pre-cleanup: delete Custom Fields whose fieldtype has changed
    # Frappe does not allow fieldtype changes via create_custom_fields(update=True)
    _fix_fieldtype_mismatches("CRM Lead", CRM_LEAD_CUSTOM_FIELDS.get("CRM Lead", []))

    create_custom_fields(CRM_LEAD_CUSTOM_FIELDS, update=True)
    frappe.clear_cache(doctype="CRM Lead")


def _fix_fieldtype_mismatches(doctype, field_definitions):
    """Delete existing Custom Fields whose fieldtype doesn't match the desired definition.
    This allows create_custom_fields to recreate them with the correct type."""
    for field_def in field_definitions:
        fieldname = field_def.get("fieldname")
        desired_type = field_def.get("fieldtype")
        cf_name = f"{doctype}-{fieldname}"
        if frappe.db.exists("Custom Field", cf_name):
            existing_type = frappe.db.get_value("Custom Field", cf_name, "fieldtype")
            if existing_type and existing_type != desired_type:
                # Drop the DB column first to avoid schema conflicts
                if frappe.db.has_column(doctype, fieldname):
                    try:
                        frappe.db.sql_ddl(f"ALTER TABLE `tab{doctype}` DROP COLUMN `{fieldname}`")
                    except Exception:
                        pass
                frappe.delete_doc("Custom Field", cf_name, ignore_permissions=True, force=True)
                frappe.db.commit()


def setup_user_agent_custom_fields():
    if not frappe.db.exists("DocType", "User"):
        return

    _fix_fieldtype_mismatches("User", CRM_USER_CUSTOM_FIELDS.get("User", []))
    create_custom_fields(CRM_USER_CUSTOM_FIELDS, update=True)
    frappe.clear_cache(doctype="User")


def ensure_real_estate_lead_statuses():
    if not frappe.db.exists("DocType", "CRM Lead Status"):
        return

    statuses = [
        {"lead_status": "Fresh Lead", "type": "Ongoing", "color": "blue", "position": 10},
        {"lead_status": "Contacted", "type": "Ongoing", "color": "blue", "position": 20},
        {"lead_status": "No Answer", "type": "Ongoing", "color": "orange", "position": 35},
        {"lead_status": "Interested", "type": "Ongoing", "color": "green", "position": 40},
        {"lead_status": "Not Interested", "type": "Lost", "color": "red", "position": 90},
    ]
    for status in statuses:
        if frappe.db.exists("CRM Lead Status", status["lead_status"]):
            continue
        doc = frappe.get_doc({"doctype": "CRM Lead Status", **status})
        doc.insert(ignore_permissions=True)


def enforce_crm_lead_phone_mandatory():
    """Ensure mobile_no is NOT individually mandatory.
    The 'phone' field has been removed from the DocType entirely.
    mobile_no + whatsapp_number are the only contact fields.
    Client script validates that at least one is filled."""

    # AGGRESSIVE CLEANUP: Remove ALL legacy Property Setters for phone/mobile_no reqd
    # This handles old migrations that set reqd=1 and any duplicates.
    frappe.db.sql("""
        DELETE FROM `tabProperty Setter`
        WHERE doc_type = 'CRM Lead'
        AND field_name IN ('phone', 'mobile_no')
        AND property IN ('reqd', 'hidden')
    """)
    frappe.db.commit()

    # Ensure mobile_no is explicitly not mandatory
    make_property_setter("CRM Lead", "mobile_no", "reqd", "0", "Check")

    # Also drop the phone column from the database if it still exists
    # (since the field was removed from the DocType JSON)
    try:
        if frappe.db.has_column("CRM Lead", "phone"):
            frappe.db.sql_ddl("ALTER TABLE `tabCRM Lead` DROP COLUMN `phone`")
    except Exception:
        pass  # Column may already be gone

    # Clear DocType cache so the schema changes take effect immediately
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
    },
    onload(frm) {
        if (frm.is_new()) {
            if (!frm.doc.status) {
                frm.set_value('status', 'Available');
            }
            if (!frm.doc.created_by && frappe.session.user) {
                frm.set_value('created_by', frappe.session.user);
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
        // Validate number format: digits only, 7-12 digits (local number without country code)
        const number_regex = /^\d{7,12}$/;
        if (mobile && !number_regex.test(mobile)) {
            frappe.msgprint(__('Mobile No should contain only digits (7-12 digits, without country code). Example: 1001234567'));
            frappe.validated = false;
            return;
        }
        if (whatsapp && !number_regex.test(whatsapp)) {
            frappe.msgprint(__('WhatsApp Number should contain only digits (7-12 digits, without country code). Example: 1001234567'));
            frappe.validated = false;
            return;
        }
        // Ensure country code is selected when number is provided
        if (mobile && !frm.doc.mobile_country_code) {
            frappe.msgprint(__('Please select a Country Code for Mobile No.'));
            frappe.validated = false;
            return;
        }
        if (whatsapp && !frm.doc.whatsapp_country_code) {
            frappe.msgprint(__('Please select a Country Code for WhatsApp Number.'));
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
            {"name": "col_phone", "fields": ["mobile_country_code", "mobile_no", "whatsapp_country_code", "whatsapp_number"]},
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
