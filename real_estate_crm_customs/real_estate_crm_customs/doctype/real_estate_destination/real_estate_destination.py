import re

import frappe
from frappe import _
from frappe.model.document import Document


class RealEstateDestination(Document):
    def validate(self):
        self.destination_name = " ".join(str(self.destination_name or "").split())
        if not self.destination_name:
            frappe.throw(_("Destination Name is mandatory."))
        if self.destination_code:
            normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", str(self.destination_code).strip()).strip("-")
            self.destination_code = normalized.upper()
