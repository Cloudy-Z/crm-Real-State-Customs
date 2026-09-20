"""Additive migration into the canonical real-estate field model.

The patch never drops columns, deletes Custom Fields, guesses financial values,
or changes Lead Interest lifecycle state.
"""

import frappe

from real_estate_crm_customs.master_data import (
    ensure_default_unit_types,
    ensure_legacy_unit_type,
)
from real_estate_crm_customs.party_roles import normalize_party_role


def execute():
    ensure_default_unit_types()
    normalize_party_aliases()
    destinations = migrate_compound_destinations()
    migrate_unit_fields(destinations)
    migrate_requested_unit_types()
    migrate_interest_destinations(destinations)
    frappe.clear_cache()


def _normalize(value):
    return " ".join(str(value or "").strip().split())


def _log_issue(title, message):
    frappe.log_error(message=message, title=title)


def normalize_party_aliases():
    if not frappe.db.exists("DocType", "CRM Lead") or not frappe.db.has_column("CRM Lead", "party_type"):
        return
    alias_fields = [field for field in ("custom_type", "lead_type") if frappe.db.has_column("CRM Lead", field)]
    fields = ["name", "party_type", *alias_fields]
    for lead in frappe.get_all("CRM Lead", fields=fields, limit_page_length=0):
        canonical = normalize_party_role(lead.get("party_type"))
        aliases = {normalize_party_role(lead.get(field)) for field in alias_fields if lead.get(field)}
        aliases.discard(None)
        if canonical:
            if aliases and aliases != {canonical}:
                _log_issue(
                    "CRM Lead party-role conflict",
                    f"{lead.name}: party_type={lead.party_type!r}; aliases={sorted(aliases)!r}",
                )
            if lead.party_type != canonical:
                frappe.db.set_value("CRM Lead", lead.name, "party_type", canonical, update_modified=False)
            continue
        if len(aliases) == 1:
            frappe.db.set_value("CRM Lead", lead.name, "party_type", next(iter(aliases)), update_modified=False)
        elif aliases:
            _log_issue(
                "CRM Lead party-role conflict",
                f"{lead.name}: invalid party_type={lead.party_type!r}; aliases={sorted(aliases)!r}",
            )


def _destination_index():
    if not frappe.db.exists("DocType", "Real Estate Destination"):
        return {}
    return {
        _normalize(row.destination_name).casefold(): row.name
        for row in frappe.get_all(
            "Real Estate Destination",
            fields=["name", "destination_name"],
            limit_page_length=0,
        )
        if row.destination_name
    }


def _destination_for_text(value, index, create=False):
    display = _normalize(value)
    if not display:
        return None
    key = display.casefold()
    if key in index:
        return index[key]
    if not create:
        return None
    destination = frappe.get_doc(
        {
            "doctype": "Real Estate Destination",
            "destination_name": display,
            "is_active": 1,
            "description": "Created from an exact legacy Compound location during field unification.",
        }
    )
    destination.insert(ignore_permissions=True)
    index[key] = destination.name
    return destination.name


def migrate_compound_destinations():
    index = _destination_index()
    if not frappe.db.exists("DocType", "Real Estate Project"):
        return index
    fields = ["name", "location", "destination"]
    for compound in frappe.get_all("Real Estate Project", fields=fields, limit_page_length=0):
        if compound.destination or not compound.location:
            continue
        destination = _destination_for_text(compound.location, index, create=True)
        if destination:
            frappe.db.set_value(
                "Real Estate Project",
                compound.name,
                "destination",
                destination,
                update_modified=False,
            )
    return index


def migrate_unit_fields(destinations):
    if not frappe.db.exists("DocType", "Real Estate Unit"):
        return
    has_party_type = frappe.db.has_column("CRM Lead", "party_type")
    fields = [
        "name",
        "project",
        "developer",
        "destination",
        "unit_type",
        "physical_unit_type",
        "inventory_type",
        "owner_lead",
    ]
    for unit in frappe.get_all("Real Estate Unit", fields=fields, limit_page_length=0):
        values = {}
        if unit.project:
            compound = frappe.db.get_value(
                "Real Estate Project",
                unit.project,
                ["developer", "destination", "location"],
                as_dict=True,
            ) or {}
            if compound.get("developer") and unit.developer != compound.get("developer"):
                values["developer"] = compound.get("developer")
            destination = compound.get("destination") or _destination_for_text(
                compound.get("location"),
                destinations,
                create=False,
            )
            if destination and unit.destination != destination:
                values["destination"] = destination
        legacy_type = _normalize(unit.unit_type)
        if not unit.physical_unit_type and legacy_type:
            values["physical_unit_type"] = ensure_legacy_unit_type(legacy_type)
        owner_role = (
            frappe.db.get_value("CRM Lead", unit.owner_lead, "party_type")
            if unit.owner_lead and has_party_type
            else None
        )
        if not unit.inventory_type:
            if owner_role == "Seller":
                values["inventory_type"] = "Resale"
            elif not unit.owner_lead:
                values["inventory_type"] = "Primary"
            else:
                _log_issue(
                    "Unresolved Real Estate Unit classification",
                    f"{unit.name}: owner_lead={unit.owner_lead!r}; party_type is unavailable before fixture sync",
                )
        if unit.owner_lead and has_party_type and owner_role != "Seller":
            _log_issue(
                "Invalid Real Estate Unit owner",
                f"{unit.name}: preserved owner_lead={unit.owner_lead!r}; party_type={owner_role!r}; manual review required",
            )
        if values:
            frappe.db.set_value("Real Estate Unit", unit.name, values, update_modified=False)


def migrate_interest_destinations(destinations):
    mappings = (
        ("Lead Interest", "requested_area", "requested_destination"),
        ("Lead Interested Unit", "requested_area", "requested_destination"),
        ("CRM Lead", "preferred_area", "preferred_destination"),
    )
    for doctype, source_field, target_field in mappings:
        if not frappe.db.exists("DocType", doctype):
            continue
        if not frappe.db.has_column(doctype, source_field) or not frappe.db.has_column(doctype, target_field):
            continue
        for row in frappe.get_all(
            doctype,
            fields=["name", source_field, target_field],
            limit_page_length=0,
        ):
            if row.get(target_field) or not row.get(source_field):
                continue
            destination = _destination_for_text(row.get(source_field), destinations, create=False)
            if destination:
                frappe.db.set_value(doctype, row.name, target_field, destination, update_modified=False)
            else:
                _log_issue(
                    "Unmapped real-estate Destination",
                    f"{doctype} {row.name}: {source_field}={row.get(source_field)!r}",
                )


def migrate_requested_unit_types():
    for doctype, fieldname in (
        ("CRM Lead", "preferred_unit_type"),
        ("Lead Interest", "requested_unit_type"),
        ("Lead Interested Unit", "requested_unit_type"),
    ):
        if not frappe.db.exists("DocType", doctype) or not frappe.db.has_column(doctype, fieldname):
            continue
        for row in frappe.get_all(doctype, fields=["name", fieldname], limit_page_length=0):
            legacy_value = row.get(fieldname)
            if not legacy_value:
                continue
            canonical_type = ensure_legacy_unit_type(legacy_value)
            if canonical_type != legacy_value:
                frappe.db.set_value(doctype, row.name, fieldname, canonical_type, update_modified=False)
