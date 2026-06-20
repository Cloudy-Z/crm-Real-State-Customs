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
            "fieldname": "interests_summary",
            "label": "Interests Summary",
            "fieldtype": "Small Text",
            "insert_after": "party_type",
        },
        {
            "fieldname": "whatsapp_number",
            "label": "WhatsApp Number",
            "fieldtype": "Phone",
            "insert_after": "interests_summary",
        },
        {
            "fieldname": "selection_tier",
            "label": "Selection Tier",
            "fieldtype": "Select",
            "options": "\nA - Priority\nB - Qualified\nC - Nurture",
            "insert_after": "whatsapp_number",
        },
        {
            "fieldname": "next_event_window",
            "label": "Next Event Window",
            "fieldtype": "Datetime",
            "insert_after": "selection_tier",
        },
        {
            "fieldname": "event_status_lifecycle",
            "label": "Event Status Lifecycle",
            "fieldtype": "Select",
            "options": "\nScheduled\nCompleted\nPostponed\nNo Show",
            "insert_after": "next_event_window",
        },
        {
            "fieldname": "buyer_requirements_section",
            "label": "Buyer Requirements and Search Filters",
            "fieldtype": "Section Break",
            "insert_after": "event_status_lifecycle",
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
            "fieldname": "interested_unit_area",
            "label": "Interested Unit Area",
            "fieldtype": "Float",
            "insert_after": "buyer_budget",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
        {
            "fieldname": "area_unit",
            "label": "Area Unit",
            "fieldtype": "Select",
            "options": "Sq M\nSq Ft",
            "default": "Sq M",
            "insert_after": "interested_unit_area",
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
            "fieldname": "no_answer_first_call",
            "label": "No Answer – 1st Call Count",
            "fieldtype": "Int",
            "insert_after": "preferred_delivery_time",
            "depends_on": "eval:doc.party_type == 'Buyer'",
            "read_only": 1,
        },
        {
            "fieldname": "no_answer_second_call",
            "label": "No Answer – 2nd Call Count",
            "fieldtype": "Int",
            "insert_after": "no_answer_first_call",
            "depends_on": "eval:doc.party_type == 'Buyer'",
            "read_only": 1,
        },
        {
            "fieldname": "interested_in_units",
            "label": "Interested in Units",
            "fieldtype": "Table",
            "options": "Lead Interested Unit",
            "insert_after": "no_answer_second_call",
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

LEAD_CONTACT_LAYOUT_FIELDS = [
    "lead_name",
    "mobile_no",
    "phone",
    "whatsapp_number",
    "job_title",
    "selection_tier",
]

LEAD_EVENT_LAYOUT_FIELDS = ["next_event_window", "event_status_lifecycle"]

LEAD_REAL_ESTATE_LAYOUT_FIELDS = [
    "party_type",
    "interests_summary",
    "buyer_budget",
    "interested_unit_area",
    "area_unit",
    "preferred_unit_type",
    "preferred_area",
    "preferred_developer",
    "preferred_compound",
    "preferred_finishing_type",
    "preferred_delivery_time",
    "no_answer_first_call",
    "no_answer_second_call",
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

DEFAULT_CRM_LEAD_SIDE_PANEL_LAYOUT = [
    {
        "label": "Details",
        "name": "details_section",
        "opened": True,
        "columns": [
            {
                "name": "column_kl92",
                "fields": [
                    "organization",
                    "website",
                    "territory",
                    "industry",
                    "job_title",
                    "source",
                    "lead_owner",
                ],
            }
        ],
    },
    {
        "label": "Person",
        "name": "person_section",
        "opened": True,
        "columns": [
            {
                "name": "column_XmW2",
                "fields": [
                    "salutation",
                    "first_name",
                    "last_name",
                    "email",
                    "mobile_no",
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
        "next_event_window",
        "event_status_lifecycle",
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

    create_custom_fields(CRM_LEAD_CUSTOM_FIELDS, update=True)
    frappe.clear_cache(doctype="CRM Lead")


def enforce_crm_lead_phone_mandatory():
    """Make CRM Lead phone/mobile mandatory without editing the upstream CRM DocType JSON."""
    for fieldname in ("mobile_no", "phone"):
        if frappe.db.exists("DocField", {"parent": "CRM Lead", "fieldname": fieldname}):
            make_property_setter(
                "CRM Lead",
                fieldname,
                "reqd",
                "1",
                "Check",
            )


def make_property_setter(doc_type, field_name, property_name, value, property_type):
    filters = {
        "doc_type": doc_type,
        "field_name": field_name,
        "property": property_name,
    }
    if frappe.db.exists("Property Setter", filters):
        doc = frappe.get_doc("Property Setter", filters)
    else:
        doc = frappe.new_doc("Property Setter")
        doc.doc_type = doc_type
        doc.field_name = field_name
        doc.doctype_or_field = "DocField"
        doc.property = property_name
    doc.value = value
    doc.property_type = property_type
    doc.save(ignore_permissions=True)


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
        ['mobile_no', 'phone'].forEach((fieldname) => {
            if (frm.fields_dict[fieldname]) {
                frm.set_df_property(fieldname, 'reqd', 1);
            }
        });

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
        const value = (frm.doc.mobile_no || frm.doc.phone || '').trim();
        const phone_regex = /^\+(?=\d{10,13}$)\d{1,3}\d{7,10}$/;
        if (!value) {
            frappe.msgprint(__('Mobile/phone number is mandatory. Please enter it in international format, for example +201001234567.'));
            frappe.validated = false;
            return;
        }
        if (!phone_regex.test(value)) {
            frappe.msgprint(__('Invalid mobile/phone format. Use international format with a leading +, a 1-to-3 digit country code, and a 7-to-10 digit local number. The numeric part must contain 10 to 13 digits. Example: +201001234567.'));
            frappe.validated = false;
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


def ensure_lead_layouts_include_real_estate_fields():
    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Quick Entry",
        section_name="lead_section",
        column_name="column_real_estate_quick_entry",
        fields=LEAD_REAL_ESTATE_LAYOUT_FIELDS,
    )
    ensure_crm_lead_main_form_sections()
    reset_crm_lead_side_panel_to_default()


def ensure_crm_lead_main_form_sections():
    """Keep the document sections in the main Lead form, not in the right sidebar."""

    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Data Fields",
        section_name="contact_identity_data_fields_section",
        section_label="Contact Identity Card",
        column_name="column_contact_identity_data_fields",
        fields=LEAD_CONTACT_LAYOUT_FIELDS,
        section_opened=False,
        section_collapsible=True,
    )
    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Data Fields",
        section_name="real_estate_data_fields_section",
        section_label="Buyer and Seller Details",
        column_name="column_real_estate_data_fields",
        fields=LEAD_REAL_ESTATE_LAYOUT_FIELDS,
        section_opened=False,
        section_collapsible=True,
    )
    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Data Fields",
        section_name="task_execution_data_fields_section",
        section_label="Task Execution and Milestone Deadlines",
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
