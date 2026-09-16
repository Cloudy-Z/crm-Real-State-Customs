import frappe
from frappe import _
from frappe.model.document import Document


TERMINAL_STATUSES = {"Rejected", "Superseded", "Fulfilled", "Cancelled"}
INVENTORY_STATUSES = {
    "Matched",
    "Offer Sent",
    "Offer Viewed",
    "Offer Accepted",
    "Negotiating",
    "Showing Scheduled",
    "Shown - Interested",
    "Shown - Considering",
    "Rejected",
    "Superseded",
    "Cancelled",
}


class LeadInterest(Document):
    def validate(self):
        if self.record_type == "Inventory Unit" and not self.unit:
            frappe.throw(_("Inventory Lead Interests require a Real Estate Unit."))
        if self.record_type != "Inventory Unit" and self.unit:
            frappe.throw(_("Only Inventory Unit interests may link a Real Estate Unit."))
        if self.record_type == "Request" and self.workflow_status not in {
            "Requested",
            "Matched",
            "Superseded",
            "Fulfilled",
            "Cancelled",
        }:
            frappe.throw(_("Requested interests cannot use an inventory offer status."))
        if self.record_type == "Inventory Unit" and self.workflow_status not in INVENTORY_STATUSES:
            frappe.throw(_("Invalid workflow status for an inventory interest."))
        self.is_active = 0 if self.workflow_status in TERMINAL_STATUSES else 1

    def on_trash(self):
        if not getattr(self.flags, "approved_interest_deletion", False):
            frappe.throw(
                _("Lead Interests cannot be deleted directly. Use the manager approval workflow."),
                frappe.PermissionError,
            )
