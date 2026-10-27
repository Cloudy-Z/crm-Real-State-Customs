from datetime import date

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


# Only Developer Name is mandatory (it is also the record name). Every other
# Developer field and every Founders row field is optional, so Data Import and
# quick entry never fail for missing optional details.
#
# Founded In (fieldname kept as ``founded_year`` for compatibility) is an
# optional Date. Historical whole-year values are migrated to 1 January of that
# year by the pre-model patch ``convert_developer_founded_year_to_date``.
MIN_FOUNDED_DATE = date(1700, 1, 1)


def _clean_text(value):
    return " ".join(str(value or "").split())


class PropertyDeveloper(Document):
    def validate(self):
        self.drop_empty_founder_rows()
        if self.founded_year:
            founded = getdate(self.founded_year)
            today = getdate()
            if founded < MIN_FOUNDED_DATE or founded > today:
                frappe.throw(
                    _("Founded In must be between {0} and {1}.").format(
                        MIN_FOUNDED_DATE.isoformat(), today.isoformat()
                    ),
                    frappe.ValidationError,
                )
        founder_names = [
            _clean_text(row.founder_name).casefold()
            for row in (self.get("founders") or [])
            if _clean_text(row.founder_name)
        ]
        if len(founder_names) != len(set(founder_names)):
            frappe.throw(_("Founder rows cannot contain duplicate names."), frappe.ValidationError)

    def drop_empty_founder_rows(self):
        """Remove Founders rows where both Founder Name and Role / Title are blank."""
        rows = list(self.get("founders") or [])
        kept = [
            row for row in rows if _clean_text(row.founder_name) or _clean_text(row.role_title)
        ]
        if len(kept) == len(rows):
            return
        self.set("founders", kept)
        for index, row in enumerate(kept, start=1):
            row.idx = index
