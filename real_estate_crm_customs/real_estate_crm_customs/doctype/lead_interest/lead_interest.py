"""Authoritative Lead Interest controller safeguards.

The workflow service owns lifecycle state and contract evidence.  This controller
intentionally does not trust ``doc.flags`` because a client can submit those
values with a generic document write.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

from real_estate_crm_customs.party_roles import is_buyer_role, is_owner_role
from real_estate_crm_customs.requirement_policy import (
    DEAL_RENT,
    RequirementError,
    deal_type,
    delivery_date,
    is_property_requirement,
    money, requirement_amount,
)
from real_estate_crm_customs.workflow_context import (
    creation_context_mode,
    interest_mutation_active,
    require_interest_creation_context,
)


TERMINAL_STATUSES = {"Rejected", "Superseded", "Fulfilled", "Cancelled", "Closed Won"}
INVENTORY_STATUSES = {
    "Matched",
    "Offer Sent",
    "Offer Viewed",
    "Offer Accepted",
    "Negotiating",
    "Showing Scheduled",
    "Shown - Interested",
    "Shown - Considering",
    "Closing",
    "Closed Won",
    "Rejected",
    "Superseded",
    "Cancelled",
}
CREATION_STATUSES = {"Requested", "Matched"}
SERVER_MANAGED_MUTATION_FIELDS = {
    "workflow_status",
    "is_active",
    "latest_action",
    "last_transition_at",
    "last_transition_by",
    "offer_sent_at",
}
CONTRACT_EVIDENCE_FIELDS = {
    "contract_reference",
    "contract_signed_date",
    "signed_contract",
    "contract_recorded_by",
    "closed_won_at",
}


class LeadInterest(Document):
    def before_insert(self):
        # This checks request-local frappe.flags only; a submitted doc.flags is
        # deliberately irrelevant.
        require_interest_creation_context()
        mode = creation_context_mode()
        if mode != "migration" and self.workflow_status not in CREATION_STATUSES:
            frappe.throw(
                _("New Lead Interests must start as Requested or Matched through the workflow."),
                frappe.ValidationError,
            )

    def validate(self):
        self._validate_server_managed_mutations()
        binding_changed = self.is_new() or any(
            self.has_value_changed(fieldname)
            for fieldname in ("lead", "record_type", "category", "unit", "deal_type")
        )
        requirement_changed = self.is_new() or any(
            self.has_value_changed(fieldname)
            for fieldname in (
                "record_type",
                "category",
                "deal_type",
                "international_type",
                "requested_destination",
                "requested_area",
                "requested_project",
                "requested_developer",
                "requested_unit_category",
                "requested_unit_type",
                "requested_budget",
                "requested_budget_set",
                "requested_down_payment_budget",
                "requested_down_payment_budget_set",
                "requested_monthly_installment_budget",
                "requested_monthly_installment_budget_set",
                "requested_rent_budget",
                "requested_rent_budget_set",
                "requested_delivery_date",
                "inventory_search_scope",
                "budget_currency",
            )
        )
        if binding_changed:
            self._validate_binding()
        if requirement_changed:
            self._validate_requirement_snapshot()
        if self.record_type == "Request" and self.workflow_status not in {
            "Requested",
            "Matched",
            "Superseded",
            "Fulfilled",
            "Cancelled",
        }:
            frappe.throw(_("Requested interests cannot use an inventory offer or closing status."))
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
        self._validate_closed_won_evidence()
        self.is_active = 0 if self.workflow_status in TERMINAL_STATUSES else 1

    def _validate_server_managed_mutations(self):
        if self.is_new():
            return
        changed = {
            fieldname
            for fieldname in SERVER_MANAGED_MUTATION_FIELDS | CONTRACT_EVIDENCE_FIELDS
            if self.has_value_changed(fieldname)
        }
        if changed and not interest_mutation_active():
            frappe.throw(
                _("Interest lifecycle status and contract evidence can only be changed by the workflow service."),
                frappe.PermissionError,
            )
        # A signed record is immutable even to generic server saves.  The
        # closing service provides idempotent replay without changing it.
        if self.get_doc_before_save() and self.get_doc_before_save().get("workflow_status") == "Closed Won":
            immutable = {
                fieldname for fieldname in CONTRACT_EVIDENCE_FIELDS | {"workflow_status", "unit", "lead", "category", "record_type"}
                if self.has_value_changed(fieldname)
            }
            if immutable:
                frappe.throw(
                    _("Closed Won contract evidence and binding are immutable."),
                    frappe.ValidationError,
                )

    def _validate_requirement_snapshot(self):
        migration = self.is_new() and creation_context_mode() == "migration"
        try:
            self.deal_type = deal_type(self.get("deal_type"), self.category)
        except RequirementError as exc:
            frappe.throw(str(exc), frappe.ValidationError)
        renting = self.deal_type == DEAL_RENT
        try:
            self.requested_budget = money(
                requirement_amount(self, "requested_budget"),
                "Maximum Total Budget",
                required=(not migration and not renting and self.category in {"Resale", "Primary"}),
            )
            self.requested_rent_budget = money(
                requirement_amount(self, "requested_rent_budget"),
                "Maximum Monthly Rent",
                required=(not migration and renting and self.category == "Resale"),
            )
            self.requested_down_payment_budget = money(
                requirement_amount(self, "requested_down_payment_budget"), "Maximum Down Payment Budget"
            )
            self.requested_monthly_installment_budget = money(
                requirement_amount(self, "requested_monthly_installment_budget"), "Maximum Monthly Installment Budget"
            )
            self.requested_delivery_date = delivery_date(self.get("requested_delivery_date"))
        except RequirementError as exc:
            frappe.throw(str(exc), frappe.ValidationError)

        if renting:
            # Only owner-listed Resale Units can be rented, so a rental never searches Primary.
            self.inventory_search_scope = "Resale"
        if not self.get("inventory_search_scope"):
            self.inventory_search_scope = self.category if self.category in {"Resale", "Primary"} else "Both"
        if self.inventory_search_scope not in {"Resale", "Primary", "Both"}:
            frappe.throw(_("Inventory Search Scope must be Resale, Primary, or Both."))
        if not self.get("budget_currency"):
            self.budget_currency = _site_currency()

        property_requirement = is_property_requirement(
            self.category, self.get("international_type")
        )
        destination_required = property_requirement or (
            self.record_type == "International"
            and self.get("international_type") not in {"Stocks", "Charity Work"}
        )
        if not migration and destination_required and not self.get("requested_destination"):
            frappe.throw(_("A property Interest requires a Destination."), frappe.ValidationError)
        if not migration and self.record_type == "International" and not self.get("international_country"):
            frappe.throw(_("An International Interest requires a Country."), frappe.ValidationError)
        if not migration and self.category in {"Resale", "Primary"} and not self.get("requested_unit_type"):
            frappe.throw(_("A Resale or Primary Interest requires a Unit Type."), frappe.ValidationError)
        self.requirement_budget_presence_version = 1
        self._sync_requested_unit_category()
        self._validate_requirement_relations()

    def _sync_requested_unit_category(self):
        """The Unit Type is authoritative; the Category only narrows the Unit Type choice."""
        if not self.meta.has_field("requested_unit_category"):
            return
        unit_type = self.get("requested_unit_type")
        if not unit_type:
            return
        category = frappe.db.get_value("Real Estate Unit Type", unit_type, "unit_category")
        if not category:
            return
        selected = self.get("requested_unit_category")
        if selected and selected != category and not (self.is_new() and creation_context_mode() == "migration"):
            frappe.throw(
                _("Unit Type {0} belongs to Unit Category {1}, not {2}.").format(unit_type, category, selected),
                frappe.ValidationError,
            )
        self.requested_unit_category = category

    def _validate_requirement_relations(self):
        project = self.get("requested_project")
        if not project:
            return
        compound = frappe.db.get_value(
            "Real Estate Project", project, ["destination", "developer"], as_dict=True
        )
        if not compound:
            frappe.throw(_("Compound {0} was not found.").format(project), frappe.DoesNotExistError)
        if self.get("requested_destination") and compound.destination != self.requested_destination:
            frappe.throw(_("Selected Compound does not belong to the requested Destination."))
        if self.get("requested_developer") and compound.developer != self.requested_developer:
            frappe.throw(_("Selected Compound does not belong to the requested Developer."))
        # The Compound master is the source of truth.  Fill its developer only
        # when the selection was otherwise valid; never guess a Compound.
        if compound.developer:
            self.requested_developer = compound.developer
        if (
            self.get("requested_unit_type")
            and (self.is_new() or self.has_value_changed("requested_project") or self.has_value_changed("requested_unit_type"))
        ):
            allowed_types = frappe.get_all(
                "Real Estate Compound Unit Type",
                filters={
                    "parent": project,
                    "parenttype": "Real Estate Project",
                    "is_available": 1,
                },
                pluck="unit_type",
            )
            if allowed_types and self.requested_unit_type not in allowed_types:
                frappe.throw(
                    _("Unit Type {0} is not available in Compound {1}.").format(
                        self.requested_unit_type, project
                    )
                )

    def _validate_closed_won_evidence(self):
        if self.workflow_status != "Closed Won":
            return
        if not str(self.get("contract_reference") or "").strip():
            frappe.throw(_("Closed Won requires a contract reference."), frappe.ValidationError)
        signed = self.get("contract_signed_date")
        if not signed:
            frappe.throw(_("Closed Won requires the actual contract signed date."), frappe.ValidationError)
        try:
            signed_date = getdate(signed)
        except Exception:
            frappe.throw(_("Contract signed date must be a valid date."), frappe.ValidationError)
        if signed_date > getdate(nowdate()):
            frappe.throw(_("Contract signed date cannot be in the future."), frappe.ValidationError)
        if not self.get("contract_recorded_by") or not self.get("closed_won_at"):
            frappe.throw(_("Closed Won evidence must be recorded by the server."), frappe.ValidationError)

    def _validate_binding(self):
        party_type = frappe.db.get_value("CRM Lead", self.lead, "party_type")
        if not is_buyer_role(party_type):
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
                    self.category, self.record_type
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
            "Real Estate Unit", self.unit, ["inventory_type", "owner_lead", "listing_purpose"], as_dict=True
        )
        if not unit:
            frappe.throw(_("Real Estate Unit {0} was not found.").format(self.unit), frappe.DoesNotExistError)
        if unit.inventory_type != self.category:
            frappe.throw(
                _("Interest category {0} does not match Unit inventory type {1}.").format(
                    self.category, unit.inventory_type or _("blank")
                )
            )
        if self.category == "Resale":
            owner_role = (
                frappe.db.get_value("CRM Lead", unit.owner_lead, "party_type")
                if unit.owner_lead
                else None
            )
            if not is_owner_role(owner_role):
                frappe.throw(_("Resale Interests require a Unit owned by an Owner lead."))
        if self.category == "Primary" and unit.owner_lead:
            frappe.throw(_("Primary Interests cannot use an Owner-owned Unit."))
        if self.is_new() or self.has_value_changed("unit") or self.has_value_changed("deal_type"):
            _validate_unit_listing_for_deal(self.get("deal_type"), unit.get("listing_purpose"), self.unit)

    def on_trash(self):
        if not getattr(self.flags, "approved_interest_deletion", False):
            frappe.throw(
                _("Lead Interests cannot be deleted directly. Use the manager approval workflow."),
                frappe.PermissionError,
            )


def _validate_unit_listing_for_deal(deal, listing_purpose, unit_name):
    listing = listing_purpose or "For Sale"
    if deal == DEAL_RENT and listing not in ("For Rent", "For Sale or Rent"):
        frappe.throw(
            _("Unit {0} is not listed for rent, so it cannot be linked to a Rent Interest.").format(unit_name),
            frappe.ValidationError,
        )
    if deal != DEAL_RENT and listing not in ("For Sale", "For Sale or Rent"):
        frappe.throw(
            _("Unit {0} is listed for rent only, so it cannot be linked to a Buy Interest.").format(unit_name),
            frappe.ValidationError,
        )


def _site_currency():
    try:
        if frappe.db.exists("DocType", "FCRM Settings"):
            currency = frappe.db.get_single_value("FCRM Settings", "currency")
            if currency:
                return currency
    except Exception:
        pass
    try:
        if frappe.db.exists("DocType", "Global Defaults"):
            currency = frappe.db.get_single_value("Global Defaults", "default_currency")
            if currency:
                return currency
    except Exception:
        pass
    try:
        return frappe.defaults.get_global_default("currency")
    except Exception:
        return None
