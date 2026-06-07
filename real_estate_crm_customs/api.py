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


def _validate_buyer_interest_unit(lead_doc, unit):
    if not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit), frappe.DoesNotExistError)

    status = frappe.db.get_value("Real Estate Unit", unit, "status")
    if status != "Available":
        frappe.throw(_("Only Available units can be linked as interested properties."), frappe.ValidationError)

    if lead_doc.get("party_type") and lead_doc.get("party_type") != "Buyer":
        frappe.throw(_("Only Buyer leads can be linked to interested properties."), frappe.ValidationError)


@frappe.whitelist()
def link_interested_unit(lead, unit):
    return link_interested_units(lead, [unit])


@frappe.whitelist()
def link_interested_units(lead, units):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)

    if isinstance(units, str):
        units = frappe.parse_json(units)

    normalized_units = []
    for unit in units or []:
        if isinstance(unit, dict):
            unit = unit.get("unit") or unit.get("value") or unit.get("name") or unit.get("real_estate_unit")
        if unit:
            normalized_units.append(unit)

    if not normalized_units:
        frappe.throw(_("Select at least one inventory unit."), frappe.ValidationError)

    doc = frappe.get_doc("CRM Lead", lead)
    existing_units = {row.unit for row in doc.get("interested_in_units") or [] if row.unit}
    added = 0

    for unit in dict.fromkeys(normalized_units):
        _validate_buyer_interest_unit(doc, unit)
        if unit in existing_units:
            continue
        doc.append(
            "interested_in_units",
            {
                "doctype": "Lead Interested Unit",
                "unit": unit,
            },
        )
        existing_units.add(unit)
        added += 1

    if added:
        doc.save()

    return doc.as_dict()


@frappe.whitelist()
def assign_property_unit_to_seller(lead, unit):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)

    if not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit), frappe.DoesNotExistError)

    lead_doc = frappe.get_doc("CRM Lead", lead)
    if lead_doc.get("party_type") != "Seller":
        frappe.throw(_("Only Seller leads can be assigned property units."), frappe.ValidationError)

    unit_doc = frappe.get_doc("Real Estate Unit", unit)
    if unit_doc.get("status") != "Available":
        frappe.throw(_("Only Available units can be assigned to a seller lead."), frappe.ValidationError)

    if unit_doc.get("owner_lead") and unit_doc.get("owner_lead") != lead:
        frappe.throw(
            _("Unit {0} is already assigned to seller lead {1}.").format(unit, unit_doc.get("owner_lead")),
            frappe.ValidationError,
        )

    unit_doc.owner_lead = lead
    unit_doc.save()
    return unit_doc.as_dict()


@frappe.whitelist()
def get_lead_linked_units(lead):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)

    lead_doc = frappe.get_doc("CRM Lead", lead)
    interested_rows = [row for row in lead_doc.get("interested_in_units") or [] if row.unit]
    interested_units = [row.unit for row in interested_rows]
    proposal_status_by_unit = {row.unit: row.get("proposal_status") for row in interested_rows}

    filters = []
    if interested_units:
        filters.append(["Real Estate Unit", "name", "in", interested_units])
    filters.append(["Real Estate Unit", "owner_lead", "=", lead])

    if not filters:
        return []

    names = set(interested_units)
    owner_rows = frappe.get_all("Real Estate Unit", filters={"owner_lead": lead}, pluck="name")
    names.update(owner_rows)

    if not names:
        return []

    rows = frappe.get_all(
        "Real Estate Unit",
        filters={"name": ["in", list(names)]},
        fields=[
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
        order_by="modified desc",
    )

    interested_set = set(interested_units)
    for row in rows:
        if row.name in interested_set and row.owner_lead == lead:
            row.relationship = _("Interested and Owned")
        elif row.owner_lead == lead:
            row.relationship = _("Seller Unit")
        else:
            row.relationship = _("Interested Unit")
        row.proposal_status = proposal_status_by_unit.get(row.name)

    return rows
