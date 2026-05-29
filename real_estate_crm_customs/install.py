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


def after_install():
    ensure_module_def()
    setup_crm_lead_custom_fields()


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
