import frappe
from frappe import _


@frappe.whitelist()
def create_resale_unit(owner_lead, project, unit_number, price=None):
    if not frappe.db.exists("CRM Lead", owner_lead):
        frappe.throw(_("Lead {0} was not found.").format(owner_lead), frappe.DoesNotExistError)

    lead = frappe.get_doc("CRM Lead", owner_lead)
    if lead.get("party_type") and lead.get("party_type") != "Seller":
        frappe.throw(_("Only Seller leads can list resale units."), frappe.ValidationError)

    if not project:
        frappe.throw(_("Project is required."), frappe.ValidationError)

    if not unit_number:
        frappe.throw(_("Unit Number is required."), frappe.ValidationError)

    unit = frappe.get_doc(
        {
            "doctype": "Real Estate Unit",
            "project": project,
            "unit_number": unit_number,
            "unit_type": "Resale",
            "status": "Available",
            "price": price,
            "owner_lead": owner_lead,
        }
    )
    unit.insert()
    return unit.as_dict()


@frappe.whitelist()
def link_interested_unit(lead, unit):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)

    if not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit), frappe.DoesNotExistError)

    status = frappe.db.get_value("Real Estate Unit", unit, "status")
    if status != "Available":
        frappe.throw(_("Only Available units can be linked as interested properties."), frappe.ValidationError)

    doc = frappe.get_doc("CRM Lead", lead)
    if doc.get("party_type") and doc.get("party_type") != "Buyer":
        frappe.throw(_("Only Buyer leads can be linked to interested properties."), frappe.ValidationError)

    for row in doc.get("interested_in_units") or []:
        if row.unit == unit:
            return doc.as_dict()

    doc.append(
        "interested_in_units",
        {
            "doctype": "Lead Interested Unit",
            "unit": unit,
        },
    )
    doc.save()
    return doc.as_dict()
