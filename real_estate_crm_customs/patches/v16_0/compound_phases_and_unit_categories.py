"""Compound Phases and Unit Categories (additive, idempotent).

* Every existing Compound record becomes ``record_type = "Compound"``.
* Every existing Unit gets ``master_compound`` (its Compound, or the Phase's
  Parent Compound) and ``unit_category`` from its Unit Type.
* The default Unit Category / Unit Type taxonomy is seeded once.

Nothing is deleted, renamed, or committed here, and ``modified`` timestamps are
left untouched so audit history is not rewritten.
"""

import frappe

from real_estate_crm_customs.master_data import ensure_default_unit_categories


def execute():
    backfill_compound_record_types()
    backfill_unit_master_compounds()
    ensure_default_unit_categories()
    backfill_unit_categories()
    frappe.clear_cache()


def _has_columns(doctype, *fieldnames):
    return frappe.db.exists("DocType", doctype) and all(
        frappe.db.has_column(doctype, fieldname) for fieldname in fieldnames
    )


def backfill_compound_record_types():
    if not _has_columns("Real Estate Project", "record_type"):
        return
    for row in frappe.get_all(
        "Real Estate Project", fields=["name", "record_type"], limit_page_length=0
    ):
        if row.record_type in ("Compound", "Phase"):
            continue
        frappe.db.set_value(
            "Real Estate Project", row.name, "record_type", "Compound", update_modified=False
        )


def backfill_unit_master_compounds():
    if not _has_columns("Real Estate Unit", "project", "master_compound"):
        return
    if not _has_columns("Real Estate Project", "record_type", "parent_compound"):
        return
    compounds = {
        row.name: row
        for row in frappe.get_all(
            "Real Estate Project",
            fields=["name", "record_type", "parent_compound"],
            limit_page_length=0,
        )
    }
    for unit in frappe.get_all(
        "Real Estate Unit", fields=["name", "project", "master_compound"], limit_page_length=0
    ):
        compound = compounds.get(unit.project)
        if not compound:
            continue
        is_phase = compound.record_type == "Phase" and compound.parent_compound
        master = compound.parent_compound if is_phase else compound.name
        if unit.master_compound != master:
            frappe.db.set_value(
                "Real Estate Unit", unit.name, "master_compound", master, update_modified=False
            )


def backfill_unit_categories():
    if not _has_columns("Real Estate Unit", "physical_unit_type", "unit_category"):
        return
    if not _has_columns("Real Estate Unit Type", "unit_category"):
        return
    categories = {
        row.name: row.unit_category
        for row in frappe.get_all(
            "Real Estate Unit Type", fields=["name", "unit_category"], limit_page_length=0
        )
        if row.unit_category
    }
    for unit in frappe.get_all(
        "Real Estate Unit",
        fields=["name", "physical_unit_type", "unit_category"],
        limit_page_length=0,
    ):
        category = categories.get(unit.physical_unit_type)
        if category and unit.unit_category != category:
            frappe.db.set_value(
                "Real Estate Unit", unit.name, "unit_category", category, update_modified=False
            )
