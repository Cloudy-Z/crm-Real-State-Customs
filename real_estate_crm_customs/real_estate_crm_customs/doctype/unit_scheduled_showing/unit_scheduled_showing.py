import frappe
from frappe import _
from frappe.model.document import Document


ACTIVE_SHOWING_STATUSES = {"Scheduled", "Rescheduled"}


class UnitScheduledShowing(Document):
    def validate(self):
        if not self.buyer_lead:
            return
        requires_validation = (
            self.is_new()
            or self.has_value_changed("buyer_lead")
            or (
                self.has_value_changed("status")
                and self.status in ACTIVE_SHOWING_STATUSES
            )
        )
        if not requires_validation:
            return
        party_type = frappe.db.get_value(
            "CRM Lead",
            self.buyer_lead,
            "party_type",
        )
        if party_type != "Buyer":
            frappe.throw(
                _("Buyer Lead must have Party Role = Buyer."),
                frappe.ValidationError,
            )
