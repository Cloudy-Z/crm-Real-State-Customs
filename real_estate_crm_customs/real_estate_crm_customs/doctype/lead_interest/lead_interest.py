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
        binding_changed = self.is_new() or any(
            self.has_value_changed(fieldname)
            for fieldname in ("lead", "record_type", "category", "unit")
        )
        request_changed = self.is_new() or any(
            self.has_value_changed(fieldname)
            for fieldname in (
                "record_type",
                "category",
                "requested_destination",
                "requested_area",
                "requested_project",
                "requested_unit_type",
            )
        )
        if binding_changed:
            self._validate_binding()
        if (
            request_changed
            and self.record_type == "Request"
            and self.category in {"Resale", "Primary"}
            and not self.requested_destination
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
        expected_request_status = {
            "Requested": "Open",
            "Fulfilled": "Fulfilled",
            "Cancelled": "Cancelled",
            "Superseded": "Cancelled",
        }.get(self.workflow_status)
        if self.record_type == "Request" and expected_request_status:
            self.request_status = expected_request_status
        if self.record_type == "Inventory Unit" and self.workflow_status not in INVENTORY_STATUSES:
            frappe.throw(_("Invalid workflow status for an inventory interest."))
        self.is_active = 0 if self.workflow_status in TERMINAL_STATUSES else 1

    def _validate_binding(self):
        party_type = frappe.db.get_value("CRM Lead", self.lead, "party_type")
        if party_type != "Buyer":
            frappe.throw(_("Lead Interests may only belong to Buyer leads."), frappe.ValidationError)
        valid_categories = {
            "Inventory Unit": {"Resale", "Primary"},
            "Request": {"Resale", "Primary", "Brokerage Request"},
            "Outsource": {"Outsource"},
            "International": {"International"},
        }
        if self.category not in valid_categories.get(self.record_type, set()):
            frappe.throw(
                _("Category {0} is not valid for Interest record type {1}.").format(
                    self.category,
                    self.record_type,
                ),
                frappe.ValidationError,
            )
        if self.record_type == "Inventory Unit" and not self.unit:
            frappe.throw(_("Inventory Lead Interests require a Real Estate Unit."))
        if self.record_type != "Inventory Unit" and self.unit:
            frappe.throw(_("Only Inventory Unit interests may link a Real Estate Unit."))
        if self.record_type != "Inventory Unit":
            return
        unit = frappe.db.get_value(
            "Real Estate Unit",
            self.unit,
            ["inventory_type", "owner_lead"],
            as_dict=True,
        )
        if not unit:
            frappe.throw(_("Real Estate Unit {0} was not found.").format(self.unit), frappe.DoesNotExistError)
        if unit.inventory_type != self.category:
            frappe.throw(
                _("Interest category {0} does not match Unit inventory type {1}.").format(
                    self.category,
                    unit.inventory_type or _("blank"),
                )
            )
        if self.category == "Resale":
            owner_role = (
                frappe.db.get_value("CRM Lead", unit.owner_lead, "party_type")
                if unit.owner_lead
                else None
            )
            if owner_role != "Seller":
                frappe.throw(_("Resale Interests require a Unit owned by a Seller lead."))
        if self.category == "Primary" and unit.owner_lead:
            frappe.throw(_("Primary Interests cannot use a Seller-owned Unit."))

    def on_trash(self):
        if not getattr(self.flags, "approved_interest_deletion", False):
            frappe.throw(
                _("Lead Interests cannot be deleted directly. Use the manager approval workflow."),
                frappe.PermissionError,
            )
