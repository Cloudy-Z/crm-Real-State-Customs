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
        if self.record_type == "Inventory Unit" and self.unit:
            inventory_type = frappe.db.get_value("Real Estate Unit", self.unit, "inventory_type")
            if inventory_type and self.category in {"Resale", "Primary"} and inventory_type != self.category:
                frappe.throw(
                    _("Interest category {0} does not match Unit inventory type {1}.").format(
                        self.category,
                        inventory_type,
                    )
                )
        if (
            self.record_type == "Request"
            and self.category in {"Resale", "Primary"}
            and not (self.requested_destination or self.requested_area)
        ):
            frappe.throw(_("A requested Resale or Primary Interest requires a Destination."))
        if (
            self.record_type == "Request"
            and self.requested_project
            and self.requested_unit_type
            and (self.is_new() or self.has_value_changed("requested_project") or self.has_value_changed("requested_unit_type"))
        ):
            allowed_types = frappe.get_all(
                "Real Estate Compound Unit Type",
                filters={
                    "parent": self.requested_project,
                    "parenttype": "Real Estate Project",
                    "is_available": 1,
                },
                pluck="unit_type",
            )
            if allowed_types and self.requested_unit_type not in allowed_types:
                frappe.throw(
                    _("Unit Type {0} is not available in Compound {1}.").format(
                        self.requested_unit_type,
                        self.requested_project,
                    )
                )
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
