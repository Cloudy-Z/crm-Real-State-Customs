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
            "label": "Party Type",
            "fieldtype": "Select",
            "options": "Buyer\nSeller",
            "default": "Buyer",
            "insert_after": "real_estate_section",
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
        {
            "fieldname": "interested_in_units",
            "label": "Interested in Units",
            "fieldtype": "Table",
            "options": "Lead Interested Unit",
            "insert_after": "party_type",
            "depends_on": "eval:doc.party_type == 'Buyer'",
        },
    ]
}

LEAD_REAL_ESTATE_LAYOUT_FIELDS = ["party_type", "interested_in_units"]

REAL_ESTATE_FIELD_LAYOUTS = {
    "Real Estate Unit-Quick Entry": {
        "doctype": "Real Estate Unit",
        "type": "Quick Entry",
        "layout": [
            {
                "name": "unit_section",
                "columns": [
                    {"name": "column_unit_a", "fields": ["project", "unit_number"]},
                    {"name": "column_unit_b", "fields": ["unit_type", "status"]},
                    {"name": "column_unit_c", "fields": ["price", "owner_lead"]},
                ],
            }
        ],
    },
    "Real Estate Unit-Side Panel": {
        "doctype": "Real Estate Unit",
        "type": "Side Panel",
        "layout": [
            {
                "label": "Unit Details",
                "name": "unit_details_section",
                "opened": True,
                "columns": [
                    {
                        "name": "column_unit_details",
                        "fields": ["project", "unit_number", "unit_type", "status", "price", "owner_lead"],
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
                "label": "Unit Details",
                "name": "unit_details_section",
                "opened": True,
                "columns": [
                    {"name": "column_unit_data_a", "fields": ["project", "unit_number"]},
                    {"name": "column_unit_data_b", "fields": ["unit_type", "status"]},
                    {"name": "column_unit_data_c", "fields": ["price", "owner_lead"]},
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
    "Real Estate Project-Side Panel": {
        "doctype": "Real Estate Project",
        "type": "Side Panel",
        "layout": [
            {
                "label": "Project Details",
                "name": "project_details_section",
                "opened": True,
                "columns": [
                    {"name": "column_project_details", "fields": ["project_name", "location", "developer", "status"]}
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
                    {"name": "column_project_data_a", "fields": ["project_name", "location"]},
                    {"name": "column_project_data_b", "fields": ["developer", "status"]},
                ],
            }
        ],
    },
}

REAL_ESTATE_STANDARD_VIEWS = [
    {
        "label": "Real Estate Units",
        "dt": "Real Estate Unit",
        "route_name": "Real Estate Units",
        "icon": "home",
        "columns": [
            {"label": "Unit Number", "type": "Data", "key": "unit_number", "width": "12rem"},
            {"label": "Project", "type": "Link", "key": "project", "width": "14rem"},
            {"label": "Unit Type", "type": "Select", "key": "unit_type", "width": "10rem"},
            {"label": "Status", "type": "Select", "key": "status", "width": "10rem"},
            {"label": "Price", "type": "Currency", "key": "price", "width": "10rem"},
            {"label": "Owner Lead", "type": "Link", "key": "owner_lead", "width": "14rem"},
            {"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
        ],
        "rows": ["name", "unit_number", "project", "unit_type", "status", "price", "owner_lead", "modified"],
    },
    {
        "label": "Real Estate Projects",
        "dt": "Real Estate Project",
        "route_name": "Real Estate Projects",
        "icon": "building-2",
        "columns": [
            {"label": "Project Name", "type": "Data", "key": "project_name", "width": "14rem"},
            {"label": "Location", "type": "Data", "key": "location", "width": "14rem"},
            {"label": "Developer", "type": "Data", "key": "developer", "width": "14rem"},
            {"label": "Status", "type": "Select", "key": "status", "width": "10rem"},
            {"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
        ],
        "rows": ["name", "project_name", "location", "developer", "status", "modified"],
    },
]

REAL_ESTATE_QUICK_FILTERS = {
    "CRM Lead": ["lead_name", "email", "organization", "status", "source", "party_type", "lead_owner"],
    "Real Estate Unit": ["project", "unit_number", "unit_type", "status", "owner_lead"],
    "Real Estate Project": ["project_name", "location", "developer", "status"],
}


def after_install():
    sync_real_estate_crm_defaults()


def after_migrate():
    sync_real_estate_crm_defaults()


def sync_real_estate_crm_defaults():
    ensure_module_def()
    setup_crm_lead_custom_fields()
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
        fields=["party_type"],
    )
    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Side Panel",
        section_name="real_estate_side_panel_section",
        section_label="Real Estate",
        column_name="column_real_estate_side_panel",
        fields=LEAD_REAL_ESTATE_LAYOUT_FIELDS,
    )
    append_fields_to_layout(
        doctype="CRM Lead",
        layout_type="Data Fields",
        section_name="real_estate_data_fields_section",
        section_label="Real Estate",
        column_name="column_real_estate_data_fields",
        fields=LEAD_REAL_ESTATE_LAYOUT_FIELDS,
    )


def append_fields_to_layout(doctype, layout_type, section_name, column_name, fields, section_label=None):
    layout_doc = get_or_create_fields_layout(doctype, layout_type)
    layout = parse_layout(layout_doc.layout)

    section = find_layout_section(layout, section_name)
    if not section:
        section = {"name": section_name, "opened": True, "columns": []}
        if section_label:
            section["label"] = section_label
        layout.append(section)

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

    changed = False
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
