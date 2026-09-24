"""One-time compatibility migration into CRM Lead.party_type.

The canonical Party Role field is ``party_type``. Historical role aliases are read
only during this migration, then their Custom Field and CRM layout metadata are
removed so runtime code has one source of truth. Database columns are intentionally
left to Frappe's normal schema-retention policy.
"""

import json

import frappe

from real_estate_crm_customs.party_roles import (
    normalize_party_role,
    remove_fields_from_layout,
    remove_fields_from_quick_filters,
)


LEGACY_ROLE_FIELDS = ("custom_type", "lead_type")
CANONICAL_ROLES = {"Buyer", "Seller"}


def _log_issue(lead_name, message):
    frappe.log_error(
        title="CRM Lead Party Role migration requires review",
        message=f"{lead_name}: {message}",
    )


def reconcile_party_roles():
    """Populate the canonical field without allowing aliases to override it."""
    summary = {
        "updated": 0,
        "defaulted": 0,
        "conflicts": 0,
        "invalid_values": 0,
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
    fields = ["name", "party_type", *alias_fields]
    for lead in frappe.get_all("CRM Lead", fields=fields, limit_page_length=0):
        raw_canonical = lead.get("party_type")
        canonical = normalize_party_role(raw_canonical)
        raw_aliases = {
            fieldname: lead.get(fieldname)
            for fieldname in alias_fields
            if lead.get(fieldname)
        }
        aliases = {
            normalize_party_role(value)
            for value in raw_aliases.values()
        }
        aliases.discard(None)
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

        if canonical in CANONICAL_ROLES:
            if aliases and aliases != {canonical}:
                summary["conflicts"] += 1
                _log_issue(
                    lead.name,
                    f"kept canonical party_type={canonical!r}; ignored legacy aliases={sorted(aliases)!r}",
                )
            if lead.party_type != canonical:
                frappe.db.set_value(
                    "CRM Lead",
                    lead.name,
                    "party_type",
                    canonical,
                    update_modified=False,
                )
                summary["updated"] += 1
            continue

        if len(aliases) == 1:
            role = next(iter(aliases))
            if raw_canonical and not canonical:
                _log_issue(
                    lead.name,
                    f"repaired invalid party_type={raw_canonical!r} from unambiguous legacy role={role!r}",
                )
            frappe.db.set_value(
                "CRM Lead",
                lead.name,
                "party_type",
                role,
                update_modified=False,
            )
            summary["updated"] += 1
            continue

        if len(aliases) > 1:
            summary["conflicts"] += 1
            _log_issue(
                lead.name,
                f"conflicting legacy aliases={sorted(aliases)!r}; defaulted canonical Party Role to Buyer",
            )
        elif invalid_values:
            _log_issue(
                lead.name,
                "no unambiguous recognized role remained; defaulted canonical Party Role to Buyer",
            )

        frappe.db.set_value(
            "CRM Lead",
            lead.name,
            "party_type",
            "Buyer",
            update_modified=False,
        )
        summary["defaulted"] += 1

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
    remove_legacy_role_metadata()
    return summary
