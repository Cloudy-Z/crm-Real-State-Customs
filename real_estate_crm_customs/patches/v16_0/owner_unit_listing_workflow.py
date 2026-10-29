"""Initialize the Owner Unit listing workflow after its DocType fields exist.

This patch is intentionally conservative: it never invents ownership papers,
listing agreements, private attachments, or price facts. Existing Owner Units
must complete the new guided evidence gate before buyers can see them.
"""

import frappe


def _price_present(row):
    purpose = row.get("listing_purpose") or "For Sale"
    sale_required = purpose in ("For Sale", "For Sale or Rent")
    rent_required = purpose in ("For Rent", "For Sale or Rent")
    sale_ok = not sale_required or float(row.get("total_price_net") or 0) > 0 or float(row.get("price") or 0) > 0
    rent_ok = not rent_required or float(row.get("rental_monthly_rate") or 0) > 0 or float(row.get("rental_daily_rate") or 0) > 0
    return sale_ok and rent_ok


def execute():
    if not frappe.db.exists("DocType", "Real Estate Unit"):
        return {"updated": 0, "skipped": 0}
    required_columns = ("listing_status", "pricing_method", "active_buyer_interest_count", "total_buyer_interest_count")
    if not all(frappe.db.has_column("Real Estate Unit", column) for column in required_columns):
        return {"updated": 0, "skipped": 0}

    from real_estate_crm_customs.owner_workflow import reconcile_owner_workflow

    updated = skipped = 0
    rows = frappe.get_all(
        "Real Estate Unit",
        filters={"owner_lead": ["is", "set"]},
        fields=[
            "name", "status", "listing_status", "listing_purpose", "total_price_net", "price",
            "rental_monthly_rate", "rental_daily_rate", "pricing_method",
        ],
        limit_page_length=0,
    )
    for index, row in enumerate(rows):
        if row.get("listing_status"):
            skipped += 1
            continue
        savepoint = "owner_listing_backfill_{0}".format(index)
        try:
            frappe.db.savepoint(savepoint)
            values = {"pricing_method": row.get("pricing_method") or ("Owner Price" if _price_present(row) else "Agent Pricing")}
            if row.get("status") in ("Sold", "Rented"):
                values.update({"listing_status": "Withdrawn", "listing_status_reason": "Historical completed Unit; not reopened by migration."})
            elif _price_present(row):
                values.update({"listing_status": "Documents Pending", "listing_status_reason": "Historical Owner Unit requires document and listing-agreement verification."})
            else:
                values.update({"listing_status": "Pricing Requested", "listing_status_reason": "Historical Owner Unit requires pricing."})
            frappe.db.set_value("Real Estate Unit", row.name, values, update_modified=False)
            frappe.db.release_savepoint(savepoint)
            updated += 1
        except Exception:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                pass
            frappe.log_error(frappe.get_traceback(), "Owner Unit listing workflow migration skipped")
            skipped += 1
    # Refreshes distinct Buyer counts and Owner Lead rollups; all failures are
    # contained per record by the service so prior Unit data is never rolled back.
    reconcile_owner_workflow()
    return {"updated": updated, "skipped": skipped}
