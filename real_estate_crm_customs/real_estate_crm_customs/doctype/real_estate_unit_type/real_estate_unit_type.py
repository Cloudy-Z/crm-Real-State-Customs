import re

import frappe
from frappe import _
from frappe.model.document import Document


class RealEstateUnitType(Document):
    def validate(self):
        self.unit_type_name = " ".join(str(self.unit_type_name or "").split())
        abbreviation = re.sub(r"[^A-Za-z0-9]", "", str(self.abbreviation or "")).upper()
        if not abbreviation or len(abbreviation) > 4:
            frappe.throw(_("Abbreviation must contain one to four letters or numbers."))
        self.abbreviation = abbreviation
