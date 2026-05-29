import frappe
from frappe import _
from frappe.model.document import Document


class RealEstateUnit(Document):
    def validate(self):
        validate_resale_owner(self, None)


def validate_resale_owner(doc, method=None):
    if doc.unit_type == "Resale" and not doc.owner_lead:
        frappe.throw(
            _("Owner Lead is mandatory when Unit Type is Resale."),
            frappe.ValidationError,
        )
