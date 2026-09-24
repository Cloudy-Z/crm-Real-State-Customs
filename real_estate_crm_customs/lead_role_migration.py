"""One-time compatibility migration into CRM Lead.party_type.

The canonical Party Role field is ``party_type``. Historical role aliases are read
only during this migration, then their Custom Field and CRM layout metadata are
removed so runtime code has one source of truth. Database columns are intentionally
left to Frappe's normal schema-retention policy.
"""

import json

import frappe

from real_estate_crm_customs.party_roles import (
    normalize_lead_view_columns,
    normalize_party_role,
    normalize_lead_view_filters,
    normalize_lead_view_rows,
    remove_fields_from_layout,
    remove_fields_from_quick_filters,
    resolve_party_role,
)


LEGACY_ROLE_FIELDS = ("custom_type", "lead_type")
CANONICAL_ROLES = {"Buyer", "Seller"}


def _log_issue(lead_name, message):
    frappe.log_error(
        title="CRM Lead Party Role migration audit",
        message=f"{lead_name}: {message}",
    )


def _linked_leads(doctype, fieldname):
    if not frappe.db.exists("DocType", doctype):
        return set()
    if not frappe.db.has_column(doctype, fieldname):
        return set()
    return {
        row[fieldname]
        for row in frappe.get_all(
            doctype,
            filters={fieldname: ["is", "set"]},
            fields=[fieldname],
            limit_page_length=0,
        )
        if row.get(fieldname)
    }


def _lead_role_rows(alias_fields):
    Lead = frappe.qb.DocType("CRM Lead")
    fields = [Lead.name, Lead.party_type]
    fields.extend(getattr(Lead, fieldname) for fieldname in alias_fields)
    return frappe.qb.from_(Lead).select(*fields).run(as_dict=True)


def _legacy_interest_leads():
    doctype = "Lead Interested Unit"
    if not frappe.db.exists("DocType", doctype):
        return set()
    return {
        row.parent
        for row in frappe.get_all(
            doctype,
            filters={"parenttype": "CRM Lead"},
            fields=["parent"],
            limit_page_length=0,
        )
        if row.get("parent")
    }


def _migrate_lead_view_routes():
    if not frappe.db.exists("DocType", "CRM View Settings"):
        return 0
    updated = 0
    for view in frappe.get_all(
        "CRM View Settings",
        filters={"dt": "CRM Lead"},
        fields=["name", "filters", "columns", "rows", "route_name"],
        limit_page_length=0,
    ):
        filters, role, filters_changed = normalize_lead_view_filters(view.filters)
        values = {}
        if filters_changed:
            values["filters"] = json.dumps(filters, ensure_ascii=False)
        for fieldname, normalizer in (
            ("columns", normalize_lead_view_columns),
            ("rows", normalize_lead_view_rows),
        ):
            raw_value = view.get(fieldname)
            try:
                parsed_value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
            except (TypeError, ValueError):
                parsed_value = None
            normalized_value = normalizer(raw_value)
            if parsed_value != normalized_value:
                values[fieldname] = json.dumps(normalized_value, ensure_ascii=False)
        if role and view.route_name == "Leads":
            values["route_name"] = "Buyers" if role == "Buyer" else "Sellers"
        if not values:
            continue
        frappe.db.set_value(
            "CRM View Settings",
            view.name,
            values,
            update_modified=False,
        )
        updated += 1
    return updated


def _clear_legacy_role_values():
    for fieldname in LEGACY_ROLE_FIELDS:
        if not frappe.db.has_column("CRM Lead", fieldname):
            continue
        Lead = frappe.qb.DocType("CRM Lead")
        field = getattr(Lead, fieldname)
        (
            frappe.qb.update(Lead)
            .set(field, None)
            .where(field.isnotnull())
        ).run()


def reconcile_party_roles():
    """Populate the canonical field from the strongest available evidence."""
    summary = {
        "updated": 0,
        "defaulted": 0,
        "conflicts": 0,
        "invalid_values": 0,
        "relationship_repairs": 0,
    }
    if not frappe.db.exists("DocType", "CRM Lead"):
        return summary
    if not frappe.db.has_column("CRM Lead", "party_type"):
        return summary

    alias_fields = [
        fieldname
        for fieldname in LEGACY_ROLE_FIELDS
        if frappe.db.has_column("CRM Lead", fieldname)
    ]
    unit_owners = _linked_leads("Real Estate Unit", "owner_lead")
    interest_leads = (
        _linked_leads("Lead Interest", "lead")
        | _legacy_interest_leads()
        | _linked_leads("Unit Scheduled Showing", "buyer_lead")
    )
    for lead in _lead_role_rows(alias_fields):
        raw_canonical = lead.get("party_type")
        canonical = normalize_party_role(raw_canonical)
        raw_aliases = {
            fieldname: lead.get(fieldname)
            for fieldname in alias_fields
            if lead.get(fieldname)
        }
        aliases = {
            role
            for role in (
                normalize_party_role(value) for value in raw_aliases.values()
            )
            if role
        }
        invalid_values = {
            fieldname: value
            for fieldname, value in {
                "party_type": raw_canonical,
                **raw_aliases,
            }.items()
            if value and not normalize_party_role(value)
        }

        if invalid_values:
            summary["invalid_values"] += 1
            _log_issue(
                lead.name,
                f"discarded invalid role values={invalid_values!r}",
            )

        role, reason = resolve_party_role(
            raw_canonical,
            raw_aliases.values(),
            owns_unit=lead.name in unit_owners,
            has_interest=lead.name in interest_leads,
        )
        if reason in {"relationship_conflict", "legacy_alias_conflict"}:
            summary["conflicts"] += 1
            _log_issue(
                lead.name,
                f"Party Role evidence conflict: canonical={canonical!r}, aliases={sorted(aliases)!r}, owns_unit={lead.name in unit_owners}, has_interest={lead.name in interest_leads}",
            )
        if role not in CANONICAL_ROLES:
            role = "Buyer"
            summary["defaulted"] += 1
        if reason in {
            "unit_owner",
            "lead_interest",
            "relationship_conflict",
        } and canonical != role:
            summary["relationship_repairs"] += 1
            if reason != "relationship_conflict":
                _log_issue(
                    lead.name,
                    f"repaired party_type from {canonical!r} to {role!r} using {reason} evidence",
                )
        if lead.party_type != role:
            frappe.db.set_value(
                "CRM Lead",
                lead.name,
                "party_type",
                role,
                update_modified=False,
            )
            summary["updated"] += 1

    return summary


def _remove_aliases_from_crm_layouts():
    if not frappe.db.exists("DocType", "CRM Fields Layout"):
        return
    aliases = set(LEGACY_ROLE_FIELDS)
    for row in frappe.get_all(
        "CRM Fields Layout",
        filters={"dt": "CRM Lead"},
        fields=["name", "layout"],
        limit_page_length=0,
    ):
        try:
            layout = frappe.parse_json(row.layout) if row.layout else []
        except (TypeError, ValueError):
            _log_issue(row.name, "could not parse CRM Fields Layout JSON")
            continue
        if not isinstance(layout, list):
            continue
        changed = remove_fields_from_layout(layout, aliases)
        if changed:
            frappe.db.set_value(
                "CRM Fields Layout",
                row.name,
                "layout",
                json.dumps(layout),
                update_modified=False,
            )


def _remove_aliases_from_quick_filters():
    if not frappe.db.exists("DocType", "CRM Global Settings"):
        return
    aliases = set(LEGACY_ROLE_FIELDS)
    for row in frappe.get_all(
        "CRM Global Settings",
        filters={"dt": "CRM Lead", "type": "Quick Filters"},
        fields=["name", "json"],
        limit_page_length=0,
    ):
        try:
            values = frappe.parse_json(row.json) if row.json else []
        except (TypeError, ValueError):
            _log_issue(row.name, "could not parse Quick Filters JSON")
            continue
        filtered = remove_fields_from_quick_filters(values, aliases)
        if filtered is None:
            _log_issue(row.name, "Quick Filters JSON was not a list of field names")
            continue
        if filtered != values:
            frappe.db.set_value(
                "CRM Global Settings",
                row.name,
                "json",
                json.dumps(filtered),
                update_modified=False,
            )


def _set_standard_alias_hidden(fieldname):
    meta = frappe.get_meta("CRM Lead")
    if not meta.has_field(fieldname):
        return
    for property_name in ("hidden", "read_only"):
        filters = {
            "doc_type": "CRM Lead",
            "field_name": fieldname,
            "property": property_name,
        }
        name = frappe.db.get_value("Property Setter", filters, "name")
        doc = frappe.get_doc("Property Setter", name) if name else frappe.new_doc("Property Setter")
        if not name:
            doc.doc_type = "CRM Lead"
            doc.field_name = fieldname
            doc.doctype_or_field = "DocField"
            doc.property = property_name
        doc.value = "1"
        doc.property_type = "Check"
        doc.save(ignore_permissions=True)


def remove_legacy_role_metadata():
    """Delete legacy Custom Fields after their values have been reconciled."""
    if not frappe.db.exists("DocType", "CRM Lead"):
        return

    _remove_aliases_from_crm_layouts()
    _remove_aliases_from_quick_filters()

    for fieldname in LEGACY_ROLE_FIELDS:
        custom_field_name = f"CRM Lead-{fieldname}"
        if frappe.db.exists("Custom Field", custom_field_name):
            frappe.delete_doc(
                "Custom Field",
                custom_field_name,
                ignore_permissions=True,
            )
        else:
            _set_standard_alias_hidden(fieldname)

    frappe.clear_cache(doctype="CRM Lead")


def enforce_canonical_party_role_schema():
    """Idempotent install/upgrade entry point."""
    summary = reconcile_party_roles()
    summary["view_routes_updated"] = _migrate_lead_view_routes()
    _clear_legacy_role_values()
    remove_legacy_role_metadata()
    return summary
