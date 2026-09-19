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
