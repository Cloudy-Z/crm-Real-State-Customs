"""Prepare Property Developer.founded_year for its Int -> Date fieldtype change.

This is a ``pre_model_sync`` patch: it runs before Frappe syncs the DocType JSON.
MariaDB refuses to convert whole-year integers such as ``2005`` straight into a
DATE column (strict mode raises "Incorrect date value"), which would abort the
migrate. So the column is first made a nullable text column, every valid year
becomes ``YYYY-01-01``, and empty or invalid values (for example ``0``) become
NULL. The normal DocType sync then changes the column to DATE without errors.

The patch is idempotent, never deletes a Developer, and does nothing on fresh
installs or once the column is already a DATE.
"""

import re
from datetime import date

import frappe

DOCTYPE = "Property Developer"
FIELDNAME = "founded_year"
MIN_YEAR = 1700


def execute():
    if not frappe.db.table_exists(DOCTYPE) or not frappe.db.has_column(DOCTYPE, FIELDNAME):
        return
    column_type = str(frappe.db.get_column_type(DOCTYPE, FIELDNAME) or "").lower()
    if "date" in column_type:
        return
    if "int" in column_type:
        frappe.db.change_column_type(DOCTYPE, FIELDNAME, "varchar(10)", nullable=True)

    table = f"tab{DOCTYPE}"
    converted = cleared = 0
    rows = frappe.db.sql(f"select `name`, `{FIELDNAME}` from `{table}`", as_dict=True)
    for row in rows:
        current = row.get(FIELDNAME)
        target = year_to_date_text(current)
        if target == (str(current).strip() if current is not None else None):
            continue
        frappe.db.sql(
            f"update `{table}` set `{FIELDNAME}` = %s where `name` = %s",
            (target, row.get("name")),
        )
        if target:
            converted += 1
        else:
            cleared += 1
    print(
        f"Property Developer Founded In: {converted} year(s) converted to dates, "
        f"{cleared} empty/invalid value(s) cleared."
    )


def year_to_date_text(value, today=None):
    """Return an ISO date string for a stored founded value, or None.

    ``2005`` -> ``"2005-01-01"``; an existing valid ISO date is kept; empty,
    zero, out-of-range, and unparseable values return None.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    current_year = (today or date.today()).year
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            return None
        return text if MIN_YEAR <= parsed.year <= current_year else None
    if re.fullmatch(r"\d{1,4}", text):
        year = int(text)
        if MIN_YEAR <= year <= current_year:
            return f"{year:04d}-01-01"
    return None
