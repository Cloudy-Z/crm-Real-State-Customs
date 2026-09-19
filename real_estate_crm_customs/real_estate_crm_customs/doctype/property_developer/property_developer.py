from datetime import date

import frappe
from frappe import _
from frappe.model.document import Document


class PropertyDeveloper(Document):
    def validate(self):
        if self.is_new() and not self.founded_year:
            frappe.throw(_("Founded In is mandatory for a new Developer."), frappe.ValidationError)
        if self.is_new() and not self.founders:
            frappe.throw(_("A Developer requires at least one founder."), frappe.ValidationError)
        if self.founded_year:
            current_year = date.today().year
            if int(self.founded_year) < 1700 or int(self.founded_year) > current_year:
                frappe.throw(
                    _("Founded In must be between 1700 and {0}.").format(current_year),
                    frappe.ValidationError,
                )
        founder_names = [
            " ".join(str(row.founder_name or "").split()).casefold()
            for row in (self.founders or [])
            if row.founder_name
        ]
        if len(founder_names) != len(set(founder_names)):
            frappe.throw(_("Founder rows cannot contain duplicate names."), frappe.ValidationError)
