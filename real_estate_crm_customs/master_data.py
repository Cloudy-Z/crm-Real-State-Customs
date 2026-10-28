"""Idempotent master-data setup used by installation and upgrade migrations."""

import frappe


DEFAULT_UNIT_TYPES = {
    "Villa": "VIL",
    "Chalet": "CHL",
    "Apartment": "APT",
    "Duplex": "DUP",
    "Penthouse": "PEN",
    "Studio": "STD",
    "Townhouse": "TWN",
}
UNIT_TYPE_ALIASES = {"Standalone Villa": "Villa"}


def ensure_default_unit_types():
    if not frappe.db.exists("DocType", "Real Estate Unit Type"):
        return
    for name, abbreviation in DEFAULT_UNIT_TYPES.items():
        if frappe.db.exists("Real Estate Unit Type", name):
            continue
        frappe.get_doc(
            {
                "doctype": "Real Estate Unit Type",
                "unit_type_name": name,
                "abbreviation": abbreviation,
                "is_active": 1,
            }
        ).insert(ignore_permissions=True)


def ensure_legacy_unit_type(value):
    legacy_type = " ".join(str(value or "").strip().split())
    if not legacy_type:
        return None
    canonical_type = UNIT_TYPE_ALIASES.get(legacy_type, legacy_type)
    if frappe.db.exists("Real Estate Unit Type", canonical_type):
        return canonical_type
    abbreviation = "".join(
        character for character in canonical_type if character.isalnum()
    )[:4].upper() or "UNIT"
    frappe.get_doc(
        {
            "doctype": "Real Estate Unit Type",
            "unit_type_name": canonical_type,
            "abbreviation": abbreviation,
            "is_active": 1,
        }
    ).insert(ignore_permissions=True)
    return canonical_type


def ensure_default_unit_categories():
    """Create the default Unit Category / Unit Type taxonomy.

    This is called once per site (install, or the upgrade patch), not on every
    migrate, so categories or types a user later deletes are not re-created.
    Existing records are never renamed or overwritten; an existing Unit Type
    only gets a category when it has none.
    """
    result = {"categories": 0, "unit_types": 0, "assigned": 0}
    if not frappe.db.exists("DocType", "Real Estate Unit Category"):
        return result
    from real_estate_crm_customs.unit_taxonomy import DEFAULT_UNIT_CATEGORIES

    has_category_field = frappe.get_meta("Real Estate Unit Type").has_field("unit_category")
    for category_name, category_abbreviation, unit_types in DEFAULT_UNIT_CATEGORIES:
        if not frappe.db.exists("Real Estate Unit Category", category_name):
            frappe.get_doc(
                {
                    "doctype": "Real Estate Unit Category",
                    "category_name": category_name,
                    "abbreviation": category_abbreviation,
                    "is_active": 1,
                }
            ).insert(ignore_permissions=True)
            result["categories"] += 1
        if not has_category_field:
            continue
        for type_name, abbreviation in unit_types:
            existing = frappe.db.get_value(
                "Real Estate Unit Type", type_name, ["name", "unit_category"], as_dict=True
            )
            if existing:
                if not existing.unit_category:
                    frappe.db.set_value(
                        "Real Estate Unit Type",
                        existing.name,
                        "unit_category",
                        category_name,
                        update_modified=False,
                    )
                    result["assigned"] += 1
                continue
            frappe.get_doc(
                {
                    "doctype": "Real Estate Unit Type",
                    "unit_type_name": type_name,
                    "unit_category": category_name,
                    "abbreviation": abbreviation,
                    "is_active": 1,
                }
            ).insert(ignore_permissions=True)
            result["unit_types"] += 1
    return result
