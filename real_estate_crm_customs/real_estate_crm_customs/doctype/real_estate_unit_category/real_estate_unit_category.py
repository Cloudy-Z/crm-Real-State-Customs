import re

import frappe
from frappe import _
from frappe.model.document import Document


class RealEstateUnitCategory(Document):
    def validate(self):
        self.category_name = " ".join(str(self.category_name or "").split())
        if not self.category_name:
            frappe.throw(_("Category Name is mandatory."))
        if self.abbreviation:
            abbreviation = re.sub(r"[^A-Za-z0-9]", "", str(self.abbreviation)).upper()
            if len(abbreviation) > 6:
                frappe.throw(_("Category Abbreviation can contain at most six letters or numbers."))
            self.abbreviation = abbreviation or None
