"""Rename the Seller party role to Owner and move Rental inventory under Resale.

Additive and idempotent:

* ``CRM Lead.party_type`` values ``Seller`` become ``Owner``.
* Saved CRM list views that filtered ``party_type = Seller`` or used the
  ``Sellers`` route are repointed to ``Owner`` / ``Owners``.
* Units with the retired inventory type ``Rental`` become ``Resale`` listed
  ``For Rent``; every other Unit without a Listing Purpose becomes ``For Sale``.
* Lead Interests without a Deal Type become ``Buy`` and get their Unit Category
  from their requested Unit Type.

Nothing is deleted. Values are written directly (no controllers run) and
``modified`` timestamps are left untouched so audit history is not rewritten.
Rows that need a person to look at them are written to the Error Log.
"""

import frappe

from real_estate_crm_customs.party_roles import LEGACY_OWNER_ROLE, OWNER_ROLE

LEGACY_RENTAL_INVENTORY = "Rental"
LOG_TITLE = "Owner / Rent migration review"


def execute():
    rename_seller_party_role()
    repoint_saved_lead_views()
    move_rental_units_under_resale()
    backfill_unit_listing_purpose()
    backfill_interest_deal_type()
    backfill_interest_unit_category()
    frappe.clear_cache()


def _has_columns(doctype, *fieldnames):
    return frappe.db.exists("DocType", doctype) and all(
        frappe.db.has_column(doctype, fieldname) for fieldname in fieldnames
    )


def _log(message):
    frappe.log_error(title=LOG_TITLE, message=message)


def rename_seller_party_role():
    if not _has_columns("CRM Lead", "party_type"):
        return 0
    names = frappe.get_all(
        "CRM Lead", filters={"party_type": LEGACY_OWNER_ROLE}, pluck="name", limit_page_length=0
    )
    for name in names:
        frappe.db.set_value("CRM Lead", name, "party_type", OWNER_ROLE, update_modified=False)
    return len(names)


def repoint_saved_lead_views():
    """Reuse the Party Role view normalizer so filters, columns and routes stay consistent."""
    from real_estate_crm_customs.lead_role_migration import _migrate_lead_view_routes

    return _migrate_lead_view_routes()


def move_rental_units_under_resale():
    if not _has_columns("Real Estate Unit", "inventory_type", "listing_purpose"):
        return 0
    rows = frappe.get_all(
        "Real Estate Unit",
        filters={"inventory_type": LEGACY_RENTAL_INVENTORY},
        fields=["name", "owner_lead", "rental_monthly_rate", "rental_daily_rate"],
        limit_page_length=0,
    )
    for unit in rows:
        frappe.db.set_value(
            "Real Estate Unit",
            unit.name,
            {"inventory_type": "Resale", "listing_purpose": "For Rent"},
            update_modified=False,
        )
        problems = []
        if not unit.owner_lead:
            problems.append("no Owner Lead")
        if not (unit.rental_monthly_rate or 0) and not (unit.rental_daily_rate or 0):
            problems.append("no monthly or daily rent")
        if problems:
            _log(f"Unit {unit.name} moved from Rental to Resale / For Rent but has {', '.join(problems)}.")
    return len(rows)


def backfill_unit_listing_purpose():
    if not _has_columns("Real Estate Unit", "listing_purpose"):
        return 0
    names = frappe.get_all(
        "Real Estate Unit",
        filters={"listing_purpose": ["is", "not set"]},
        pluck="name",
        limit_page_length=0,
    )
    for name in names:
        frappe.db.set_value("Real Estate Unit", name, "listing_purpose", "For Sale", update_modified=False)
    return len(names)


def backfill_interest_deal_type():
    if not _has_columns("Lead Interest", "deal_type"):
        return 0
    names = frappe.get_all(
        "Lead Interest", filters={"deal_type": ["is", "not set"]}, pluck="name", limit_page_length=0
    )
    for name in names:
        frappe.db.set_value("Lead Interest", name, "deal_type", "Buy", update_modified=False)
    return len(names)


def backfill_interest_unit_category():
    if not _has_columns("Lead Interest", "requested_unit_type", "requested_unit_category"):
        return 0
    if not _has_columns("Real Estate Unit Type", "unit_category"):
        return 0
    categories = {
        row.name: row.unit_category
        for row in frappe.get_all(
            "Real Estate Unit Type", fields=["name", "unit_category"], limit_page_length=0
        )
        if row.unit_category
    }
    updated = 0
    for row in frappe.get_all(
        "Lead Interest",
        filters={"requested_unit_type": ["is", "set"], "requested_unit_category": ["is", "not set"]},
        fields=["name", "requested_unit_type"],
        limit_page_length=0,
    ):
        category = categories.get(row.requested_unit_type)
        if category:
            frappe.db.set_value(
                "Lead Interest", row.name, "requested_unit_category", category, update_modified=False
            )
            updated += 1
    return updated
