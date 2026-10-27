from datetime import date

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


# Founded In (fieldname kept as ``founded_year`` for compatibility) is an
# optional Date. Historical whole-year values are migrated to 1 January of that
# year by the pre-model patch ``convert_developer_founded_year_to_date``.
MIN_FOUNDED_DATE = date(1700, 1, 1)


class PropertyDeveloper(Document):
    def validate(self):
        if self.is_new() and not self.founders:
            frappe.throw(_("A Developer requires at least one founder."), frappe.ValidationError)
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
            " ".join(str(row.founder_name or "").split()).casefold()
            for row in (self.founders or [])
            if row.founder_name
        ]
        if len(founder_names) != len(set(founder_names)):
            frappe.throw(_("Founder rows cannot contain duplicate names."), frappe.ValidationError)
