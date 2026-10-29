"""
Real Estate CRM Customs — Buyer Lead Action Web API (v2.1)
==========================================================
Gated workflow: Fresh Lead → Call/WhatsApp → Call Log → Interest → Next Action → Meeting/Showing → Result → Loop
"""
import html
import json
import urllib.parse
from difflib import SequenceMatcher

import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime, time_diff_in_hours, add_to_date

from real_estate_crm_customs.interest_workflow import (
    action_interest_names as _standalone_action_interest_names,
    append_action_scopes as _append_action_scopes,
    assert_interests_available as _assert_standalone_interests_available,
    create_interest as _create_standalone_interest,
    get_interest as _get_standalone_interest,
    get_interests as _get_standalone_interests,
    is_available as _standalone_interests_available,
    mirror_interest_to_legacy as _mirror_standalone_interest,
    open_action_for_interest as _open_standalone_action_for_interest,
    reconcile_lead_rollup as _reconcile_standalone_rollup,
    resolve_interest_names as _resolve_standalone_interest_names,
    transition_interest as _transition_standalone_interest,
    update_action_scope_results as _update_standalone_scope_results,
)
from real_estate_crm_customs.party_roles import (
    BUYER_ROLE,
    OWNER_ROLE,
    is_owner_role as _is_owner_role,
    normalize_party_role as _normalize_party_role,
)
from real_estate_crm_customs.requirement_policy import (
    RequirementError, money as _requirement_money, delivery_date as _requirement_date,
    inventory_categories as _inventory_categories, is_property_requirement,
    listing_purposes_for_deal, requirement_snapshot, requirement_amount,
)
from real_estate_crm_customs.workflow_context import (
    interest_creation_context, creation_context_active, require_interest_creation_context,
)
from real_estate_crm_customs.interest_workflow import (
    begin_interest_closing, finalize_interest_contract, abandon_interest_closing,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LEAD_STATUS_NEW = "New"
OWNER_UNAVAILABLE_OUTCOME = "Owner/Unit Unavailable"
LEGACY_OWNER_UNAVAILABLE_OUTCOME = "Seller/Unit Unavailable"
LEAD_STATUS_FRESH = "Fresh Lead"
LEAD_STATUS_REQUESTED = "Requested"
LEAD_STATUS_OFFER_SENT = "Offer Sent"
LEAD_STATUS_NEGOTIATING = "Negotiating"
LEAD_STATUS_CLOSING = "Closing"
LEAD_STATUS_CLOSED_WON = "Closed Won"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_lead_doc(lead):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)
    doc = frappe.get_doc("CRM Lead", lead)
    if frappe.session.user != "Administrator" and not frappe.has_permission(
        "CRM Lead",
        ptype="read",
        doc=doc,
    ):
        frappe.throw(
            _("You do not have permission to read Lead {0}.").format(lead),
            frappe.PermissionError,
        )
    return doc


def _to_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _enforce_canonical_party_role(doc):
    raw_party_role = doc.get("party_type")
    role = _normalize_party_role(raw_party_role) if raw_party_role else BUYER_ROLE
    if not role:
        frappe.throw(_("Party Role must be Buyer or Owner."), frappe.ValidationError)
    # Old clients and imports may still send "Seller"; it is stored as Owner.
    doc.party_type = role


def _ensure_lead_status(status, status_type="Ongoing", color="orange", position=30):
    if not frappe.db.exists("DocType", "CRM Lead Status"):
        return
    if frappe.db.exists("CRM Lead Status", status):
        return
    frappe.get_doc({
        "doctype": "CRM Lead Status",
        "lead_status": status,
        "type": status_type,
        "color": color,
        "position": position,
    }).insert(ignore_permissions=True)


def _set_lead_status(doc, status, action="System Action"):
    """Apply a pipeline transition and persist an immutable audit record."""
    if not status or doc.get("status") == status:
        return False

    color_map = {
        LEAD_STATUS_NEW: "gray",
        LEAD_STATUS_FRESH: "blue",
        LEAD_STATUS_REQUESTED: "orange",
        "Pricing": "orange",
        "Documents Pending": "yellow",
        "Listed": "blue",
        LEAD_STATUS_OFFER_SENT: "blue",
        LEAD_STATUS_NEGOTIATING: "yellow",
        LEAD_STATUS_CLOSING: "orange",
        LEAD_STATUS_CLOSED_WON: "green",
    }
    _ensure_lead_status(status, status_type="Won" if status == LEAD_STATUS_CLOSED_WON else "Ongoing", color=color_map.get(status, "blue"))
    previous_status = doc.get("status")
    doc.status = status

    if frappe.db.exists("DocType", "Lead Status Transition"):
        frappe.get_doc({
            "doctype": "Lead Status Transition",
            "lead": doc.name,
            "from_status": previous_status,
            "to_status": status,
            "action": action or "System Action",
            "transitioned_on": now_datetime(),
            "actor": frappe.session.user,
        }).insert(ignore_permissions=True)
    return True


def _is_manager(user=None):
    roles = set(frappe.get_roles(user or frappe.session.user))
    return bool({"System Manager", "Sales Manager"} & roles)


def _is_system_manager(user=None):
    return "System Manager" in set(frappe.get_roles(user or frappe.session.user))


def _find_interest_row(doc, row_name):
    identifier = row_name
    if _standalone_interests_available() and frappe.db.exists("Lead Interest", row_name):
        interest = _get_standalone_interest(doc.name, row_name)
        identifier = interest.legacy_child_row
    for row in doc.get("interested_in_units") or []:
        if row.name == identifier:
            return row
    frappe.throw(_("Interest record {0} was not found.").format(row_name), frappe.DoesNotExistError)


def _validate_inventory_interest(category, units, allow_unavailable_units=None):
    if category not in ("Resale", "Primary"):
        return
    if not units:
        frappe.throw(_("At least one inventory unit is required for {0} interest.").format(category))

    allowed_unavailable = set(allow_unavailable_units or [])
    for unit_name in units:
        if not frappe.db.exists("Real Estate Unit", unit_name):
            frappe.throw(_("Real Estate Unit {0} was not found.").format(unit_name))
        frappe.db.sql("SELECT `name` FROM `tabReal Estate Unit` WHERE `name` = %s FOR UPDATE", (unit_name,))
        unit = frappe.db.get_value(
            "Real Estate Unit",
            unit_name,
            ["status", "owner_lead", "inventory_type", "listing_status", "next_verification_due"],
            as_dict=True,
        )
        from real_estate_crm_customs.owner_workflow import unit_is_buyer_eligible
        if not unit_is_buyer_eligible(unit):
            frappe.throw(_("Unit {0} is not an active, Owner-verified listing.").format(unit_name))
        if unit.status != "Available" and unit_name not in allowed_unavailable:
            frappe.throw(_("Unit {0} is not available.").format(unit_name))
        if unit.inventory_type and unit.inventory_type != category:
            frappe.throw(
                _("Unit {0} is {1} inventory, not {2}.").format(
                    unit_name,
                    unit.inventory_type,
                    category,
                )
            )
        if category == "Resale" and (not unit.owner_lead or not _is_owner_role(frappe.db.get_value("CRM Lead", unit.owner_lead, "party_type"))):
            frappe.throw(_("Resale interest must link an owner-listed resale unit."))
        if category == "Primary" and unit.owner_lead:
            frappe.throw(_("Primary interest must link open developer inventory, not an owner-listed resale unit."))


def _validate_active_destination(destination):
    if not destination:
        return
    is_active = frappe.db.get_value("Real Estate Destination", destination, "is_active")
    if is_active is None:
        frappe.throw(_("Destination {0} was not found.").format(destination), frappe.DoesNotExistError)
    if not is_active:
        frappe.throw(_("Destination {0} is inactive.").format(destination), frappe.ValidationError)


def _event_priority(subject, starts_on, status):
    """Return priority metadata: overdue, today, future, then completed."""
    from frappe.utils import getdate, nowdate

    if status in ("Cancelled", "Closed"):
        bucket, bucket_rank = "Completed", 3
    elif getdate(starts_on) < getdate(nowdate()):
        bucket, bucket_rank = "Overdue", 0
    elif getdate(starts_on) == getdate(nowdate()):
        bucket, bucket_rank = "Today", 1
    else:
        bucket, bucket_rank = "Upcoming", 2

    subject_lower = (subject or "").lower()
    action_rank = 0 if "showing" in subject_lower else 1 if "meeting" in subject_lower else 2 if "offer" in subject_lower else 3
    return bucket, bucket_rank, action_rank


IDEAL_STAGE_DAYS = {
    LEAD_STATUS_NEW: 0,
    LEAD_STATUS_FRESH: 0,
    LEAD_STATUS_REQUESTED: 1,
    LEAD_STATUS_OFFER_SENT: 2,
    LEAD_STATUS_NEGOTIATING: 4,
    LEAD_STATUS_CLOSING: 7,
    LEAD_STATUS_CLOSED_WON: 10,
}

REAL_ESTATE_FORM_DOCTYPES = {
    "Real Estate Unit",
    "Real Estate Project",
    "Property Developer",
    "Real Estate Destination",
    "Real Estate Unit Type",
    "Real Estate Unit Category",
    "Real Estate Amenity",
}


@frappe.whitelist()
def get_real_estate_doctype_permissions(doctype):
    if doctype not in REAL_ESTATE_FORM_DOCTYPES:
        frappe.throw(_("Unsupported real-estate DocType."), frappe.PermissionError)
    return {
        permission: bool(frappe.has_permission(doctype, ptype=permission))
        for permission in ("read", "create", "write", "delete")
    }


def guard_crm_lead_workflow(doc, method=None):
    """Prevent agent-side manual status changes and direct child-row deletion."""
    _enforce_canonical_party_role(doc)
    if doc.is_new():
        if doc.get("interested_in_units") and not creation_context_active():
            frappe.throw(_("Create Interests only through the workflow action form."), frappe.PermissionError)
        return

    previous = doc.get_doc_before_save()
    if not previous:
        return

    previous_role = _normalize_party_role(previous.get("party_type"))
    current_role = doc.get("party_type")

    if (
        previous_role == OWNER_ROLE
        and current_role != OWNER_ROLE
        and frappe.db.exists("DocType", "Real Estate Unit")
    ):
        owned_units = frappe.db.count("Real Estate Unit", {"owner_lead": doc.name})
        if owned_units:
            frappe.throw(
                _("This Lead owns {0} Unit(s). Transfer or remove ownership before changing Party Role.").format(
                    owned_units
                ),
                frappe.ValidationError,
            )

    if previous_role == "Buyer" and current_role != "Buyer":
        standalone_interests = 0
        if frappe.db.exists("DocType", "Lead Interest"):
            standalone_interests = frappe.db.count(
                "Lead Interest",
                {"lead": doc.name},
            )
        legacy_interests = 0
        if frappe.db.exists("DocType", "Lead Interested Unit"):
            legacy_interests = frappe.db.count(
                "Lead Interested Unit",
                {"parent": doc.name, "parenttype": "CRM Lead"},
            )
        interest_count = max(standalone_interests, legacy_interests)
        if interest_count:
            frappe.throw(
                _(
                    "This Lead has {0} Interest record(s). Resolve or remove them before changing Party Role."
                ).format(interest_count),
                frappe.ValidationError,
            )

        active_showings = 0
        if frappe.db.exists("DocType", "Unit Scheduled Showing"):
            active_showings = frappe.db.count(
                "Unit Scheduled Showing",
                {
                    "buyer_lead": doc.name,
                    "status": ["in", ["Scheduled", "Rescheduled"]],
                },
            )
        if active_showings:
            frappe.throw(
                _(
                    "This Lead has {0} active Showing record(s). Complete or cancel them before changing Party Role."
                ).format(active_showings),
                frappe.ValidationError,
            )

        active_actions = 0
        if frappe.db.exists("DocType", "Lead Action Execution"):
            active_actions = frappe.db.count(
                "Lead Action Execution",
                {
                    "lead": doc.name,
                    "workflow_status": ["in", ["Planned", "Due", "In Progress"]],
                },
            )
        if active_actions:
            frappe.throw(
                _(
                    "This Lead has {0} active workflow action(s). Complete or cancel them before changing Party Role."
                ).format(active_actions),
                frappe.ValidationError,
            )

    if previous.get("status") != doc.get("status"):
        allowed = getattr(doc.flags, "real_estate_status_transition", False)
        if not allowed and not _is_system_manager():
            frappe.throw(_("Lead Status is system-managed. Use the workflow action buttons."), frappe.PermissionError)

    previous_rows = {row.name for row in previous.get("interested_in_units") or [] if row.name}
    current_rows = {row.name for row in doc.get("interested_in_units") or [] if row.name}
    added_rows = [row for row in (doc.get("interested_in_units") or []) if not row.name or row.name not in previous_rows]
    if added_rows and not creation_context_active():
        frappe.throw(_("Create Interests only through the workflow action form."), frappe.PermissionError)
    removed_rows = previous_rows - current_rows
    approved = set(getattr(doc.flags, "approved_interest_deletions", []) or [])
    if removed_rows and not (_is_manager() and removed_rows <= approved):
        frappe.throw(_("Interest records cannot be deleted directly. Submit a deletion request for Sales Manager approval."), frappe.PermissionError)


def _save_workflow_doc(doc):
    doc.flags.real_estate_status_transition = True
    doc.save(ignore_permissions=True)


def _save_approved_interest_deletion(doc, row_names):
    doc.flags.approved_interest_deletions = list(row_names)
    doc.save(ignore_permissions=True)


def _add_lead_comment(lead_doc, text):
    try:
        lead_doc.add_comment("Comment", text=text)
    except Exception:
        pass


def _assert_write_permission(doc):
    if frappe.session.user == "Administrator":
        return
    if not frappe.has_permission(doc.doctype, ptype="write", doc=doc):
        frappe.throw(
            _("You do not have permission to update {0} {1}.").format(doc.doctype, doc.name),
            frappe.PermissionError,
        )


def _validate_buyer_lead(lead_doc):
    _assert_write_permission(lead_doc)
    if lead_doc.get("party_type") != "Buyer":
        frappe.throw(_("Only Buyer leads can use buyer interest actions."), frappe.ValidationError)


def _lead_contact_number(lead_doc):
    """Return full international phone number.
    Phone fieldtype stores the complete number with country code (e.g. +201070009839)."""
    return lead_doc.get("whatsapp_number") or lead_doc.get("mobile_no") or None


def _resolve_assigned_agent_identity(lead_doc):
    user = lead_doc.get("lead_owner") or frappe.session.user
    user_doc = frappe.get_doc("User", user) if frappe.db.exists("User", user) else None
    return frappe._dict({
        "user": user,
        "full_name": user_doc.get("full_name") if user_doc else user,
        "email": (user_doc.get("real_estate_agent_outreach_email") or user_doc.get("email")) if user_doc else None,
        "whatsapp_number": (user_doc.get("real_estate_agent_whatsapp_number") or user_doc.get("mobile_no")) if user_doc else None,
    })


def _create_lead_event(lead, subject, starts_on, event_type="Private", meeting_type=None, notes=None):
    """Create an Event linked to a CRM Lead via event_participants."""
    starts = get_datetime(starts_on)
    ends = add_to_date(starts, hours=1)
    event = frappe.get_doc({
        "doctype": "Event",
        "subject": subject,
        "starts_on": starts,
        "ends_on": ends,
        "event_type": event_type,
        "status": "Open",
        "description": notes or "",
    })
    event.append("event_participants", {
        "reference_doctype": "CRM Lead",
        "reference_docname": lead,
    })
    event.insert(ignore_permissions=True)
    return event.name


# ---------------------------------------------------------------------------
# 1. Lead Age — Scheduled Job (hourly)
# ---------------------------------------------------------------------------
def update_all_lead_ages():
    """Scheduled job: updates lead_age field for all leads every hour."""
    if not frappe.db.exists("DocType", "CRM Lead") or not frappe.db.has_column(
        "CRM Lead",
        "lead_age",
    ):
        return
    leads = frappe.get_all("CRM Lead", fields=["name", "creation"], limit_page_length=0)
    now = now_datetime()
    for lead in leads:
        if not lead.creation:
            continue
        hours = time_diff_in_hours(now, get_datetime(lead.creation))
        days = int(hours // 24)
        remaining_hours = int(hours % 24)
        age_str = f"{days}d {remaining_hours}h" if days > 0 else f"{remaining_hours}h"
        frappe.db.set_value("CRM Lead", lead.name, "lead_age", age_str, update_modified=False)


# ---------------------------------------------------------------------------
# 2. WhatsApp with Subject Recording
# ---------------------------------------------------------------------------
@frappe.whitelist()
def record_whatsapp_subject(lead, subject):
    """Record WhatsApp message subject, create Communication, return deep link."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    phone = _lead_contact_number(doc)
    if not phone:
        frappe.throw(_("Lead has no WhatsApp or mobile number set."))

    frappe.get_doc({
        "doctype": "Communication",
        "communication_type": "Communication",
        "communication_medium": "Other",
        "subject": subject or _("WhatsApp Message"),
        "content": _("WhatsApp message sent with subject: {0}").format(subject),
        "reference_doctype": "CRM Lead",
        "reference_name": lead,
        "sender": frappe.session.user,
        "sent_or_received": "Sent",
    }).insert(ignore_permissions=True)

    _add_lead_comment(doc, _("WhatsApp message recorded — Subject: {0}").format(subject))
    clean_phone = phone.replace(" ", "").replace("-", "").replace("+", "")
    whatsapp_url = f"https://wa.me/{clean_phone}"
    if subject:
        whatsapp_url += f"?text={urllib.parse.quote(subject)}"
    return {"whatsapp_url": whatsapp_url}


# ---------------------------------------------------------------------------
# 3. Call Log — Record Outcome (Answered / No Answer)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def record_call_outcome(lead, outcome, schedule_next_call=None):
    """Record contact result. No Answer updates history; Answered resets the streak."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)

    consecutive = _to_int(doc.get("no_answer_consecutive_count"))
    total = _to_int(doc.get("no_answer_total_count"))

    if outcome == "No Answer":
        consecutive += 1
        total += 1
        doc.no_answer_consecutive_count = consecutive
        doc.no_answer_total_count = total
        doc.last_call_outcome = "No Answer"
        doc.last_call_at = now_datetime()
        _add_lead_comment(doc, _("Call attempt #{0} — No Answer (total: {1})").format(consecutive, total))
        doc.save(ignore_permissions=True)

        event_name = None
        if schedule_next_call:
            event_name = _create_lead_event(
                lead=lead,
                subject=_("Follow-up Call — Attempt #{0}").format(consecutive + 1),
                starts_on=schedule_next_call,
                meeting_type="Call",
            )
        return {
            "status": doc.status,
            "no_answer_consecutive_count": consecutive,
            "no_answer_total_count": total,
            "last_call_outcome": "No Answer",
            "last_call_at": str(doc.last_call_at),
            "scheduled_event": event_name,
        }

    elif outcome == "Answered":
        doc.no_answer_consecutive_count = 0
        doc.last_call_outcome = "Answered"
        doc.last_call_at = now_datetime()
        _add_lead_comment(doc, _("Call answered — streak reset (total history: {0})").format(total))
        doc.save(ignore_permissions=True)
        return {
            "status": doc.status,
            "no_answer_consecutive_count": 0,
            "no_answer_total_count": doc.no_answer_total_count,
            "last_call_outcome": "Answered",
            "last_call_at": str(doc.last_call_at),
        }

    frappe.throw(_("Invalid outcome. Must be 'Answered' or 'No Answer'."))


# ---------------------------------------------------------------------------
# 4. Interest Determination
# ---------------------------------------------------------------------------
def _site_currency():
    if frappe.db.exists("DocType", "FCRM Settings"):
        currency = frappe.db.get_single_value("FCRM Settings", "currency")
        if currency:
            return currency
    if frappe.db.exists("DocType", "Global Defaults"):
        currency = frappe.db.get_single_value("Global Defaults", "default_currency")
        if currency:
            return currency
    return frappe.defaults.get_global_default("currency") or None


def _validate_requirement_relations(snapshot):
    destination = snapshot.get("requested_destination")
    _validate_active_destination(destination)
    developer = snapshot.get("requested_developer")
    if developer and not frappe.db.exists("Property Developer", developer):
        frappe.throw(_("Developer was not found."), frappe.DoesNotExistError)
    project = snapshot.get("requested_project")
    if project:
        compound = frappe.db.get_value("Real Estate Project", project, ["destination", "developer"], as_dict=True)
        if not compound:
            frappe.throw(_("Compound was not found."), frappe.DoesNotExistError)
        if destination and compound.destination != destination:
            frappe.throw(_("Compound must belong to the selected Destination."))
        if developer and compound.developer != developer:
            frappe.throw(_("Compound must belong to the selected Developer."))
        snapshot["requested_developer"] = compound.developer
    unit_category = snapshot.get("requested_unit_category")
    if unit_category and not frappe.db.exists("Real Estate Unit Category", unit_category):
        frappe.throw(_("Unit Category {0} was not found.").format(unit_category), frappe.DoesNotExistError)
    unit_type = snapshot.get("requested_unit_type")
    if unit_type:
        type_category = frappe.db.get_value("Real Estate Unit Type", unit_type, "unit_category")
        if unit_category and type_category and type_category != unit_category:
            frappe.throw(
                _("Unit Type {0} belongs to Unit Category {1}, not {2}.").format(unit_type, type_category, unit_category)
            )
        if type_category:
            snapshot["requested_unit_category"] = type_category
    currency = snapshot.get("budget_currency")
    if currency and not frappe.db.exists("Currency", currency):
        frappe.throw(_("Select a valid budget Currency."))
    return snapshot


@frappe.whitelist()
def get_requirement_compounds(destination, developer=None):
    if not frappe.has_permission("Real Estate Project", ptype="read"):
        frappe.throw(_("You cannot read Compounds."), frappe.PermissionError)
    if not destination:
        return {"compounds": [], "currency": _site_currency()}
    _validate_active_destination(destination)
    filters = {"destination": destination}
    if developer:
        filters["developer"] = developer
    return {
        "compounds": frappe.get_list("Real Estate Project", filters=filters,
            fields=["name", "project_name", "destination", "developer", "record_type", "parent_compound"],
            order_by="project_name asc", limit_page_length=0),
        "currency": _site_currency(),
    }


def _apply_interest_payload(doc, interest_data, require_inventory_requirements=False, apply_status=True):
    """Build independent row snapshots; never overwrite Lead defaults or older Interests."""
    interest_data = _parse_json_object(interest_data, _("Interest"))
    category = interest_data.get("interest_category")
    if category not in ("Resale", "Primary", "Brokerage Request", "International"):
        frappe.throw(_("Please select a valid interest category."))
    units = _parse_json_list(interest_data.get("units"))
    requested = bool(_to_int(interest_data.get("requested_unit")))
    if requested and units:
        frappe.throw(_("Choose inventory units or Requested Unit, not both."))
    property_request = is_property_requirement(category, interest_data.get("international_type"))
    try:
        snapshot = requirement_snapshot(interest_data, currency=_site_currency(),
            required_total=require_inventory_requirements and category in ("Resale", "Primary"))
    except RequirementError as exc:
        frappe.throw(str(exc), frappe.ValidationError)
    if property_request and not snapshot.get("requested_destination"):
        frappe.throw(_("Preferred Destination is mandatory for property requirements."))
    if property_request and not snapshot.get("budget_currency") and any(
        snapshot.get(key) is not None for key in (
            "requested_budget", "requested_down_payment_budget", "requested_monthly_installment_budget",
            "requested_rent_budget")
    ):
        frappe.throw(_("Set Budget Currency or configure FCRM Settings currency before saving affordability amounts."))
    _validate_requirement_relations(snapshot)
    if require_inventory_requirements and category in ("Resale", "Primary") and not snapshot.get("requested_unit_type"):
        frappe.throw(_("Preferred Unit Type is mandatory."))
    if category not in ("Resale", "Primary") and units:
        frappe.throw(_("Inventory selection requires a Resale or Primary property flow."))
    if category in ("Resale", "Primary") and not requested and not units:
        frappe.throw(_("Select matched inventory or choose Requested Unit."))
    if category == "Brokerage Request" and not interest_data.get("request_notes"):
        frappe.throw(_("Brokerage requirements are mandatory."))
    if category == "International" and (not interest_data.get("international_type") or not interest_data.get("international_country")):
        frappe.throw(_("International category and Country are mandatory."))
    if category == "International" and interest_data.get("international_type") == "Real Estate":
        country = frappe.db.get_value("Real Estate Destination", snapshot["requested_destination"], "country")
        if country and country != interest_data.get("international_country"):
            frappe.throw(_("Destination must belong to the selected Country."))
    existing_names, existing_units = [], set()
    if units:
        existing = frappe.get_all("Lead Interest", filters={"lead": doc.name, "record_type": "Inventory Unit",
            "unit": ["in", units], "is_active": 1}, fields=["name", "unit"])
        existing_names = [row.name for row in existing]
        existing_units = {row.unit for row in existing}
    values = []
    allowed_categories = _inventory_categories(snapshot["inventory_search_scope"], category, snapshot.get("deal_type"))
    deal_listings = listing_purposes_for_deal(snapshot.get("deal_type"))
    for unit_name in sorted(units):
        unit = frappe.db.get_value("Real Estate Unit", unit_name,
            ["inventory_type", "project", "destination", "developer", "listing_purpose"], as_dict=True)
        if not unit or unit.inventory_type not in allowed_categories:
            frappe.throw(_("Selected Unit is outside the chosen inventory search scope."))
        if (unit.get("listing_purpose") or "For Sale") not in deal_listings:
            frappe.throw(
                _("Unit {0} is listed {1}; it does not match a {2} requirement.").format(
                    unit_name, unit.get("listing_purpose") or "For Sale", snapshot.get("deal_type") or "Buy"
                )
            )
        _validate_inventory_interest(unit.inventory_type, [unit_name])
        # Read projections again after acquiring the Unit lock in validation.
        unit = frappe.db.get_value("Real Estate Unit", unit_name,
            ["inventory_type", "project", "destination", "developer"], as_dict=True)
        for key, unit_key, label in (("requested_destination", "destination", "Destination"),
                                    ("requested_project", "project", "Compound"),
                                    ("requested_developer", "developer", "Developer")):
            if snapshot.get(key) and unit.get(unit_key) != snapshot[key]:
                frappe.throw(_("Selected Unit does not match the required {0}.").format(label))
        if unit_name not in existing_units:
            values.append({**snapshot, "record_type": "Inventory Unit", "category": unit.inventory_type,
                "unit": unit_name, "workflow_status": "Matched"})
    if requested or category == "Brokerage Request":
        values.append({**snapshot, "record_type": "Request", "category": category,
            "workflow_status": "Requested", "request_status": "Open",
            "request_notes": interest_data.get("request_notes") or _("No suitable inventory match found.")})
    if category == "International":
        values.append({**snapshot, "record_type": "International", "category": category,
            "workflow_status": "Requested", "international_type": interest_data.get("international_type"),
            "international_country": interest_data.get("international_country"),
            "international_details": interest_data.get("international_details")})
    doc.interest_status = "Interested"
    doc.is_primary_buyer = int(any(v.get("category") == "Primary" for v in values) or bool(
        frappe.db.exists("Lead Interest", {"lead": doc.name, "category": "Primary", "is_active": 1})))
    return {"category": category, "requested_unit": requested,
        "interest_values": values, "existing_interest_names": existing_names}


def _promote_applied_interest_rows(lead_doc, applied_interest):
    """Create authoritative standalone Interests and return their names."""
    if not applied_interest or not _standalone_interests_available():
        return []
    promoted = list(dict.fromkeys(applied_interest.get("existing_interest_names") or []))
    for values in applied_interest.get("interest_values") or []:
        interest = _create_standalone_interest(
            lead_doc,
            values,
            reason=_("Created from Lead workflow"),
        )
        if interest and interest.name not in promoted:
            promoted.append(interest.name)
    _reconcile_standalone_rollup(lead_doc.name, _("Interest records added"))
    return promoted


@frappe.whitelist()
def record_interest_determination(lead, interested, is_primary_buyer=0, interest_data=None, qualification_only=0):
    """Record the mandatory Interested/Not Interested outcome and its payload."""
    require_interest_creation_context()
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    interested = int(interested or 0)

    if not interested:
        doc.interest_status = "Not Interested"
        _add_lead_comment(doc, _("Call qualification outcome: Not Interested."))
        doc.save(ignore_permissions=True)
        return {"status": doc.status, "interested": False, "interest_status": "Not Interested"}

    if int(qualification_only or 0):
        doc.interest_status = "Interested"
        _add_lead_comment(doc, _("Call qualification outcome: Interested."))
        doc.save(ignore_permissions=True)
        return {"status": doc.status, "interested": True, "interest_status": "Interested"}

    applied = _apply_interest_payload(doc, interest_data)
    _add_lead_comment(doc, _("Call qualification outcome: Interested — {0}.").format(applied["category"]))
    _save_workflow_doc(doc)
    interest_names = _promote_applied_interest_rows(doc, applied)
    doc.reload()
    return {
        "status": doc.status,
        "interested": True,
        "is_primary_buyer": doc.is_primary_buyer,
        "interest_status": "Interested",
        "interest_category": applied["category"],
        "requested_unit": applied["requested_unit"],
        "interest_rows": interest_names,
    }


# ---------------------------------------------------------------------------
# 5. Next Action Scheduling
# ---------------------------------------------------------------------------
@frappe.whitelist()
def schedule_next_action(lead, action_type, starts_on, subject=None, notes=None, target_unit=None):
    """Retired: all buyer mutations use the scoped action cycle."""
    frappe.throw(_("Use the current scoped workflow action form."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# 6. Meeting/Showing Result Logging
# ---------------------------------------------------------------------------
@frappe.whitelist()
def log_meeting_result(lead, event_name, result, result_note=None, reschedule_to=None, target_unit=None):
    """Retired: all buyer mutations use the scoped action cycle."""
    frappe.throw(_("Use the current scoped workflow action form."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# 7. Get Lead Upcoming Events (for result logging UI)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_lead_upcoming_events(lead):
    """Get pending events for a lead that may need result logging."""
    if not frappe.db.exists("CRM Lead", lead):
        return []
    _get_lead_doc(lead)
    participants = frappe.get_all("Event Participants", filters={
        "reference_doctype": "CRM Lead", "reference_docname": lead,
    }, fields=["parent"])
    if not participants:
        return []
    event_names = [p.parent for p in participants]
    return frappe.get_all("Event", filters={
        "name": ["in", event_names],
        "status": ["not in", ["Cancelled", "Closed"]],
    }, fields=["name", "subject", "starts_on", "ends_on", "event_type", "status"], order_by="starts_on asc")


# ---------------------------------------------------------------------------
# Existing Endpoints (preserved)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def create_resale_unit(
    owner_lead,
    project=None,
    unit_number=None,
    physical_unit_type=None,
    floor=None,
    bua=None,
    bedrooms=0,
    bathrooms=0,
    finishing_type=None,
    delivery_date=None,
    paid=None,
    over_price=None,
    over_is_gross=0,
    remaining=None,
    price=None,
    listing_purpose=None,
    rental_monthly_rate=None,
    rental_daily_rate=None,
    unit_data=None,
    pricing_method=None,
):
    """Compatibility endpoint for Owner Unit creation.

    Existing callers may retain the historical positional arguments. New callers
    can send ``unit_data`` containing all editable Unit intake fields. Both
    paths use the Owner workflow service so an incomplete listing never becomes
    Buyer-matchable on insert.
    """
    lead = _get_lead_doc(owner_lead)
    _assert_write_permission(lead)
    if not _is_owner_role(lead.get("party_type")):
        frappe.throw(_("Only Owner leads can list resale units."), frappe.ValidationError)
    _lock_lead_workflow(owner_lead)
    from real_estate_crm_customs.owner_workflow import _create_owner_unit, reconcile_owner_lead_rollup

    if unit_data:
        if isinstance(unit_data, str):
            try:
                payload = json.loads(unit_data)
            except (TypeError, ValueError, json.JSONDecodeError):
                frappe.throw(_("Unit data must be valid JSON."), frappe.ValidationError)
        else:
            payload = dict(unit_data)
    else:
        if not project or not unit_number or not physical_unit_type:
            frappe.throw(_("Compound, Unit Number, and Unit Type are required."), frappe.ValidationError)
        if not finishing_type or not delivery_date:
            frappe.throw(_("Finishing Type and Delivery Date are required."), frappe.ValidationError)
        if price not in (None, "") and all(value in (None, "") for value in (paid, over_price, remaining)):
            frappe.throw(
                _("Legacy Asking Price is ambiguous. Enter Paid, Over Price, and Remaining so Total Gross can be calculated."),
                frappe.ValidationError,
            )
        payload = {
            "project": project,
            "unit_number": unit_number,
            "physical_unit_type": physical_unit_type,
            "floor": floor,
            "bua": bua,
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "finishing_type": finishing_type,
            "delivery_date": delivery_date,
            "paid": paid,
            "over_price": over_price,
            "over_is_gross": _to_int(over_is_gross),
            "remaining": remaining,
            "listing_purpose": listing_purpose,
            "rental_monthly_rate": rental_monthly_rate,
            "rental_daily_rate": rental_daily_rate,
        }
    unit = _create_owner_unit(lead, payload, pricing_method)
    reconcile_owner_lead_rollup(owner_lead, "Owner Unit created")
    return unit.as_dict()


@frappe.whitelist()
def add_interest_request(lead, request_notes, request_status="Open"):
    require_interest_creation_context()
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    if not str(request_notes or "").strip():
        frappe.throw(_("Brokerage requirements are mandatory."))
    doc.interest_status = "Interested"
    if doc.status in (LEAD_STATUS_NEW, LEAD_STATUS_FRESH):
        doc.previous_status = doc.status
        _set_lead_status(doc, LEAD_STATUS_REQUESTED, _("Brokerage request added"))
    _save_workflow_doc(doc)
    promoted = _promote_applied_interest_rows(
        doc,
        {
            "category": "Brokerage Request",
            "requested_unit": True,
            "interest_values": [
                {
                    "record_type": "Request",
                    "category": "Brokerage Request",
                    "workflow_status": "Requested",
                    "request_notes": request_notes,
                    "request_status": "Open",
                }
            ],
        },
    )
    doc.reload()
    result = doc.as_dict()
    result["standalone_interests"] = promoted
    return result


@frappe.whitelist()
def link_interested_units(lead, units, interest_category="Resale"):
    require_interest_creation_context()
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    if isinstance(units, str):
        units = json.loads(units)
    units = list(dict.fromkeys(filter(None, units or [])))
    applied = _apply_interest_payload(
        doc,
        {"interest_category": interest_category, "units": units},
        apply_status=False,
    )
    _save_workflow_doc(doc)
    promoted = _promote_applied_interest_rows(doc, applied)
    if promoted:
        doc.reload()
    result = doc.as_dict()
    result["standalone_interests"] = promoted
    return result


@frappe.whitelist()
def assign_property_unit_to_seller(lead, unit):
    """Deprecated name kept for clients cached before the Owner rename."""
    return assign_property_unit_to_owner(lead, unit)


@frappe.whitelist()
def release_rented_unit(unit, note=None):
    """Return a leased Unit to the market after its lease ends (manager action).

    The signed rental Interest stays Closed Won as history; only the Unit's
    availability and reservation links are cleared.
    """
    if not _is_manager():
        frappe.throw(_("Only a Sales Manager or System Manager can return a rented Unit to the market."), frappe.PermissionError)
    if not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit), frappe.DoesNotExistError)
    frappe.db.sql("SELECT `name` FROM `tabReal Estate Unit` WHERE `name` = %s FOR UPDATE", (unit,))
    unit_doc = frappe.get_doc("Real Estate Unit", unit)
    if unit_doc.status != "Rented":
        frappe.throw(_("Only a Rented Unit can be returned to the market."), frappe.ValidationError)
    from real_estate_crm_customs.workflow_context import unit_mutation_context
    from frappe.utils import nowdate
    previous_interest = unit_doc.get("sold_interest")
    with unit_mutation_context("lease_release"):
        unit_doc.status = "Available"
        unit_doc.closing_interest = None
        unit_doc.sold_interest = None
        # Do not silently relist a Unit when its lease ends. The Owner must
        # validate availability before Buyer Smart Match sees it again.
        if unit_doc.get("owner_lead"):
            unit_doc.listing_status = "Verification Due"
            unit_doc.listing_status_reason = _("Lease ended; Owner availability confirmation is required before relisting.")
            unit_doc.next_verification_due = nowdate()
        unit_doc.save(ignore_permissions=True)
    unit_doc.add_comment(
        "Info",
        _("Lease ended; Unit returned to the market by {0}. Previous rental Interest: {1}. {2}").format(
            frappe.session.user, previous_interest or "-", (note or "").strip()
        ).strip(),
    )
    if unit_doc.get("owner_lead"):
        from real_estate_crm_customs.owner_workflow import _ensure_verification_action, reconcile_owner_lead_rollup

        _ensure_verification_action(unit_doc)
        reconcile_owner_lead_rollup(unit_doc.owner_lead, "Lease release requires Owner verification")
    return frappe.get_doc("Real Estate Unit", unit).as_dict()


@frappe.whitelist()
def assign_property_unit_to_owner(lead, unit):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)
    if not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit), frappe.DoesNotExistError)
    lead_doc = frappe.get_doc("CRM Lead", lead)
    _assert_write_permission(lead_doc)
    if not _is_owner_role(lead_doc.get("party_type")):
        frappe.throw(_("Only Owner leads can be assigned property units."), frappe.ValidationError)
    unit_doc = frappe.get_doc("Real Estate Unit", unit)
    if unit_doc.get("status") != "Available":
        frappe.throw(_("Only Available units can be assigned to an Owner lead."), frappe.ValidationError)
    if unit_doc.get("owner_lead") and unit_doc.get("owner_lead") != lead:
        frappe.throw(_("Unit {0} is already assigned to Owner lead {1}.").format(unit, unit_doc.get("owner_lead")), frappe.ValidationError)
    if unit_doc.get("inventory_type") != "Resale":
        active_interest = frappe.db.exists(
            "Lead Interest",
            {"unit": unit, "is_active": 1},
        ) if _standalone_interests_available() else None
        if active_interest:
            frappe.throw(
                _("Unit {0} has an active Buyer Interest and cannot be converted to Resale inventory.").format(unit),
                frappe.ValidationError,
            )
        unit_doc.inventory_type = "Resale"
    unit_doc.owner_lead = lead
    # Converted historical inventory must enter the Owner listing workflow
    # before it is eligible for Buyer selection.
    from real_estate_crm_customs.owner_workflow import _initial_listing_status, reconcile_owner_lead_rollup
    from real_estate_crm_customs.workflow_context import unit_mutation_context

    if not unit_doc.get("pricing_method"):
        unit_doc.pricing_method = "Owner Price"
    if not unit_doc.get("listing_status"):
        unit_doc.listing_status = _initial_listing_status(unit_doc)
        unit_doc.listing_status_reason = _("Assigned to Owner; complete the guided listing workflow.")
    with unit_mutation_context("owner_assignment"):
        unit_doc.save()
    reconcile_owner_lead_rollup(lead, "Unit assigned to Owner")
    return unit_doc.as_dict()


@frappe.whitelist()
def get_lead_linked_units(lead):
    lead_doc = _get_lead_doc(lead)
    use_standalone = _standalone_interests_available()
    interest_table_rows = (
        _get_standalone_interests(lead, include_closed=True)
        if use_standalone
        else (lead_doc.get("interested_in_units") or [])
    )
    interested_rows = [row for row in interest_table_rows if row.unit]
    non_unit_rows = [row for row in interest_table_rows if not row.unit]
    interested_units = [row.unit for row in interested_rows]
    interest_by_unit = {}
    for interest_row in interested_rows:
        interest_by_unit.setdefault(interest_row.unit, interest_row)
    names = set(interested_units)
    owner_rows = frappe.get_all("Real Estate Unit", filters={"owner_lead": lead}, pluck="name")
    names.update(owner_rows)
    rows = []
    if names:
        rows = frappe.get_all("Real Estate Unit", filters={"name": ["in", list(names)]}, fields=[
            "name", "unit_number", "sku", "project", "destination", "developer", "inventory_type",
            "physical_unit_type", "unit_type", "floor", "bua", "bedrooms", "bathrooms",
            "finishing_type", "delivery_status", "status", "total_gross", "price", "owner_lead", "modified",
        ], order_by="modified desc")
    interested_set = set(interested_units)
    for row in rows:
        row.unit_type = row.get("physical_unit_type") or row.get("unit_type")
        row.effective_price, row.price_source = _effective_unit_price(row)
        interest_row = interest_by_unit.get(row.name)
        row.interest_record_type = "Inventory Unit"
        row.interest_row_name = interest_row.name if interest_row else None
        row.interest_category = (
            interest_row.get("category") if use_standalone else interest_row.get("interest_category")
        ) if interest_row else None
        workflow_status = interest_row.get("workflow_status") if interest_row and use_standalone else None
        row.unit_interest_status = (
            "Lost Interest" if workflow_status in {"Rejected", "Superseded", "Cancelled"} else "Active"
        ) if interest_row and use_standalone else (
            interest_row.get("unit_interest_status") if interest_row else None
        )
        row.offer_sent = int(workflow_status in {
            "Offer Sent", "Offer Viewed", "Offer Accepted", "Negotiating",
            "Showing Scheduled", "Shown - Interested", "Shown - Considering",
        }) if interest_row and use_standalone else (
            interest_row.get("offer_sent") if interest_row else 0
        )
        row.offer_sent_at = interest_row.get("offer_sent_at") if interest_row else None
        row.deletion_request_status = interest_row.get("deletion_request_status") if interest_row else None
        row.deletion_request = interest_row.get("deletion_request") if interest_row else None
        if row.name in interested_set and row.owner_lead == lead:
            row.relationship = _("Interested and Owned")
        elif row.owner_lead == lead:
            row.relationship = _("Owner Unit")
        else:
            row.relationship = _("Interested Unit")
        row.proposal_status = (
            {
                "Matched": "Pending",
                "Offer Sent": "Sent",
                "Offer Viewed": "Viewed",
                "Offer Accepted": "Offer Accepted",
                "Negotiating": "Offer Accepted",
                "Showing Scheduled": "Offer Accepted",
                "Shown - Interested": "Offer Accepted",
                "Shown - Considering": "Offer Accepted",
                "Rejected": "Rejected",
                "Superseded": "Rejected",
            }.get(workflow_status)
            if interest_row and use_standalone
            else (interest_row.get("proposal_status") if interest_row else None)
        )

    for index, interest_row in enumerate(non_unit_rows, start=1):
        record_type = (
            interest_row.get("record_type") if use_standalone else interest_row.get("interest_record_type")
        ) or "Request"
        category = (
            interest_row.get("category") if use_standalone else interest_row.get("interest_category")
        ) or (
            "International" if record_type == "International" else "Brokerage Request"
        )
        rows.append(frappe._dict({
            "name": interest_row.name or f"interest-{index}",
            "interest_row_name": interest_row.name,
            "sku": category,
            "interest_record_type": record_type,
            "interest_category": category,
            "request_status": interest_row.get("request_status") or "Open",
            "request_notes": interest_row.get("request_notes"),
            "requested_destination": interest_row.get("requested_destination"),
            "requested_area": interest_row.get("requested_area"),
            "requested_unit_type": interest_row.get("requested_unit_type"),
            "requested_budget": interest_requirement_amount(row, "requested_budget"),
            "requested_project": interest_row.get("requested_project"),
            "requested_developer": interest_row.get("requested_developer"),
            "requested_finishing_type": interest_row.get("requested_finishing_type"),
            "requested_delivery_time": interest_row.get("requested_delivery_time"),
            "international_type": interest_row.get("international_type"),
            "international_country": interest_row.get("international_country"),
            "international_details": interest_row.get("international_details"),
            "outsource_company": interest_row.get("outsource_company"),
            "outsource_broker_name": interest_row.get("outsource_broker_name"),
            "outsource_broker_number": interest_row.get("outsource_broker_number"),
            "outsource_unit_details": interest_row.get("outsource_unit_details"),
            "unit_interest_status": (
                "Lost Interest"
                if use_standalone and interest_row.get("workflow_status") in {"Rejected", "Superseded", "Fulfilled", "Cancelled"}
                else "Active"
                if use_standalone
                else interest_row.get("unit_interest_status")
            ),
            "deletion_request_status": interest_row.get("deletion_request_status"),
            "deletion_request": interest_row.get("deletion_request"),
            "relationship": _("Interest Request"),
            "proposal_status": interest_row.get("proposal_status") if not use_standalone else None,
            "owner_lead": None,
            "modified": interest_row.modified,
        }))
    return rows


def _effective_unit_price(unit, deal_type=None):
    """Comparable price for matching: monthly rent for Rent deals, Total Gross for Buy deals."""
    if deal_type == "Rent":
        monthly = unit.get("rental_monthly_rate")
        if monthly not in (None, "") and float(monthly or 0) > 0:
            return float(monthly), "Monthly Rent"
        daily = unit.get("rental_daily_rate")
        if daily not in (None, "") and float(daily or 0) > 0:
            # Daily-only rentals are compared on an estimated 30-day month.
            return float(daily) * 30, "Daily Rent x 30"
        return None, None
    total_gross = unit.get("total_gross")
    if total_gross not in (None, "") and float(total_gross or 0) > 0:
        return float(total_gross), "Total Gross"
    legacy_price = unit.get("price")
    if legacy_price not in (None, "") and float(legacy_price or 0) > 0:
        return float(legacy_price), "Legacy Price"
    return None, None


def _resolve_destination_alias(destination=None, location=None):
    if not location:
        return destination
    destination_from_location = frappe.db.get_value(
        "Real Estate Destination",
        {"destination_name": location},
        "name",
    )
    if destination and destination_from_location and destination != destination_from_location:
        frappe.throw(_("Destination conflicts with the legacy Location filter."), frappe.ValidationError)
    resolved = destination or destination_from_location
    _validate_active_destination(resolved)
    return resolved


@frappe.whitelist()
def get_available_units_for_selection(
    lead=None,
    interest_category="Resale",
    search=None,
    project=None,
    developer=None,
    destination=None,
    location=None,
    physical_unit_type=None,
    unit_type=None,
    finishing_type=None,
    min_price=None,
    max_price=None,
    include_unit=None,
    page_length=100,
    inventory_search_scope=None,
    unit_category=None,
    deal_type=None,
):
    """Return category-valid inventory using canonical Compound, Destination, and Total Gross fields.

    ``deal_type`` Rent returns only Units listed for rent and prices them by monthly rent.
    """
    if interest_category not in ("Resale", "Primary"):
        frappe.throw(_("Inventory selection is available only for Resale or Primary interests."))
    if lead:
        lead_doc = _get_lead_doc(lead)
        _validate_buyer_lead(lead_doc)
    else:
        lead_doc = None

    destination = _resolve_destination_alias(destination, location)
    physical_unit_type = physical_unit_type or unit_type
    from real_estate_crm_customs.requirement_policy import deal_type as _normalize_deal_type
    try:
        deal_type = _normalize_deal_type(deal_type)
        categories = _inventory_categories(inventory_search_scope, interest_category, deal_type)
    except RequirementError as exc:
        frappe.throw(str(exc))
    filters = {
        "status": "Available",
        "inventory_type": ["in", categories],
        "listing_purpose": ["in", listing_purposes_for_deal(deal_type)],
    }
    if unit_category:
        # Filter through the Category's Unit Types so a stale Unit projection cannot hide a Unit.
        category_types = frappe.get_all(
            "Real Estate Unit Type", filters={"unit_category": unit_category}, pluck="name", limit_page_length=0
        )
        if physical_unit_type and physical_unit_type not in category_types:
            return []
        if not category_types:
            return []
        if not physical_unit_type:
            filters["physical_unit_type"] = ["in", category_types]
    if project:
        # A Compound also matches the Units inside its Phases; a Phase matches only itself.
        filters["project"] = ["in", _compound_family(project)]
    if developer:
        filters["developer"] = developer
    if physical_unit_type:
        filters["physical_unit_type"] = physical_unit_type
    if finishing_type:
        filters["finishing_type"] = finishing_type
    if destination:
        filters["destination"] = destination

    try:
        minimum = float(min_price) if min_price not in (None, "") else None
        maximum = float(max_price) if max_price not in (None, "") else None
        if minimum is not None and maximum is not None and minimum > maximum:
            frappe.throw(_("Minimum price cannot be greater than maximum price."))
    except (TypeError, ValueError):
        frappe.throw(_("Price filters must be valid numbers."))

    or_filters = None
    if search and search.strip():
        pattern = "%{0}%".format(search.strip())
        or_filters = [
            ["Real Estate Unit", "name", "like", pattern],
            ["Real Estate Unit", "unit_number", "like", pattern],
            ["Real Estate Unit", "sku", "like", pattern],
            ["Real Estate Unit", "project", "like", pattern],
            ["Real Estate Unit", "developer", "like", pattern],
            ["Real Estate Unit", "physical_unit_type", "like", pattern],
        ]

    fields = [
        "name",
        "unit_number",
        "sku",
        "project",
        "master_compound",
        "destination",
        "developer",
        "inventory_type",
        "unit_category",
        "physical_unit_type",
        "unit_type",
        "floor",
        "bua",
        "bedrooms",
        "bathrooms",
        "finishing_type",
        "delivery_status",
        "delivery_date",
        "status",
        "total_gross",
        "price",
        "listing_purpose",
        "rental_monthly_rate",
        "rental_daily_rate",
        "owner_lead",
        "listing_status",
        "next_verification_due",
        "modified",
    ]
    units = frappe.get_all(
        "Real Estate Unit",
        filters=filters,
        or_filters=or_filters,
        fields=fields,
        order_by="modified desc",
        limit_page_length=500,
    )

    allowed_existing = include_unit if include_unit and frappe.db.exists("Real Estate Unit", include_unit) else None
    has_active_filters = any(
        (
            search,
            project,
            developer,
            destination,
            location,
            unit_category,
            physical_unit_type,
            finishing_type,
            min_price,
            max_price,
        )
    )
    if allowed_existing and not has_active_filters and not any(unit.name == allowed_existing for unit in units):
        existing_unit = frappe.db.get_value("Real Estate Unit", allowed_existing, fields, as_dict=True)
        if (
            existing_unit
            and existing_unit.get("inventory_type") in categories
            and (existing_unit.get("listing_purpose") or "For Sale") in listing_purposes_for_deal(deal_type)
        ):
            units.insert(0, existing_unit)

    already_linked = set()
    if lead_doc:
        if not _standalone_interests_available():
            already_linked.update(row.unit for row in (lead_doc.get("interested_in_units") or [])
                if row.unit and row.unit != allowed_existing and row.get("unit_interest_status") != "Lost Interest")
        if _standalone_interests_available():
            already_linked.update(
                row.unit
                for row in _get_standalone_interests(lead_doc.name, include_closed=False)
                if row.unit and row.unit != allowed_existing
            )
    # ``include_unit`` is a user-experience convenience, never a bypass for an
    # incomplete or stale Owner listing. Blank lifecycle state is retained only
    # for historical Units created before this workflow was introduced.
    from real_estate_crm_customs.owner_workflow import unit_is_buyer_eligible
    units = [unit for unit in units if unit.name not in already_linked and unit_is_buyer_eligible(unit)]

    project_names = list({unit.project for unit in units if unit.project})
    projects = {}
    if project_names:
        projects = {
            row.name: row
            for row in frappe.get_all(
                "Real Estate Project",
                filters={"name": ["in", project_names]},
                fields=["name", "project_name", "destination", "location", "status"],
                limit_page_length=500,
            )
        }

    filtered_units = []
    for unit in units:
        project_row = projects.get(unit.project) or {}
        unit.destination = unit.destination or project_row.get("destination")
        unit.destination_label = unit.destination or project_row.get("location")
        unit.location = unit.destination_label
        unit.project_label = project_row.get("project_name") or unit.project
        unit.project_status = project_row.get("status")
        unit.inventory_category = unit.inventory_type
        unit.budget_currency = _site_currency()
        unit.financing_status = "Unknown"
        unit.unit_type = unit.physical_unit_type or unit.unit_type
        unit.deal_type = deal_type
        unit.effective_price, unit.price_source = _effective_unit_price(unit, deal_type)
        if minimum is not None and (unit.effective_price is None or unit.effective_price < minimum):
            continue
        if maximum is not None and (unit.effective_price is None or unit.effective_price > maximum):
            continue
        filtered_units.append(unit)

    result_limit = max(10, min(_to_int(page_length) or 100, 500))
    return filtered_units[:result_limit]


@frappe.whitelist()
def get_property_match_filter_options(interest_category="Resale", inventory_search_scope=None, deal_type=None):
    """Return inventory-backed Unit Category, Unit Type, and finishing options."""
    if interest_category not in ("Resale", "Primary"):
        frappe.throw(_("Inventory filters are available only for Resale or Primary interests."))
    from real_estate_crm_customs.requirement_policy import deal_type as _normalize_deal_type
    try:
        deal_type = _normalize_deal_type(deal_type)
        categories = _inventory_categories(inventory_search_scope, interest_category, deal_type)
    except RequirementError as exc:
        frappe.throw(str(exc))
    unit_filters = {
        "status": "Available",
        "inventory_type": ["in", categories],
        "listing_purpose": ["in", listing_purposes_for_deal(deal_type)],
    }
    destinations = frappe.get_all(
        "Real Estate Destination",
        filters={"is_active": 1},
        fields=["name", "destination_name"],
        order_by="destination_name asc",
        limit_page_length=500,
    )
    candidate_units = frappe.get_all(
        "Real Estate Unit",
        filters=unit_filters,
        fields=["name", "physical_unit_type", "finishing_type", "owner_lead", "listing_status", "next_verification_due"],
        limit_page_length=500,
    )
    from real_estate_crm_customs.owner_workflow import unit_is_buyer_eligible
    candidate_units = [unit for unit in candidate_units if unit_is_buyer_eligible(unit)]
    unit_type_names = sorted(set(filter(None, (unit.physical_unit_type for unit in candidate_units))))
    finishing_types = [unit.finishing_type for unit in candidate_units]
    # Category of every Unit Type in stock, so the client can narrow Unit Type by Category.
    unit_type_categories = {}
    if unit_type_names:
        unit_type_categories = {
            row.name: row.unit_category
            for row in frappe.get_all(
                "Real Estate Unit Type",
                filters={"name": ["in", unit_type_names]},
                fields=["name", "unit_category"],
                limit_page_length=0,
            )
        }
    unit_categories = sorted(set(filter(None, unit_type_categories.values())))
    return {
        "currency": _site_currency(),
        "deal_type": deal_type,
        "destinations": [dict(row) for row in destinations],
        "locations": [row.destination_name for row in destinations],
        "unit_categories": unit_categories,
        "unit_types": unit_type_names,
        "unit_type_categories": unit_type_categories,
        "finishing_types": sorted(set(filter(None, finishing_types))),
    }


def _normalize_match_text(value):
    return " ".join(str(value or "").strip().lower().replace("-", " ").split())


def _text_match_ratio(expected, actual):
    expected_text = _normalize_match_text(expected)
    actual_text = _normalize_match_text(actual)
    if not expected_text:
        return None
    if not actual_text:
        return 0.0
    if expected_text == actual_text:
        return 1.0
    if expected_text in actual_text or actual_text in expected_text:
        return 0.85
    expected_tokens = set(expected_text.split())
    actual_tokens = set(actual_text.split())
    union = expected_tokens | actual_tokens
    token_ratio = len(expected_tokens & actual_tokens) / len(union) if union else 0
    sequence_ratio = SequenceMatcher(None, expected_text, actual_text).ratio()
    return max(token_ratio, sequence_ratio * 0.8)


def _lead_property_match_profile(lead_doc, interest_category=None, interest=None):
    """Use the exact Interest snapshot; never fill it from a different row or unit price."""
    scoped = _get_standalone_interest(lead_doc.name, interest) if interest else None
    mappings = {"destination": "requested_destination", "unit_type": "requested_unit_type",
        "unit_category": "requested_unit_category", "deal_type": "deal_type",
        "developer": "requested_developer", "project": "requested_project", "finishing_type": "requested_finishing_type",
        "budget": "requested_budget", "down_payment_budget": "requested_down_payment_budget",
        "monthly_installment_budget": "requested_monthly_installment_budget",
        "rent_budget": "requested_rent_budget", "delivery_date": "requested_delivery_date",
        "inventory_search_scope": "inventory_search_scope", "budget_currency": "budget_currency"}
    amount_fields = ("requested_budget", "requested_down_payment_budget", "requested_monthly_installment_budget",
        "requested_rent_budget")
    if scoped:
        profile = frappe._dict({key: requirement_amount(scoped, field) if field in amount_fields else scoped.get(field) for key, field in mappings.items()})
        profile.update({"source": "Scoped Lead Interest", "interest": scoped.name})
    else:
        profile = frappe._dict({"destination": lead_doc.get("preferred_destination"),
            "unit_type": lead_doc.get("preferred_unit_type"), "developer": lead_doc.get("preferred_developer"),
            "project": lead_doc.get("preferred_compound"), "finishing_type": lead_doc.get("preferred_finishing_type"),
            "budget": lead_doc.get("buyer_budget"), "source": "Lead Defaults"})
    profile.budget_currency = profile.get("budget_currency") or _site_currency()
    profile.deal_type = profile.get("deal_type") or "Buy"
    profile.inventory_search_scope = profile.get("inventory_search_scope") or interest_category
    profile.location = profile.get("destination")
    profile.budget_min = None
    profile.criteria_count = sum(profile.get(key) not in (None, "") for key in mappings)
    return profile


def _compound_family(project):
    """Compound -> itself plus its Phases; Phase -> only itself."""
    from real_estate_crm_customs.real_estate_crm_customs.doctype.real_estate_project.real_estate_project import (
        get_compound_family,
    )

    return get_compound_family(project) or [project]


def _unit_compound_for_profile(unit, expected_project):
    """Let a Unit inside a Phase count as matching its Parent Compound."""
    actual = unit.get("project")
    master = unit.get("master_compound")
    if expected_project and master and expected_project == master:
        return master
    return actual


def _apply_match_profile_overrides(
    profile,
    project=None,
    developer=None,
    destination=None,
    location=None,
    unit_type=None,
    min_price=None,
    max_price=None,
):
    destination = "" if destination == "" else _resolve_destination_alias(destination, location)
    overrides = {
        "project": project,
        "developer": developer,
        "destination": destination,
        "location": destination or location,
        "unit_type": unit_type,
        "budget_min": min_price,
        "budget": max_price,
    }
    changed = False
    for fieldname, value in overrides.items():
        if value is not None:
            profile[fieldname] = value
            changed = True
    if changed:
        profile.source = "Adjusted Smart Match"
    profile.criteria_count = sum(
        1
        for fieldname in (
            "destination",
            "unit_type",
            "developer",
            "project",
            "finishing_type",
            "budget_min",
            "budget",
        )
        if profile.get(fieldname) not in (None, "")
    )
    return profile


def _score_property_match(unit, profile):
    weighted_score = 0.0
    total_weight = 0.0
    reasons = []
    gaps = []

    expected_destination = profile.get("destination")
    if expected_destination:
        total_weight += 30
        if unit.get("destination") == expected_destination:
            weighted_score += 30
            reasons.append(_("Destination matches"))
        else:
            gaps.append(_("Destination differs"))
    elif profile.get("location"):
        ratio = _text_match_ratio(profile.get("location"), unit.get("location")) or 0
        total_weight += 30
        weighted_score += ratio * 30
        (reasons if ratio >= 0.8 else gaps).append(_("Legacy location matches") if ratio >= 0.8 else _("Legacy location differs"))

    comparisons = (
        ("Unit type", profile.get("unit_type"), unit.get("physical_unit_type") or unit.get("unit_type"), 22),
        ("Compound", profile.get("project"), _unit_compound_for_profile(unit, profile.get("project")), 16),
        ("Developer", profile.get("developer"), unit.get("developer"), 12),
        ("Finishing", profile.get("finishing_type"), unit.get("finishing_type"), 10),
    )
    for label, expected, actual, weight in comparisons:
        ratio = _text_match_ratio(expected, actual)
        if ratio is None:
            continue
        total_weight += weight
        weighted_score += ratio * weight
        if ratio >= 0.8:
            reasons.append(_("{0} matches").format(label))
        elif ratio >= 0.4:
            reasons.append(_("{0} is similar").format(label))
        else:
            gaps.append(_("{0} differs").format(label))

    minimum_budget = profile.get("budget_min")
    maximum_budget = profile.get("budget")
    if minimum_budget not in (None, "") or maximum_budget not in (None, ""):
        total_weight += 30
        try:
            minimum_value = float(minimum_budget) if minimum_budget not in (None, "") else None
            maximum_value = float(maximum_budget) if maximum_budget not in (None, "") else None
            price_value = float(unit.get("effective_price") or 0) if unit.get("price_comparison_available", True) else 0
        except (TypeError, ValueError):
            minimum_value = None
            maximum_value = None
            price_value = 0
        if price_value <= 0:
            budget_score = 0.0
            gaps.append(_("Price comparison unavailable"))
        elif minimum_value is not None and price_value < minimum_value:
            budget_score = max(0.4, price_value / minimum_value) if minimum_value > 0 else 1.0
            gaps.append(_("Below preferred price range"))
        elif maximum_value is None or price_value <= maximum_value:
            budget_score = 1.0
            reasons.append(_("Within monthly rent budget") if profile.get("deal_type") == "Rent" else _("Within preferred price range"))
        elif price_value <= maximum_value * 1.1:
            budget_score = 0.75
            reasons.append(_("Up to 10% above budget"))
        elif price_value <= maximum_value * 1.2:
            budget_score = 0.45
            gaps.append(_("Up to 20% above budget"))
        else:
            budget_score = 0.0
            gaps.append(_("More than 20% above budget"))
        weighted_score += budget_score * 30

    for fieldname, label in (("down_payment_budget", "Down payment"), ("monthly_installment_budget", "Monthly installment")):
        if profile.get(fieldname) not in (None, ""):
            total_weight += 12
            gaps.append(_("{0} comparison unavailable: financing unknown").format(label))
    if profile.get("delivery_date"):
        total_weight += 12
        actual_date = unit.get("delivery_date")
        if not actual_date:
            gaps.append(_("Delivery date comparison unavailable"))
        elif str(actual_date) <= profile.delivery_date:
            weighted_score += 12
            reasons.append(_("Delivery within requested deadline"))
        else:
            gaps.append(_("Delivery later than requested date"))
    score = round((weighted_score / total_weight) * 100) if total_weight else None
    if score is None:
        level = "Unscored"
    elif score >= 85:
        level = "Excellent"
    elif score >= 65:
        level = "Good"
    elif score >= 40:
        level = "Possible"
    else:
        level = "Alternative"
    return score, level, reasons[:4], gaps[:3]


@frappe.whitelist()
def get_smart_matched_units(lead, interest_category="Resale", interest=None, search=None, project=None,
    developer=None, destination=None, location=None, physical_unit_type=None, unit_type=None, finishing_type=None,
    min_price=None, max_price=None, include_unit=None, page_length=100, strict_filters=0,
    inventory_search_scope=None, down_payment_budget=None, monthly_installment_budget=None,
    delivery_date=None, budget_currency=None, unit_category=None, deal_type=None, rent_budget=None):
    """Rank mixed inventory against an independent requirement snapshot with truthful affordability.

    Rent requirements match only Units listed for rent and compare the monthly rent budget.
    """
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    strict = bool(_to_int(strict_filters))
    profile = _apply_match_profile_overrides(_lead_property_match_profile(lead_doc, interest_category, interest),
        project=project, developer=developer, destination=destination, location=location,
        unit_type=physical_unit_type or unit_type, min_price=min_price, max_price=max_price)
    from real_estate_crm_customs.requirement_policy import deal_type as _normalize_deal_type
    try:
        profile.deal_type = _normalize_deal_type(deal_type or profile.get("deal_type"))
        renting = profile.deal_type == "Rent"
        if renting:
            # A rental is compared on monthly rent; purchase budgets do not apply.
            profile.budget = max_price if max_price is not None else (
                rent_budget if rent_budget is not None else profile.get("rent_budget"))
            profile.rent_budget = profile.budget
            down_payment_budget = monthly_installment_budget = None
            profile.down_payment_budget = profile.monthly_installment_budget = None
        for key, value in (("budget", profile.get("budget")), ("budget_min", profile.get("budget_min")),
            ("down_payment_budget", down_payment_budget if down_payment_budget is not None else profile.get("down_payment_budget")),
            ("monthly_installment_budget", monthly_installment_budget if monthly_installment_budget is not None else profile.get("monthly_installment_budget"))):
            profile[key] = _requirement_money(value, key)
        profile.delivery_date = _requirement_date(delivery_date if delivery_date is not None else profile.get("delivery_date"))
        profile.inventory_search_scope = "Resale" if renting else (
            inventory_search_scope or profile.get("inventory_search_scope") or interest_category)
        _inventory_categories(profile.inventory_search_scope, interest_category, profile.deal_type)
    except RequirementError as exc:
        frappe.throw(str(exc))
    if unit_category is not None:
        profile.unit_category = unit_category or None
    if profile.budget is not None and profile.budget_min is not None and profile.budget_min > profile.budget:
        frappe.throw(_("Minimum budget exceeds maximum budget."))
    profile.budget_currency = budget_currency or profile.get("budget_currency") or _site_currency()
    if finishing_type is not None:
        profile.finishing_type = finishing_type or None
    relations = _validate_requirement_relations({"requested_destination": profile.get("destination"),
        "requested_project": profile.get("project"), "requested_developer": profile.get("developer"),
        "requested_unit_category": profile.get("unit_category"), "requested_unit_type": profile.get("unit_type"),
        "budget_currency": profile.budget_currency})
    profile.developer = relations.get("requested_developer")
    profile.unit_category = relations.get("requested_unit_category")
    profile.criteria_count = sum(profile.get(key) not in (None, "") for key in (
        "destination", "unit_category", "unit_type", "project", "developer", "finishing_type", "budget", "budget_min",
        "down_payment_budget", "monthly_installment_budget", "delivery_date"))
    units = get_available_units_for_selection(lead=lead, interest_category=interest_category,
        inventory_search_scope=profile.inventory_search_scope, search=search,
        unit_category=profile.get("unit_category"), deal_type=profile.deal_type,
        project=profile.get("project"), developer=profile.get("developer"), destination=profile.get("destination"),
        physical_unit_type=profile.get("unit_type") if strict else None,
        finishing_type=profile.get("finishing_type") if strict else None,
        min_price=profile.get("budget_min") if strict else None, max_price=profile.get("budget") if strict else None,
        include_unit=include_unit, page_length=500)
    result = []
    for unit in units:
        unit.price_comparison_available = bool(profile.budget_currency and unit.budget_currency == profile.budget_currency)
        deadline = profile.get("delivery_date")
        unit.affordability = {
            "deal_type": profile.deal_type,
            "total_budget": "Unknown" if not unit.price_comparison_available or unit.effective_price is None else (
                "Not specified" if profile.budget is None else "Within budget" if unit.effective_price <= profile.budget else "Over budget"),
            "down_payment": "Unknown financing" if profile.get("down_payment_budget") is not None else "Not specified",
            "monthly_installment": "Unknown financing" if profile.get("monthly_installment_budget") is not None else "Not specified",
            "delivery": "Not specified" if not deadline else "Unknown" if not unit.get("delivery_date") else (
                "Within deadline" if str(unit.delivery_date) <= deadline else "Later than requested"),
        }
        if strict and (profile.get("down_payment_budget") is not None or profile.get("monthly_installment_budget") is not None):
            continue  # Missing verified payment facts cannot satisfy an Exact finance constraint.
        if strict and profile.budget is not None and not unit.price_comparison_available:
            continue
        if strict and deadline and (not unit.get("delivery_date") or str(unit.delivery_date) > deadline):
            continue
        unit.match_score, unit.match_level, unit.match_reasons, unit.match_gaps = _score_property_match(unit, profile)
        result.append(unit)
    result.sort(key=lambda row: (-(row.match_score or 0), row.effective_price if row.effective_price is not None else float('inf'), row.name))
    return {"units": result[:max(10, min(_to_int(page_length) or 100, 500))],
        "criteria": dict(profile), "profile": dict(profile), "smart_match_active": bool(profile.get("criteria_count")),
        "match_mode": "Exact Filters" if strict else "Smart Match", "deal_type": profile.deal_type,
        "currency": _site_currency(), "financing_available": False}


# ---------------------------------------------------------------------------
# 9. Send Offer — Mark units as sent and prepare WhatsApp message
# ---------------------------------------------------------------------------
@frappe.whitelist()
def send_offer_to_lead(lead, unit_rows):
    """Retired: all buyer mutations use the scoped action cycle."""
    frappe.throw(_("Use the current scoped workflow action form."), frappe.PermissionError)



# ---------------------------------------------------------------------------
# 10. Rollback Offer Rejection — Roll back to previous status
# ---------------------------------------------------------------------------
@frappe.whitelist()
def rollback_offer_rejection(lead):
    """When lead rejects all offers, roll back status to the previous pipeline stage."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)

    if _standalone_interests_available():
        target = _reconcile_standalone_rollup(lead, _("Offer rejection rollup"))
        return {"status": target}
    previous = doc.get("previous_status") or (LEAD_STATUS_FRESH if doc.get("source") else LEAD_STATUS_NEW)
    _set_lead_status(doc, previous, _("Offer rejected"))
    doc.previous_status = ""
    _save_workflow_doc(doc)
    return {"status": doc.status}


# ---------------------------------------------------------------------------
# 11. Mark Lead as Negotiating
# ---------------------------------------------------------------------------
@frappe.whitelist()
def mark_lead_negotiating(lead, unit=None):
    """Retired: all buyer mutations use the scoped action cycle."""
    frappe.throw(_("Use the current scoped workflow action form."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# 12. Add Outsource Interest Record
# ---------------------------------------------------------------------------
@frappe.whitelist()
def add_outsource_interest(lead, company_name, broker_name=None, broker_number=None, unit_details=None):
    """Add an authoritative standalone outsource Interest."""
    require_interest_creation_context()
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    if not str(company_name or "").strip():
        frappe.throw(_("Outsource company name is mandatory."))
    _add_lead_comment(doc, _("Outsource unit added from {0}").format(company_name))
    doc.save(ignore_permissions=True)
    standalone = _create_standalone_interest(
        doc,
        {
            "record_type": "Outsource",
            "category": "Outsource",
            "workflow_status": "Matched",
            "outsource_company": company_name,
            "outsource_broker_name": broker_name,
            "outsource_broker_number": broker_number,
            "outsource_unit_details": unit_details,
        },
        reason=_("Outsource Interest added"),
    )
    _reconcile_standalone_rollup(lead, _("Outsource interest added"))
    return {"added": True, "company": company_name, "interest": standalone.name}


# ---------------------------------------------------------------------------
# 13. Mark Unit Interest Lost
# ---------------------------------------------------------------------------
@frappe.whitelist()
def mark_unit_interest_lost(lead, row_name):
    """Retired: all buyer mutations use the scoped action cycle."""
    frappe.throw(_("Use the current scoped workflow action form."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# 14. Interest Record Editing and Manager-approved Deletion
# ---------------------------------------------------------------------------
@frappe.whitelist()
def update_interest_record(lead, row_name, interest_data):
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    _lock_lead_workflow(lead)
    row = _get_standalone_interest(lead, row_name)
    if row.workflow_status != "Requested" or _open_standalone_action_for_interest(row.name):
        frappe.throw(_("Only an unlocked Requested Interest may be edited directly."), frappe.PermissionError)
    data = _parse_json_object(interest_data, _("Interest"))
    if data.get("units") or data.get("unit") or data.get("interest_category", row.category) != row.category:
        frappe.throw(_("Use the workflow to select inventory or change category."), frappe.PermissionError)
    original = {
        "interest_category": row.category, "preferred_destination": row.get("requested_destination"),
        "deal_type": row.get("deal_type") or "Buy",
        "preferred_unit_category": row.get("requested_unit_category"),
        "rent_budget": requirement_amount(row, "requested_rent_budget"),
        "preferred_unit_type": row.get("requested_unit_type"), "buyer_budget": requirement_amount(row, "requested_budget"),
        "down_payment_budget": requirement_amount(row, "requested_down_payment_budget"),
        "monthly_installment_budget": requirement_amount(row, "requested_monthly_installment_budget"),
        "preferred_delivery_date": row.get("requested_delivery_date"),
        "preferred_compound": row.get("requested_project"), "preferred_developer": row.get("requested_developer"),
        "preferred_finishing_type": row.get("requested_finishing_type"),
        "inventory_search_scope": row.get("inventory_search_scope"), "budget_currency": row.get("budget_currency"),
        "international_type": row.get("international_type"), "international_country": row.get("international_country"),
    }
    merged = {**original, **data}
    try:
        snapshot = requirement_snapshot(merged, currency=_site_currency(), required_total=row.category in ("Resale", "Primary"))
    except RequirementError as exc:
        frappe.throw(str(exc))
    if is_property_requirement(row.category, merged.get("international_type")) and not snapshot.get("requested_destination"):
        frappe.throw(_("Preferred Destination is mandatory."))
    if not snapshot.get("budget_currency") and any(snapshot.get(key) is not None for key in (
        "requested_budget", "requested_down_payment_budget", "requested_monthly_installment_budget",
        "requested_rent_budget")
    ):
        frappe.throw(_("Set Budget Currency or configure FCRM Settings currency before saving affordability amounts."))
    _validate_requirement_relations(snapshot)
    if row.category in ("Resale", "Primary") and not snapshot.get("requested_unit_type"):
        frappe.throw(_("Preferred Unit Type is mandatory."))
    for field, value in snapshot.items():
        row.set(field, value)
    if row.category == "Brokerage Request":
        notes = data.get("request_notes", row.request_notes)
        if not notes:
            frappe.throw(_("Brokerage requirements are mandatory."))
        row.request_notes = notes
        row.request_status = "Open"
    if row.category == "International":
        if not merged.get("international_type") or not merged.get("international_country"):
            frappe.throw(_("International category and Country are mandatory."))
        for field in ("international_type", "international_country", "international_details"):
            if field in data:
                row.set(field, data[field])
    row.save(ignore_permissions=True)
    _mirror_standalone_interest(row)
    _reconcile_standalone_rollup(lead, _("Interest requirements updated"))
    _add_lead_comment(doc, _("Interest requirements updated: {0}").format(row.name))
    return {"updated": True, "row": row.as_dict(), "interest": row.name}


@frappe.whitelist()
def request_interest_deletion(lead, row_name, reason):
    """Create a manager approval task; the interest row remains untouched."""
    if not reason:
        frappe.throw(_("Deletion reason is mandatory."))
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    standalone = (
        _get_standalone_interest(lead, row_name)
        if _standalone_interests_available()
        else None
    )
    row = None if standalone else _find_interest_row(doc, row_name)
    source = standalone or row
    if source.get("deletion_request_status") == "Pending Manager Approval":
        return {"requested": True, "request": source.get("deletion_request")}

    managers = frappe.get_all(
        "Has Role",
        filters={"role": "Sales Manager", "parenttype": "User"},
        pluck="parent",
    )
    managers = [user for user in managers if frappe.db.get_value("User", user, "enabled")]
    allocated_to = managers[0] if managers else "Administrator"
    task = frappe.get_doc({
        "doctype": "ToDo",
        "allocated_to": allocated_to,
        "assigned_by": frappe.session.user,
        "description": _("Approve deletion of interest {0} from lead {1}. Reason: {2}").format(
            row_name, lead, reason
        ),
        "reference_type": "CRM Lead",
        "reference_name": lead,
        "priority": "High",
        "status": "Open",
    })
    task.insert(ignore_permissions=True)

    source.deletion_request_status = "Pending Manager Approval"
    source.deletion_request = task.name
    _add_lead_comment(doc, _("Interest deletion requested for manager approval: {0}").format(row_name))
    doc.save(ignore_permissions=True)
    if standalone:
        standalone.save(ignore_permissions=True)
        _mirror_standalone_interest(standalone)
    return {
        "requested": True,
        "request": task.name,
        "allocated_to": allocated_to,
        "interest": standalone.name if standalone else row.name,
    }


@frappe.whitelist()
def review_interest_deletion(lead, row_name, decision, request_name=None, note=None):
    """Sales Manager/System Manager approves or rejects a pending deletion."""
    if not _is_manager():
        frappe.throw(_("Only a Sales Manager or System Manager can review deletion requests."), frappe.PermissionError)
    if decision not in ("Approve", "Reject"):
        frappe.throw(_("Decision must be Approve or Reject."))

    doc = _get_lead_doc(lead)
    standalone = (
        _get_standalone_interest(lead, row_name)
        if _standalone_interests_available()
        else None
    )
    row = None if standalone else _find_interest_row(doc, row_name)
    source = standalone or row
    linked_request = source.get("deletion_request")
    if request_name and request_name != linked_request:
        frappe.throw(
            _("The supplied approval task does not match this Interest."),
            frappe.PermissionError,
        )
    if source.get("deletion_request_status") != "Pending Manager Approval":
        frappe.throw(_("This interest record has no pending deletion request."))
    if linked_request:
        todo_reference = frappe.db.get_value(
            "ToDo",
            linked_request,
            ["reference_type", "reference_name"],
            as_dict=True,
        )
        if (
            not todo_reference
            or todo_reference.reference_type != "CRM Lead"
            or todo_reference.reference_name != lead
        ):
            frappe.throw(
                _("The stored deletion approval task is not linked to this Lead."),
                frappe.PermissionError,
            )

    if decision == "Approve":
        legacy_row_name = standalone.legacy_child_row if standalone else row.name
        if standalone:
            _transition_standalone_interest(
                standalone.name,
                "Cancelled",
                outcome="Deletion Approved",
                reason=note or _("Manager-approved interest removal"),
                force=True,
                mirror_legacy=False,
            )
        if legacy_row_name:
            doc.set(
                "interested_in_units",
                [r for r in doc.get("interested_in_units") or [] if r.name != legacy_row_name],
            )
        _add_lead_comment(doc, _("Interest deletion approved by {0}: {1}. {2}").format(
            frappe.session.user, row_name, note or ""
        ))
        _save_approved_interest_deletion(doc, [legacy_row_name])
        if standalone:
            _reconcile_standalone_rollup(lead, _("Interest deletion approved"))
    else:
        source.deletion_request_status = "Rejected"
        _add_lead_comment(doc, _("Interest deletion rejected by {0}: {1}. {2}").format(
            frappe.session.user, row_name, note or ""
        ))
        doc.save(ignore_permissions=True)
        if standalone:
            standalone.save(ignore_permissions=True)
            _mirror_standalone_interest(standalone)

    if linked_request and frappe.db.exists("ToDo", linked_request):
        frappe.db.set_value("ToDo", linked_request, "status", "Closed")

    return {
        "reviewed": True,
        "decision": decision,
        "deleted": decision == "Approve",
        "interest": standalone.name if standalone else row.name,
    }


@frappe.whitelist()
def get_interest_workflow_context(lead):
    """Return standalone Interests and role capabilities required by the workboard."""
    doc = _get_lead_doc(lead)
    return {
        "rows": _eligible_interest_rows(doc),
        "workboard": _interest_workboard(doc),
        "can_review_deletions": _is_manager(),
    }


# ---------------------------------------------------------------------------
# 15. Smart Event View and Sales Progress Data
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_lead_smart_events(lead):
    """Return all linked events ordered by urgency and business importance."""
    if not frappe.db.exists("CRM Lead", lead):
        return []
    participants = frappe.get_all(
        "Event Participants",
        filters={"reference_doctype": "CRM Lead", "reference_docname": lead},
        pluck="parent",
    )
    if not participants:
        return []

    events = frappe.get_all(
        "Event",
        filters={"name": ["in", list(dict.fromkeys(participants))]},
        fields=["name", "subject", "starts_on", "ends_on", "event_type", "status", "description", "owner"],
    )
    for event in events:
        bucket, bucket_rank, action_rank = _event_priority(event.subject, event.starts_on, event.status)
        event.priority_bucket = bucket
        event.priority_rank = bucket_rank
        event.action_rank = action_rank
    events.sort(key=lambda item: (item.priority_rank, item.action_rank, get_datetime(item.starts_on)))
    return events


@frappe.whitelist()
def get_lead_progress(lead):
    """Return ideal stage targets and actual audited status transitions."""
    doc = _get_lead_doc(lead)
    creation = get_datetime(doc.creation)
    ideal = [
        {"status": status, "day": day, "hours": day * 24}
        for status, day in IDEAL_STAGE_DAYS.items()
    ]

    transitions = []
    if frappe.db.exists("DocType", "Lead Status Transition"):
        transitions = frappe.get_all(
            "Lead Status Transition",
            filters={"lead": lead},
            fields=["name", "from_status", "to_status", "action", "transitioned_on", "actor"],
            order_by="transitioned_on asc",
        )

    actual = [{
        "status": doc.get("status") if not transitions else (transitions[0].from_status or LEAD_STATUS_NEW),
        "hours": 0,
        "transitioned_on": str(doc.creation),
        "action": _("Lead created"),
        "actor": doc.owner,
    }]
    for transition in transitions:
        actual.append({
            "status": transition.to_status,
            "hours": round(float(time_diff_in_hours(get_datetime(transition.transitioned_on), creation)), 2),
            "transitioned_on": str(transition.transitioned_on),
            "action": transition.action,
            "actor": transition.actor,
        })

    last_action = actual[-1] if len(actual) > 1 else None
    return {
        "ideal": ideal,
        "actual": actual,
        "last_status_action": last_action,
        "current_status": doc.status,
    }


# ---------------------------------------------------------------------------
# 16. Dynamic Lead Action Cycle (context-aware workflow authority)
# ---------------------------------------------------------------------------
ACTION_OPEN_STATUSES = ("Planned", "Due", "In Progress")
ACTION_TERMINAL_STATUSES = ("Completed", "Cancelled", "Rescheduled", "Missed")
ACTION_TYPES = ("Call", "Add Interest", "Meeting", "Showing", "Send Offer", "Offer Decision", "Negotiation Follow-up", "Closing")
ACTION_PURPOSES = (
    "Initial Qualification",
    "Requirements Discovery",
    "Meeting Confirmation",
    "Showing Confirmation",
    "Offer Follow-up",
    "Negotiation Follow-up",
    "General Follow-up",
    "Explore Meeting",
    "Owner Meeting",
    "Developer Meeting",
    "Contract Signing",
    "Offer Decision",
    "Match Requested Unit",
)


def _require_action_doctype():
    if not frappe.db.exists("DocType", "Lead Action Execution"):
        frappe.throw(_("Lead Action Execution is not installed. Run migrate after updating Real Estate CRM Customs."))


def _parse_json_list(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(item for item in value if item))


def _action_interest_rows(action):
    """Return authoritative standalone Lead Interest names for this action."""
    if _standalone_interests_available():
        try:
            return _standalone_action_interest_names(action)
        except (frappe.DoesNotExistError, frappe.PermissionError):
            pass
    return _parse_json_list(action.get("interest_rows"))


def _serialize_action(action):
    if not action:
        return None
    result = action.as_dict()
    result["legacy_interest_rows"] = _parse_json_list(action.get("interest_rows"))
    result["interest_rows"] = _action_interest_rows(action)
    result["interest_names"] = result["interest_rows"]
    result["interest_scopes"] = [row.as_dict() for row in (action.get("interest_scopes") or [])]
    return result


def _validate_action_type(action_type, purpose=None):
    if action_type not in ACTION_TYPES or (purpose and purpose not in ACTION_PURPOSES):
        frappe.throw(_("Invalid action type or purpose."))
    if action_type == "Meeting" and purpose not in ("Explore Meeting", "Owner Meeting", "Developer Meeting"):
        frappe.throw(_("Choose Explore Meeting, Owner Meeting, or Developer Meeting."))
    if action_type == "Closing" and purpose != "Contract Signing":
        frappe.throw(_("Closing actions must use Contract Signing purpose."))
    if action_type == "Showing" and purpose not in ("Showing Confirmation", "General Follow-up"):
        frappe.throw(_("Invalid Showing purpose."))
    if action_type == "Offer Decision" and purpose != "Offer Decision":
        frappe.throw(_("Invalid Offer Decision purpose."))
    if action_type == "Negotiation Follow-up" and purpose != "Negotiation Follow-up":
        frappe.throw(_("Invalid Negotiation purpose."))


def _get_action_doc(action_name, lead=None):
    _require_action_doctype()
    if not frappe.db.exists("Lead Action Execution", action_name):
        frappe.throw(_("Lead action {0} was not found.").format(action_name), frappe.DoesNotExistError)
    action = frappe.get_doc("Lead Action Execution", action_name)
    if lead and action.lead != lead:
        frappe.throw(_("Lead action {0} does not belong to this lead.").format(action_name), frappe.PermissionError)
    return action


def _lock_lead_workflow(lead):
    """Serialize workflow mutations for one lead to prevent duplicate required actions."""
    frappe.db.sql("SELECT `name` FROM `tabCRM Lead` WHERE `name` = %s FOR UPDATE", (lead,))


def _get_required_action(lead, include_terminal=False):
    """Return only the open Lead-level action; Interest locks are resolved per scope row."""
    _require_action_doctype()
    statuses = list(ACTION_OPEN_STATUSES if not include_terminal else ACTION_OPEN_STATUSES + ACTION_TERMINAL_STATUSES)
    actions = frappe.get_all(
        "Lead Action Execution",
        filters={"lead": lead, "is_required": 1, "workflow_status": ["in", statuses]},
        fields=["name", "scheduled_start", "workflow_status", "scope_type"],
        order_by="scheduled_start asc, creation asc",
        limit_page_length=0,
    )
    for row in actions:
        action = _get_action_doc(row.name, lead)
        scope_names = _action_interest_rows(action)
        if action.get("scope_type") == "Lead" or (not action.get("scope_type") and not scope_names):
            return action
    return None


def _assert_action_is_current(action):
    """Validate an action against its own Lead or Interest lock, not another row's work."""
    interest_names = _action_interest_rows(action)
    if interest_names and _standalone_interests_available():
        for interest_name in interest_names:
            current = _open_standalone_action_for_interest(interest_name)
            if current != action.name:
                frappe.throw(
                    _("Action {0} is not the current required action for Interest {1}.").format(
                        action.name, interest_name
                    ),
                    frappe.PermissionError,
                )
        return action
    current = _get_required_action(action.lead)
    if not current or current.name != action.name:
        frappe.throw(_("Only the current required Lead action can be changed."), frappe.PermissionError)
    return action


def _set_action_event_state(action, event_status=None):
    if not action.get("event") or not frappe.db.exists("Event", action.event):
        return
    event = frappe.get_doc("Event", action.event)
    if event_status:
        event.status = event_status
    event.save(ignore_permissions=True)


def _transition_action_to_due(action):
    if action.workflow_status == "Planned" and get_datetime(action.scheduled_start) <= now_datetime():
        action.workflow_status = "Due"
        action.save(ignore_permissions=True)
    return action


def _action_schema(action, lead_doc):
    """Return the result form contract required by the frontend for this action."""
    if action.action_type == "Call":
        if action.purpose == "Initial Qualification" and not lead_doc.get("interest_status"):
            return "call.initial_qualification.v1"
        if action.purpose == "Offer Follow-up":
            return "call.offer_follow_up.v1"
        if action.purpose == "Meeting Confirmation":
            return "call.meeting_confirmation.v1"
        if action.purpose == "Showing Confirmation":
            return "call.showing_confirmation.v1"
        if action.purpose == "Negotiation Follow-up":
            return "call.negotiation_follow_up.v1"
        return "call.follow_up.v1"
    if action.action_type == "Meeting":
        meeting_schema = {
            "Explore Meeting": "meeting.explore.v2",
            "Owner Meeting": "meeting.owner.v2",
            "Developer Meeting": "meeting.developer.v1",
        }
        return meeting_schema.get(action.purpose, "meeting.result.v1")
    if action.action_type == "Closing":
        return "closing.contract.v1"
    if action.action_type == "Offer Decision":
        return "offer.decision.v1"
    if action.action_type == "Showing":
        return "showing.result.v1"
    if action.action_type == "Send Offer":
        return "offer.dispatch.v1"
    if action.action_type == "Negotiation Follow-up":
        return "negotiation.result.v1"
    if action.action_type == "Add Interest":
        return "interest.add_or_edit.v1"
    return "generic.result.v1"


def _allowed_action_definitions(lead_doc):
    """Return Lead-level actions only; unit-specific actions belong to each Lead Interest."""
    if _is_owner_role(lead_doc.get("party_type")):
        return []
    if lead_doc.get("interest_status") != "Interested":
        return [{
            "action_type": "Call",
            "purpose": "Initial Qualification",
            "label": _("Call and qualify the lead"),
            "requires_interest_rows": False,
            "requires_unit": False,
            "scope_type": "Lead",
        }]

    options = [
        {
            "action_type": "Add Interest",
            "purpose": "Requirements Discovery",
            "label": _("Add new buyer interest"),
            "requires_interest_rows": False,
            "requires_unit": False,
            "scope_type": "Lead",
        },
        {
            "action_type": "Call",
            "purpose": "General Follow-up",
            "label": _("General lead follow-up call"),
            "requires_interest_rows": False,
            "requires_unit": False,
            "scope_type": "Lead",
        },
        {
            "action_type": "Meeting",
            "purpose": "Explore Meeting",
            "label": _("Explore requirements meeting"),
            "requires_interest_rows": False,
            "requires_unit": False,
            "scope_type": "Lead",
        },
    ]
    matched = (
        [
            row.name
            for row in _get_standalone_interests(lead_doc.name, include_closed=False)
            if row.workflow_status == "Matched"
            and not _open_standalone_action_for_interest(row.name)
        ]
        if _standalone_interests_available()
        else []
    )
    if matched:
        options.append({
            "action_type": "Send Offer",
            "purpose": "Offer Follow-up",
            "label": _("Send selected matched offers by WhatsApp"),
            "requires_interest_rows": True,
            "requires_unit": False,
            "interest_row_names": matched,
            "scope_type": "Batch",
        })
    return options


def _allowed_interest_action_definitions(interest):
    """Legal row-level actions, including explicit signing and category-specific meetings."""
    if not interest.is_active or interest.workflow_status in ("Rejected", "Superseded", "Cancelled", "Fulfilled", "Closed Won"):
        return []
    scoped = {"interest_row_names": [interest.name], "interest_names": [interest.name],
        "requires_interest_rows": True, "scope_type": "Interest", "unit": interest.unit, "requires_unit": bool(interest.unit)}
    actions = []
    state = interest.workflow_status
    unit = frappe.db.get_value("Real Estate Unit", interest.unit, ["status", "owner_lead", "developer", "closing_interest"], as_dict=True) if interest.unit else None
    if unit and (unit.status in ("Sold", "Rented") or (unit.status == "Reserved" and unit.closing_interest != interest.name)):
        return []
    def add(kind, purpose, label):
        actions.append({**scoped, "action_type": kind, "purpose": purpose, "label": _(label)})
    if state == "Requested":
        if interest.category in ("Resale", "Primary"):
            add("Add Interest", "Match Requested Unit", "Run Smart Match and link inventory")
        add("Call", "Requirements Discovery", "Follow up this request")
    if state in ("Requested", "Matched") and interest.category in ("Resale", "Primary"):
        add("Meeting", "Explore Meeting", "Explore this requirement")
    if state == "Matched":
        add("Call", "General Follow-up", "Discuss this matched unit")
    if state in ("Offer Sent", "Offer Viewed"):
        add("Offer Decision", "Offer Decision", "Record this offer decision")
        add("Call", "Offer Follow-up", "Follow up this offer")
    if state in ("Offer Accepted", "Negotiating", "Shown - Considering", "Shown - Interested"):
        add("Negotiation Follow-up", "Negotiation Follow-up", "Record negotiation for this unit")
        add("Showing", "Showing Confirmation", "Schedule or record showing for this unit")
    if state in ("Offer Accepted", "Negotiating", "Shown - Interested", "Closing") and interest.unit:
        add("Closing", "Contract Signing", "Record contract signing" if state == "Closing" else "Begin contract closing")
    if state in ("Offer Accepted", "Negotiating", "Shown - Considering", "Shown - Interested", "Closing") and unit:
        if interest.category == "Resale" and unit.owner_lead and _is_owner_role(frappe.db.get_value("CRM Lead", unit.owner_lead, "party_type")):
            add("Meeting", "Owner Meeting", "Meet this Resale unit owner")
        if interest.category == "Primary" and not unit.owner_lead and unit.developer:
            add("Meeting", "Developer Meeting", "Meet this Primary unit developer")
        add("Meeting", "Explore Meeting", "Explore this property requirement")
    return actions


def _current_workflow_snapshot(lead_doc):
    if _standalone_interests_available():
        interests = _get_standalone_interests(lead_doc.name, include_closed=True)
        active = [row for row in interests if row.is_active]
        return {
            "closed_won_count": sum(row.workflow_status == "Closed Won" and bool(row.get("contract_reference")) and bool(row.get("contract_signed_date")) for row in interests),
            "closing_interest_count": sum(row.workflow_status == "Closing" for row in active),
            "active_interest_count": len(active),
            "sent_offer_count": len([
                row for row in active if row.workflow_status in ("Offer Sent", "Offer Viewed")
            ]),
            "negotiating_interest_count": len([
                row for row in active if row.workflow_status in (
                    "Offer Accepted", "Negotiating", "Showing Scheduled", "Shown - Considering", "Shown - Interested"
                )
            ]),
            "has_unmatched_request": any(row.workflow_status == "Requested" for row in active),
        }

    rows = lead_doc.get("interested_in_units") or []
    active_rows = [row for row in rows if row.get("unit_interest_status") != "Lost Interest"]
    sent_rows = [row for row in active_rows if row.get("offer_sent") and row.get("proposal_status") != "Rejected"]
    accepted_rows = [row for row in sent_rows if row.get("proposal_status") == "Offer Accepted"]
    return {
        "active_interest_count": len(active_rows),
        "sent_offer_count": len(sent_rows),
        "negotiating_interest_count": len(accepted_rows),
        "has_unmatched_request": any(
            row.get("interest_record_type") in ("Request", "International")
            and row.get("unit_interest_status") != "Lost Interest"
            and row.get("request_status") not in ("Fulfilled", "Cancelled")
            for row in rows
        ),
    }


def _eligible_interest_rows(lead_doc):
    if _standalone_interests_available():
        rows = []
        for interest in _get_standalone_interests(lead_doc.name, include_closed=True):
            label = interest.unit or interest.request_notes or interest.international_country or interest.name
            unit_facts = frappe.db.get_value("Real Estate Unit", interest.unit, ["owner_lead", "developer"], as_dict=True) if interest.unit else {}
            unit_facts = unit_facts or {}
            rows.append({
                "name": interest.name,
                "legacy_row_name": interest.legacy_child_row,
                "label": _("{0}: {1}").format(interest.category or "Interest", label),
                "unit": interest.unit,
                "owner_lead": unit_facts.get("owner_lead"),
                "unit_developer": unit_facts.get("developer"),
                "developer": unit_facts.get("developer"),
                "owner_party_role": frappe.db.get_value("CRM Lead", unit_facts.owner_lead, "party_type") if unit_facts.get("owner_lead") else None,
                "interest_category": interest.category,
                "record_type": interest.record_type,
                "workflow_status": interest.workflow_status,
                "is_active": int(interest.is_active or 0),
                "offer_sent": int(interest.workflow_status in ("Offer Sent", "Offer Viewed", "Offer Accepted", "Negotiating", "Showing Scheduled", "Shown - Interested", "Shown - Considering")),
                "proposal_status": {
                    "Offer Sent": "Sent",
                    "Offer Viewed": "Viewed",
                    "Offer Accepted": "Offer Accepted",
                    "Negotiating": "Offer Accepted",
                    "Showing Scheduled": "Offer Accepted",
                    "Shown - Interested": "Offer Accepted",
                    "Shown - Considering": "Offer Accepted",
                    "Rejected": "Rejected",
                }.get(interest.workflow_status, "Pending"),
                "request_status": interest.request_status,
                "request_notes": interest.request_notes,
                "requested_destination": interest.requested_destination,
                "requested_area": interest.requested_area,
                "deal_type": interest.get("deal_type") or "Buy",
                "requested_unit_category": interest.get("requested_unit_category"),
                "requested_unit_type": interest.requested_unit_type,
                "requested_budget": requirement_amount(interest, "requested_budget"),
                "requested_down_payment_budget": requirement_amount(interest, "requested_down_payment_budget"),
                "requested_monthly_installment_budget": requirement_amount(interest, "requested_monthly_installment_budget"),
                "requested_rent_budget": requirement_amount(interest, "requested_rent_budget"),
                "requested_delivery_date": interest.get("requested_delivery_date"),
                "inventory_search_scope": interest.get("inventory_search_scope"),
                "budget_currency": interest.get("budget_currency") or _site_currency(),
                "contract_reference": interest.get("contract_reference"),
                "contract_signed_date": interest.get("contract_signed_date"),
                "signed_contract": interest.get("signed_contract"),
                "contract_recorded_by": interest.get("contract_recorded_by"),
                "closed_won_at": interest.get("closed_won_at"),

                "requested_project": interest.requested_project,
                "requested_developer": interest.requested_developer,
                "requested_finishing_type": interest.requested_finishing_type,
                "requested_delivery_time": interest.requested_delivery_time,
            })
        return rows

    rows = []
    for row in lead_doc.get("interested_in_units") or []:
        if row.get("unit_interest_status") == "Lost Interest":
            continue
        label = row.get("unit") or row.get("request_notes") or row.get("international_country") or row.name
        rows.append({
            "name": row.name,
            "label": _("{0}: {1}").format(row.get("interest_category") or "Interest", label),
            "unit": row.get("unit"),
            "interest_category": row.get("interest_category"),
            "offer_sent": int(row.get("offer_sent") or 0),
            "proposal_status": row.get("proposal_status") or "Pending",
            "workflow_status": "Rejected" if row.get("unit_interest_status") == "Lost Interest" else "Matched",
            "is_active": int(row.get("unit_interest_status") != "Lost Interest"),
        })
    return rows


def _validate_interest_unit_commitment(interest):
    """Serialize against unit closing/sale, preserving the winning Interest only."""
    if not interest.unit:
        return
    frappe.db.sql("SELECT `name` FROM `tabReal Estate Unit` WHERE `name` = %s FOR UPDATE", (interest.unit,))
    unit = frappe.db.get_value("Real Estate Unit", interest.unit, ["status", "closing_interest"], as_dict=True)
    if not unit or unit.status in ("Sold", "Rented") or (unit.status == "Reserved" and unit.closing_interest != interest.name):
        frappe.throw(_("The Unit is sold, rented, or reserved by another closing."))


def _validate_action_interest_scope(lead_doc, action_type, purpose, row_names, unit=None):
    selected = _resolve_standalone_interest_names(lead_doc.name, row_names)
    commercial = action_type in ("Send Offer", "Offer Decision", "Negotiation Follow-up", "Showing", "Closing") or (
        action_type == "Meeting" and purpose in ("Owner Meeting", "Developer Meeting"))
    if commercial and not selected:
        frappe.throw(_("Select the Interest for this commercial action."))
    if not selected:
        if unit:
            frappe.throw(_("A Unit requires its exact scoped Interest."))
        return
    rows = [_get_standalone_interest(lead_doc.name, name) for name in selected]
    if action_type != "Send Offer" and len(rows) != 1:
        frappe.throw(_("Only Send Offer may target multiple Interests."))
    for row in sorted(rows, key=lambda item: item.unit or item.name):
        _validate_interest_unit_commitment(row)
        if not row.is_active:
            frappe.throw(_("This Interest is terminal."))
        if unit and row.unit != unit:
            frappe.throw(_("The action Unit must match its Interest."))
        if row.unit:
            data = frappe.db.get_value("Real Estate Unit", row.unit, ["status", "closing_interest"], as_dict=True)
            if not data or data.status in ("Sold", "Rented") or (data.status == "Reserved" and data.closing_interest != row.name):
                frappe.throw(_("The Unit is sold, rented, or reserved by another closing."))
        if action_type == "Send Offer":
            if row.record_type != "Inventory Unit" or not row.unit or row.workflow_status != "Matched":
                frappe.throw(_("Send Offer requires Matched inventory Interests."))
        elif action_type == "Add Interest" and purpose == "Match Requested Unit":
            if row.workflow_status != "Requested" or row.category not in ("Resale", "Primary"):
                frappe.throw(_("Only a Resale or Primary Request can be matched."))
        elif not any(item["action_type"] == action_type and item["purpose"] == purpose for item in _allowed_interest_action_definitions(row)):
            frappe.throw(_("Action is not permitted for this Interest's current status."), frappe.PermissionError)
    if action_type in ("Showing", "Closing") or (action_type == "Meeting" and purpose in ("Owner Meeting", "Developer Meeting")):
        if not unit or unit != rows[0].unit:
            frappe.throw(_("Select the exact Unit linked to this Interest."))


def _pipeline_target_from_facts(lead_doc):
    """Resolve the highest verified commercial milestone without relying on the last dialog."""
    facts = _current_workflow_snapshot(lead_doc)
    if facts.get("closed_won_count"):
        return LEAD_STATUS_CLOSED_WON
    if facts.get("closing_interest_count"):
        return LEAD_STATUS_CLOSING
    if facts["negotiating_interest_count"]:
        return LEAD_STATUS_NEGOTIATING
    if facts["sent_offer_count"]:
        return LEAD_STATUS_OFFER_SENT
    if facts["has_unmatched_request"]:
        return LEAD_STATUS_REQUESTED
    return None


def _latest_offer_origin_status(lead_doc):
    """Return the stage from which the current offer batch was dispatched."""
    if lead_doc.get("status") in (
        LEAD_STATUS_NEW,
        LEAD_STATUS_FRESH,
        LEAD_STATUS_REQUESTED,
    ):
        return lead_doc.get("status")
    if frappe.db.exists("DocType", "Lead Action Execution"):
        latest = frappe.get_all(
            "Lead Action Execution",
            filters={
                "lead": lead_doc.name,
                "action_type": "Send Offer",
                "workflow_status": "Completed",
                "outcome": "Dispatched",
            },
            fields=["offer_origin_status"],
            order_by="completed_at desc, modified desc",
            limit_page_length=1,
        )
        if latest and latest[0].get("offer_origin_status"):
            return latest[0].offer_origin_status
    if frappe.db.exists("DocType", "Lead Status Transition"):
        origins = frappe.get_all(
            "Lead Status Transition",
            filters={"lead": lead_doc.name, "to_status": LEAD_STATUS_OFFER_SENT},
            fields=["from_status"],
            order_by="transitioned_on desc, creation desc",
            limit_page_length=1,
        )
        if origins and origins[0].get("from_status") in (
            LEAD_STATUS_NEW,
            LEAD_STATUS_FRESH,
            LEAD_STATUS_REQUESTED,
        ):
            return origins[0].from_status
    previous = lead_doc.get("previous_status")
    if previous in (LEAD_STATUS_NEW, LEAD_STATUS_FRESH, LEAD_STATUS_REQUESTED):
        return previous
    return LEAD_STATUS_REQUESTED if _current_workflow_snapshot(lead_doc)["has_unmatched_request"] else LEAD_STATUS_NEW


def _action_offer_origin(lead_doc, action_type, purpose, source_action=None):
    if source_action and source_action.get("offer_origin_status"):
        return source_action.get("offer_origin_status")
    if purpose in ("Offer Follow-up", "Offer Decision", "Negotiation Follow-up", "Contract Signing") or action_type in (
        "Offer Decision",
        "Showing",
        "Negotiation Follow-up",
    ):
        return _latest_offer_origin_status(lead_doc)
    return lead_doc.get("status")


def _apply_planning_milestone(lead_doc, action_type, interest_rows=None, action=None):
    if action_type not in ("Showing", "Closing"):
        return False
    names = _resolve_standalone_interest_names(lead_doc.name, interest_rows)
    if len(names) != 1:
        frappe.throw(_("Showing and Closing must target one Interest."))
    if action_type == "Closing":
        begin_interest_closing(names[0], action=action.name if action else None)
    else:
        _transition_standalone_interest(names[0], "Showing Scheduled", action=action.name if action else None,
            outcome="Scheduled", reason=_("Showing scheduled for this interest"))
    _reconcile_standalone_rollup(lead_doc.name, _("{0} planned").format(action_type))
    lead_doc.reload()
    return True


def _apply_pipeline_from_facts(lead_doc, action_label):
    if lead_doc.get("interest_status") == "Not Interested":
        return False
    target = _pipeline_target_from_facts(lead_doc)
    if not target:
        return False
    stage_rank = {
        LEAD_STATUS_NEW: 0,
        LEAD_STATUS_FRESH: 0,
        LEAD_STATUS_REQUESTED: 1,
        LEAD_STATUS_OFFER_SENT: 2,
        LEAD_STATUS_NEGOTIATING: 3,
        LEAD_STATUS_CLOSING: 4,
        LEAD_STATUS_CLOSED_WON: 5,
    }
    # Fact recalculation may advance a milestone, but it must never erase a
    # later verified stage. Explicit rollback logic handles permitted reversals.
    if stage_rank.get(lead_doc.get("status"), 0) > stage_rank.get(target, 0):
        return False
    return _set_lead_status(lead_doc, target, action_label)


def _create_action_execution(
    lead_doc,
    action_type,
    purpose,
    scheduled_start,
    notes=None,
    unit=None,
    interest_rows=None,
    source_action=None,
    is_required=1,
    create_event=True,
    offer_origin_status=None,
    allow_existing_required_action=False,
):
    """Create the authoritative workflow action and, when needed, its calendar projection."""
    _require_action_doctype()
    _validate_action_type(action_type, purpose)
    if not scheduled_start:
        scheduled_start = now_datetime()

    normalized_rows = (
        _resolve_standalone_interest_names(lead_doc.name, interest_rows)
        if _standalone_interests_available()
        else _parse_json_list(interest_rows)
    )
    rescheduling_showing = False
    if action_type == "Showing" and source_action and len(normalized_rows) == 1:
        previous_action = _get_action_doc(source_action, lead_doc.name)
        row = _get_standalone_interest(lead_doc.name, normalized_rows[0])
        rescheduling_showing = (
            previous_action.action_type == "Showing"
            and previous_action.unit == unit == row.unit
            and set(_action_interest_rows(previous_action)) == set(normalized_rows)
            and row.workflow_status == "Showing Scheduled"
        )
        if rescheduling_showing:
            _validate_interest_unit_commitment(row)
    if not rescheduling_showing:
        _validate_action_interest_scope(lead_doc, action_type, purpose, normalized_rows, unit)
    if is_required:
        if normalized_rows and _standalone_interests_available():
            _assert_standalone_interests_available(
                normalized_rows,
                exclude_action=source_action if allow_existing_required_action else None,
            )
        elif not allow_existing_required_action and _get_required_action(lead_doc.name):
            frappe.throw(_("This lead already has a required Lead-level action. Complete, reschedule, or cancel it before creating another."))

    if unit and not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit))
    if action_type in ("Showing", "Closing") and not unit:
        frappe.throw(_("A Showing action requires one related unit."))
    if action_type == "Meeting" and purpose in ("Owner Meeting", "Developer Meeting") and not unit:
        frappe.throw(_("Owner or Developer Meeting requires its exact related Unit."))

    seller_lead = None
    if unit:
        seller_lead = frappe.db.get_value("Real Estate Unit", unit, "owner_lead")

    event_name = None
    if create_event:
        event_name = _create_lead_event(
            lead_doc.name,
            _("{0}: {1}").format(action_type, purpose or lead_doc.get("lead_name") or lead_doc.name),
            scheduled_start,
            meeting_type=action_type,
            notes=notes,
        )

    action = frappe.get_doc({
        "doctype": "Lead Action Execution",
        "lead": lead_doc.name,
        "action_type": action_type,
        "purpose": purpose,
        "workflow_status": "Planned",
        "is_required": int(is_required or 0),
        "scope_type": "Batch" if len(normalized_rows) > 1 else "Interest" if normalized_rows else "Lead",
        "scheduled_start": scheduled_start,
        "pipeline_status_at_start": lead_doc.get("status"),
        "qualification_at_start": lead_doc.get("interest_status") or "Unknown",
        "event": event_name,
        "unit": unit,
        "seller_lead": seller_lead,
        "interest_rows": json.dumps(normalized_rows),
        "offer_origin_status": offer_origin_status or lead_doc.get("status"),
        "source_action": source_action,
    })
    if _standalone_interests_available():
        _append_action_scopes(action, normalized_rows, primary=normalized_rows[0] if len(normalized_rows) == 1 else None)
    action.insert(ignore_permissions=True)

    if create_event and action_type == "Meeting" and purpose == "Owner Meeting" and unit and seller_lead:
        _create_lead_event(
            seller_lead,
            _("Owner meeting scheduled for unit {0}").format(unit),
            scheduled_start,
            meeting_type="Owner Meeting",
            notes=_("Buyer: {0}; Action: {1}").format(
                lead_doc.get("lead_name") or lead_doc.name,
                action.name,
            ),
        )

    if action_type == "Showing" and unit:
        unit_doc = frappe.get_doc("Real Estate Unit", unit)
        unit_doc.append("scheduled_showings", {
            "showing_date": scheduled_start,
            "buyer_lead": lead_doc.name,
            "buyer_name": lead_doc.get("lead_name"),
            "agent": lead_doc.get("lead_owner") or frappe.session.user,
            "status": "Scheduled",
        })
        unit_doc.save(ignore_permissions=True)
        if seller_lead and frappe.db.exists("CRM Lead", seller_lead):
            _create_lead_event(
                seller_lead,
                _("Showing scheduled on your unit {0}").format(unit),
                scheduled_start,
                meeting_type="Showing",
                notes=_('Buyer: {0}; Action: {1}').format(lead_doc.get("lead_name") or lead_doc.name, action.name),
            )

    _add_lead_comment(lead_doc, _("Workflow action planned: {0} — {1}.").format(action_type, purpose or ""))
    return action


def _ensure_future_action_event(action, lead_doc):
    """Repair a missing Event only for a genuinely future, still-planned action."""
    if not action or action.action_type == "Add Interest":
        return action
    if action.workflow_status != "Planned" or not action.get("scheduled_start"):
        return action
    if get_datetime(action.scheduled_start) <= now_datetime():
        return action
    if action.get("event") and frappe.db.exists("Event", action.event):
        return action

    action.event = _create_lead_event(
        lead_doc.name,
        _("{0}: {1}").format(
            action.action_type,
            action.purpose or lead_doc.get("lead_name") or lead_doc.name,
        ),
        action.scheduled_start,
        meeting_type=action.action_type,
        notes=_("Workflow action {0}").format(action.name),
    )
    action.save(ignore_permissions=True)
    return action


def _interest_workboard(lead_doc):
    """Serialize each standalone Interest with its independent lock and action menu."""
    if not _standalone_interests_available():
        return []
    board = []
    for interest in _get_standalone_interests(lead_doc.name, include_closed=True):
        current_name = _open_standalone_action_for_interest(interest.name)
        current = _get_action_doc(current_name, lead_doc.name) if current_name else None
        if current:
            current = _transition_action_to_due(current)
            current = _ensure_future_action_event(current, lead_doc)
        allowed = (
            []
            if current or not interest.is_active or lead_doc.get("interest_status") != "Interested"
            else _allowed_interest_action_definitions(interest)
        )
        transitions = frappe.get_all(
            "Lead Interest Transition",
            filters={"lead_interest": interest.name},
            fields=["from_status", "to_status", "action", "outcome", "reason", "transitioned_on", "actor"],
            order_by="transitioned_on desc, creation desc",
            limit_page_length=5,
        ) if frappe.db.exists("DocType", "Lead Interest Transition") else []
        label = interest.unit or interest.request_notes or interest.international_country or interest.name
        unit_facts = frappe.db.get_value("Real Estate Unit", interest.unit, ["owner_lead", "developer"], as_dict=True) if interest.unit else {}
        unit_facts = unit_facts or {}
        board.append({
            "name": interest.name,
            "legacy_row_name": interest.legacy_child_row,
            "label": _("{0}: {1}").format(interest.category, label),
            "lead": interest.lead,
            "record_type": interest.record_type,
            "interest_category": interest.category,
            "workflow_status": interest.workflow_status,
            "is_active": int(interest.is_active or 0),
            "unit": interest.unit,
            "owner_lead": unit_facts.get("owner_lead"),
            "unit_developer": unit_facts.get("developer"),
            "developer": unit_facts.get("developer"),
            "owner_party_role": frappe.db.get_value("CRM Lead", unit_facts.owner_lead, "party_type") if unit_facts.get("owner_lead") else None,
            "request_status": interest.request_status,
            "request_notes": interest.request_notes,
            "requested_destination": interest.requested_destination,
            "requested_area": interest.requested_area,
            "deal_type": interest.get("deal_type") or "Buy",
            "requested_unit_category": interest.get("requested_unit_category"),
            "requested_unit_type": interest.requested_unit_type,
            "requested_budget": requirement_amount(interest, "requested_budget"),
            "requested_down_payment_budget": requirement_amount(interest, "requested_down_payment_budget"),
            "requested_monthly_installment_budget": requirement_amount(interest, "requested_monthly_installment_budget"),
            "requested_rent_budget": requirement_amount(interest, "requested_rent_budget"),
            "requested_delivery_date": interest.get("requested_delivery_date"),
            "inventory_search_scope": interest.get("inventory_search_scope"),
            "budget_currency": interest.get("budget_currency") or _site_currency(),
            "contract_reference": interest.get("contract_reference"),
            "contract_signed_date": interest.get("contract_signed_date"),
            "signed_contract": interest.get("signed_contract"),
            "contract_recorded_by": interest.get("contract_recorded_by"),
            "closed_won_at": interest.get("closed_won_at"),

            "requested_project": interest.requested_project,
            "requested_developer": interest.requested_developer,
            "requested_finishing_type": interest.requested_finishing_type,
            "requested_delivery_time": interest.requested_delivery_time,
            "international_type": interest.international_type,
            "international_country": interest.international_country,
            "international_details": interest.international_details,
            "outsource_company": interest.outsource_company,
            "outsource_broker_name": interest.outsource_broker_name,
            "outsource_broker_number": interest.outsource_broker_number,
            "outsource_unit_details": interest.outsource_unit_details,
            "deletion_request_status": interest.deletion_request_status,
            "deletion_request": interest.deletion_request,
            "latest_action": interest.latest_action,
            "last_transition_at": interest.last_transition_at,
            "current_action": _serialize_action(current),
            "allowed_actions": allowed,
            "transitions": [dict(item) for item in transitions],
        })
    return board


@frappe.whitelist()
def get_lead_interest_workboard(lead):
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    return {
        "lead": lead,
        "lead_status": lead_doc.get("status"),
        "interests": _interest_workboard(lead_doc),
    }


@frappe.whitelist()
def get_lead_action_context(lead):
    """Return the context-aware Action Web policy for the current lead."""
    lead_doc = _get_lead_doc(lead)
    if _is_owner_role(lead_doc.get("party_type")):
        return {
            "lead": lead_doc.name,
            "lead_status": lead_doc.get("status"),
            "qualification": "Not Applicable",
            "current_action": None,
            "primary_command": None,
            "allowed_actions": [],
            "allowed_next_actions": [],
            "required_result_schema": None,
            "blockers": [],
            "warnings": [],
            "facts": {},
            "interest_rows": [],
            "interest_workboard": [],
        }
    _validate_buyer_lead(lead_doc)
    _require_action_doctype()
    action = _get_required_action(lead)
    if action:
        action = _transition_action_to_due(action)
        action = _ensure_future_action_event(action, lead_doc)

    blockers = []
    warnings = []
    if action:
        if action.workflow_status in ACTION_TERMINAL_STATUSES:
            action = None
        elif action.workflow_status == "In Progress":
            primary_command = _("Record {0} result").format(action.action_type)
        else:
            primary_command = _("Do {0} now").format(action.action_type)
    else:
        primary_command = _("Choose next action")

    if lead_doc.get("interest_status") == "Not Interested":
        warnings.append(_("This lead is marked Not Interested. Use an explicit manager-approved requalification before continuing commercial actions."))
        policy_actions = []
    else:
        policy_actions = _allowed_action_definitions(lead_doc)
    # A required action suppresses immediate planning but does not suppress the
    # successor policy needed by its result form.
    allowed_actions = [] if action else policy_actions

    snapshot = _current_workflow_snapshot(lead_doc)
    if action and action.action_type == "Showing" and not action.get("unit"):
        blockers.append(_("Showing action is missing its unit context."))
    if action and action.action_type == "Send Offer" and not _action_interest_rows(action):
        blockers.append(_("Offer action is missing selected interest records."))

    return {
        "lead": lead_doc.name,
        "lead_status": lead_doc.get("status"),
        "qualification": lead_doc.get("interest_status") or "Unknown",
        "current_action": _serialize_action(action),
        "primary_command": primary_command,
        "allowed_actions": allowed_actions,
        "allowed_next_actions": policy_actions,
        "required_result_schema": _action_schema(action, lead_doc) if action else None,
        "blockers": blockers,
        "warnings": warnings,
        "facts": snapshot,
        "interest_rows": _eligible_interest_rows(lead_doc),
        "interest_workboard": _interest_workboard(lead_doc),
    }


@frappe.whitelist()
def plan_lead_action(
    lead,
    action_type,
    purpose=None,
    scheduled_start=None,
    notes=None,
    unit=None,
    interest_rows=None,
    expected_required_action=None,
    execute_now=0,
):
    """Create the next required action from the policy-approved choices only."""
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    _require_action_doctype()
    _lock_lead_workflow(lead)

    selected_rows = (
        _resolve_standalone_interest_names(lead, interest_rows)
        if _standalone_interests_available()
        else _parse_json_list(interest_rows)
    )
    if len(selected_rows) > 1 and action_type != "Send Offer":
        frappe.throw(_("Only Send Offer may target multiple interests. Select one interest for this action."))
    if selected_rows and lead_doc.get("interest_status") != "Interested":
        frappe.throw(
            _("Interest-specific commercial actions require an Interested Lead qualification."),
            frappe.PermissionError,
        )

    allowed = _allowed_action_definitions(lead_doc)
    requested = next(
        (item for item in allowed if item["action_type"] == action_type and item["purpose"] == (purpose or item["purpose"])),
        None,
    )
    if not requested and len(selected_rows) == 1 and _standalone_interests_available():
        interest = _get_standalone_interest(lead, selected_rows[0])
        requested = next(
            (
                item for item in _allowed_interest_action_definitions(interest)
                if item["action_type"] == action_type and item["purpose"] == (purpose or item["purpose"])
            ),
            None,
        )
    if not requested:
        frappe.throw(_("This action is not permitted for the selected workflow scope."), frappe.PermissionError)
    if requested["requires_unit"] and not unit:
        frappe.throw(_("This action requires a related unit."))
    if requested["requires_interest_rows"] and not selected_rows:
        frappe.throw(_("Select one or more interest records for this action."))

    if selected_rows:
        _assert_standalone_interests_available(selected_rows)
    else:
        current = _get_required_action(lead)
        if current:
            if expected_required_action and current.name == expected_required_action:
                frappe.throw(_("Complete or reschedule the current action before planning another."))
            frappe.throw(_("Lead already has required action {0}.").format(current.name))
    _validate_action_interest_scope(lead_doc, action_type, purpose or requested["purpose"], selected_rows, unit)

    execute_now = bool(_to_int(execute_now))
    if execute_now:
        scheduled_start = now_datetime()
    if not execute_now and (
        not scheduled_start or get_datetime(scheduled_start) <= now_datetime()
    ):
        frappe.throw(_("Schedule for Later requires a future date and time."))
    action = _create_action_execution(
        lead_doc=lead_doc,
        action_type=action_type,
        purpose=purpose or requested["purpose"],
        scheduled_start=scheduled_start,
        notes=notes,
        unit=unit,
        interest_rows=selected_rows,
        offer_origin_status=_action_offer_origin(
            lead_doc,
            action_type,
            purpose or requested["purpose"],
        ),
        create_event=action_type != "Add Interest" and not execute_now,
    )
    if not execute_now and action_type != "Add Interest":
        if not action.get("event") or not frappe.db.exists("Event", action.event):
            frappe.throw(_("The scheduled workflow action could not create its calendar Event."))
    _apply_planning_milestone(lead_doc, action_type, selected_rows, action)
    return {
        "action": _serialize_action(action),
        "event": action.get("event"),
        "execution_mode": "Immediate" if execute_now else "Scheduled",
        "context": get_lead_action_context(lead),
    }


@frappe.whitelist()
def start_lead_action(lead, action_name):
    """Mark the current required action In Progress, without changing the lead facts."""
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    action = _get_action_doc(action_name, lead)
    _assert_action_is_current(action)
    if action.workflow_status in ACTION_TERMINAL_STATUSES:
        frappe.throw(_("This action is already closed."))
    if action.action_type == "Call" and not _lead_contact_number(lead_doc):
        frappe.throw(_("This lead has no mobile or WhatsApp number for the call action."))
    if action.workflow_status != "In Progress":
        action.workflow_status = "In Progress"
        action.started_at = now_datetime()
        action.save(ignore_permissions=True)
    return {"action": _serialize_action(action), "context": get_lead_action_context(lead)}


def _ensure_action_not_completed(action, client_request_id=None):
    if action.workflow_status in ACTION_TERMINAL_STATUSES:
        if client_request_id and action.get("client_request_id") == client_request_id:
            return False
        frappe.throw(_("This action has already been completed or closed."), frappe.ValidationError)
    return True


def _update_unit_outcomes(lead_doc, row_names, outcome, action=None):
    """Transition only the standalone interests scoped by this unit outcome."""
    status_by_outcome = {
        "Pending": "Matched",
        "Sent": "Offer Sent",
        "Viewed": "Offer Viewed",
        "Offer Accepted": "Offer Accepted",
        "Rejected": "Rejected",
    }
    if outcome not in status_by_outcome:
        frappe.throw(_("Invalid offer outcome: {0}").format(outcome))
    if _standalone_interests_available():
        selected = _resolve_standalone_interest_names(lead_doc.name, row_names)
        if not selected:
            frappe.throw(_("Select one or more Lead Interests for this outcome."))
        for name in selected:
            interest = _get_standalone_interest(lead_doc.name, name)
            if interest.record_type != "Inventory Unit" or not interest.unit:
                frappe.throw(_("Offer outcomes can only target inventory-unit interests."))
            _transition_standalone_interest(
                interest.name,
                status_by_outcome[outcome],
                action=action.name if action else None,
                outcome=outcome,
                reason=_("Scoped unit outcome recorded"),
                force=outcome == "Pending",
            )
        return len(selected)

    selected = set(_parse_json_list(row_names))
    changed = 0
    for row in lead_doc.get("interested_in_units") or []:
        if row.name not in selected:
            continue
        row.proposal_status = outcome
        row.unit_interest_status = "Lost Interest" if outcome == "Rejected" else "Active"
        changed += 1
    if not changed:
        frappe.throw(_("No matching interest rows were found."))
    return changed


def _all_rows_rejected(lead_doc, scoped_rows):
    if _standalone_interests_available():
        names = _resolve_standalone_interest_names(lead_doc.name, scoped_rows)
        rows = [_get_standalone_interest(lead_doc.name, name) for name in names]
        return bool(rows) and all(row.workflow_status in ("Rejected", "Superseded", "Cancelled") for row in rows)
    scoped = set(_parse_json_list(scoped_rows))
    rows = [row for row in lead_doc.get("interested_in_units") or [] if row.name in scoped]
    return bool(rows) and all(row.get("proposal_status") == "Rejected" for row in rows)


def _reconcile_stage_after_unit_rejection(lead_doc, action, action_label):
    """Recalculate the Lead summary without altering unrelated interest rows."""
    if _standalone_interests_available():
        before = lead_doc.get("status")
        target = _reconcile_standalone_rollup(lead_doc.name, action_label)
        lead_doc.reload()
        return before != target
    target = _pipeline_target_from_facts(lead_doc) or action.get("offer_origin_status") or _latest_offer_origin_status(lead_doc)
    return _set_lead_status(lead_doc, target, action_label)


def _has_live_sent_offer(lead_doc):
    if _standalone_interests_available():
        return any(
            row.workflow_status in ("Offer Sent", "Offer Viewed")
            for row in _get_standalone_interests(lead_doc.name, include_closed=False)
        )
    return any(row.get("offer_sent") and row.get("proposal_status") != "Rejected" for row in (lead_doc.get("interested_in_units") or []))


def _next_action_is_required(lead_doc, action, payload):
    if payload.get("outcome") == "Rescheduled":
        return False
    if action.action_type == "Call" and action.purpose == "Initial Qualification":
        return payload.get("contact_result") != "Answered" or payload.get("qualification") == "Interested"
    if _standalone_interests_available():
        interest_names = _action_interest_rows(action)
        # A batch dispatch fans out into independent row action menus; it must not
        # create one shared successor that re-couples the offered units.
        if action.action_type == "Send Offer":
            return False
        if interest_names:
            return any(
                _get_standalone_interest(lead_doc.name, name).is_active
                for name in interest_names
            )
    return lead_doc.get("interest_status") == "Interested"


def _future_scheduled_time(value, label="Scheduled action"):
    if not value:
        frappe.throw(_("{0} requires a future date and time.").format(label))
    try:
        scheduled = get_datetime(value)
        if scheduled <= now_datetime():
            frappe.throw(_("{0} must be scheduled in the future.").format(label))
    except (TypeError, ValueError, OverflowError):
        frappe.throw(_("{0} requires a valid future date/time in the site's timezone.").format(label))
    return scheduled


def _validate_next_action_payload(lead_doc, action, result_data):
    next_action = result_data.get("next_action") or None
    required = _next_action_is_required(lead_doc, action, result_data)
    if not next_action:
        if required:
            frappe.throw(_("This action requires one next action before it can be completed."))
        return
    action_type = next_action.get("action_type")
    purpose = next_action.get("purpose")
    scheduled_start = next_action.get("scheduled_start")
    if not action_type or (not scheduled_start and not _to_int(next_action.get("execute_now"))):
        frappe.throw(_("Next action type and scheduled date/time are mandatory."))
    next_action["scheduled_start"] = str(now_datetime() if _to_int(next_action.get("execute_now"))
        else _future_scheduled_time(scheduled_start, _("Next action")))
    _validate_action_type(action_type, purpose)
    selected_rows = (
        _resolve_standalone_interest_names(lead_doc.name, next_action.get("interest_rows"))
        if _standalone_interests_available()
        else _parse_json_list(next_action.get("interest_rows"))
    )
    if len(selected_rows) > 1 and action_type != "Send Offer":
        frappe.throw(_("Only Send Offer may target multiple next-action interests."))

    allowed = _allowed_action_definitions(lead_doc)
    if len(selected_rows) == 1 and _standalone_interests_available():
        allowed = allowed + _allowed_interest_action_definitions(
            _get_standalone_interest(lead_doc.name, selected_rows[0])
        )
    if not any(item["action_type"] == action_type and item["purpose"] == purpose for item in allowed):
        frappe.throw(_("This next action is not permitted for the selected Interest's updated context."), frappe.PermissionError)
    if action_type == "Showing" and not next_action.get("unit"):
        frappe.throw(_("A next Showing action requires one related unit."))
    requires_rows = action_type in ("Send Offer", "Offer Decision", "Negotiation Follow-up", "Showing", "Closing") or (
        action_type == "Meeting" and purpose in ("Owner Meeting", "Developer Meeting")
    ) or (action_type == "Call" and purpose in ("Offer Follow-up", "Requirements Discovery"))
    if requires_rows and not selected_rows:
        frappe.throw(_("Select the Lead Interest for the next action."))
    _validate_action_interest_scope(lead_doc, action_type, purpose, selected_rows, next_action.get("unit"))


def _result_interest_rows(lead_doc, action, payload, required=False):
    """Resolve standalone result scope and prevent cross-interest mutation."""
    action_scope = set(_action_interest_rows(action))
    payload_scope = set(
        _resolve_standalone_interest_names(lead_doc.name, payload.get("interest_rows"))
        if _standalone_interests_available()
        else _parse_json_list(payload.get("interest_rows"))
    )
    if payload_scope and action_scope and not payload_scope <= action_scope:
        frappe.throw(_("The result contains an Interest outside this action's scope."), frappe.PermissionError)
    selected = payload_scope or action_scope
    if _standalone_interests_available():
        selected = set(_resolve_standalone_interest_names(lead_doc.name, list(selected)))
    else:
        valid_rows = {row.name for row in (lead_doc.get("interested_in_units") or []) if row.name}
        if not selected <= valid_rows:
            frappe.throw(_("One or more result interest records no longer belong to this lead."), frappe.PermissionError)
    if required and not selected:
        frappe.throw(_("This action result requires at least one scoped Lead Interest."))
    if action.action_type != "Send Offer" and len(selected) > 1:
        frappe.throw(_("Only Send Offer may complete multiple Lead Interests together."))
    if action.action_type == "Showing" and selected:
        interest = _get_standalone_interest(lead_doc.name, next(iter(selected))) if _standalone_interests_available() else None
        if interest and interest.unit != action.get("unit"):
            frappe.throw(_("The Showing Interest does not match the action unit."))
    return list(selected)


def _validate_call_result(action, contact_result, outcome):
    if contact_result != "Answered":
        if outcome:
            frappe.throw(_("A non-answered call cannot record a commercial outcome."))
        return
    if action.purpose == "Offer Follow-up" and outcome not in (
        "Viewed",
        "Offer Accepted",
        "Rejected",
        "Needs Alternatives",
    ):
        frappe.throw(_("An answered offer follow-up requires a valid offer outcome."))
    if action.purpose == "Negotiation Follow-up" and outcome not in (
        "Continuing",
        "Terms Changed",
        "Accepted",
        "Declined",
    ):
        frappe.throw(_("An answered negotiation follow-up requires a valid negotiation outcome."))
    if action.purpose not in (
        "Initial Qualification",
        "Offer Follow-up",
        "Negotiation Follow-up",
    ) and outcome not in ("Completed", "Needs Callback", "Confirmed", "Cancelled"):
        frappe.throw(_("An answered follow-up call requires a valid outcome."))


def _format_offer_price(value):
    if value in (None, ""):
        return _("Price on request")
    try:
        return "{:,.0f}".format(float(value))
    except (TypeError, ValueError):
        return str(value)


def _prepare_offer_whatsapp_dispatch(lead_doc, row_names, dispatch_note):
    """Build one offer message from authoritative unit data and record the dispatch."""
    phone = _lead_contact_number(lead_doc)
    if not phone:
        frappe.throw(_("Lead has no WhatsApp or mobile number for offer dispatch."))
    dispatch_note = (dispatch_note or _("Here are the property options selected for you.")).strip()

    selected_names = (
        _resolve_standalone_interest_names(lead_doc.name, row_names)
        if _standalone_interests_available()
        else _parse_json_list(row_names)
    )
    if _standalone_interests_available():
        rows_by_name = {
            name: _get_standalone_interest(lead_doc.name, name)
            for name in selected_names
        }
        rows_by_name = {
            name: row for name, row in rows_by_name.items()
            if row.record_type == "Inventory Unit" and row.unit
        }
    else:
        rows_by_name = {
            row.name: row for row in (lead_doc.get("interested_in_units") or [])
            if row.name in selected_names and row.get("unit")
        }
    if len(rows_by_name) != len(selected_names):
        frappe.throw(_("Every offer Interest must link a valid inventory unit."))

    unit_names = [rows_by_name[name].unit for name in selected_names]
    unit_rows = frappe.get_all(
        "Real Estate Unit",
        filters={"name": ["in", unit_names]},
        fields=[
            "name",
            "unit_number",
            "sku",
            "project",
            "destination",
            "developer",
            "inventory_type",
            "physical_unit_type",
            "unit_type",
            "floor",
            "bua",
            "bedrooms",
            "bathrooms",
            "finishing_type",
            "delivery_status",
            "total_gross",
            "price",
            "listing_purpose",
            "rental_monthly_rate",
            "rental_daily_rate",
            "status",
        ],
        limit_page_length=max(len(unit_names), 1),
    )
    units_by_name = {unit.name: unit for unit in unit_rows}
    project_names = list({unit.project for unit in unit_rows if unit.project})
    projects_by_name = {}
    if project_names:
        projects_by_name = {
            project.name: project
            for project in frappe.get_all(
                "Real Estate Project",
                filters={"name": ["in", project_names]},
                fields=["name", "project_name", "destination", "location"],
                limit_page_length=len(project_names),
            )
        }

    message_lines = [dispatch_note, "", _("Selected properties:")]
    offer_units = []
    for index, row_name in enumerate(selected_names, start=1):
        interest_row = rows_by_name[row_name]
        unit = units_by_name.get(interest_row.unit)
        if not unit:
            frappe.throw(_("Offer unit {0} was not found.").format(interest_row.unit))
        project = projects_by_name.get(unit.project) or {}
        project_label = project.get("project_name") or unit.project or _("Unspecified Compound")
        destination = unit.get("destination") or project.get("destination") or project.get("location") or _("Destination not specified")
        facts = [unit.get("physical_unit_type") or unit.get("unit_type"), unit.get("finishing_type")]
        if unit.get("floor") not in (None, ""):
            facts.append(_("Floor {0}").format(unit.floor))
        if unit.get("bedrooms") not in (None, ""):
            facts.append(_("{0} bedrooms").format(unit.bedrooms))
        offer_deal = interest_row.get("deal_type") or "Buy"
        effective_price, price_source = _effective_unit_price(unit, offer_deal)
        if offer_deal == "Rent":
            if unit.get("rental_monthly_rate"):
                price_line = _("Monthly Rent: {0}").format(_format_offer_price(unit.rental_monthly_rate))
            else:
                price_line = _("Daily Rent: {0}").format(_format_offer_price(unit.get("rental_daily_rate")))
        else:
            price_line = _("Total Gross: {0}").format(_format_offer_price(effective_price))
        message_lines.extend([
            "{0}. {1} — {2}".format(index, project_label, destination),
            " | ".join(filter(None, facts)),
            price_line,
            _("Reference: {0}").format(unit.get("unit_number") or unit.get("sku") or unit.name),
            "",
        ])
        offer_units.append({
            "interest_row": row_name,
            "unit": unit.name,
            "reference": unit.get("unit_number") or unit.get("sku") or unit.name,
            "project": project_label,
            "destination": destination,
            "location": destination,
            "price": effective_price,
            "price_source": price_source,
            "deal_type": offer_deal,
        })

    message = "\n".join(message_lines).strip()
    communication = frappe.get_doc({
        "doctype": "Communication",
        "communication_type": "Communication",
        "communication_medium": "Other",
        "subject": _("WhatsApp Property Offer — {0} unit(s)").format(len(offer_units)),
        "content": html.escape(message).replace("\n", "<br>"),
        "reference_doctype": "CRM Lead",
        "reference_name": lead_doc.name,
        "sender": frappe.session.user,
        "sent_or_received": "Sent",
    })
    communication.insert(ignore_permissions=True)

    clean_phone = "".join(character for character in str(phone) if character.isdigit())
    if not clean_phone:
        frappe.throw(_("Lead WhatsApp or mobile number is invalid."))
    whatsapp_url = "https://wa.me/{0}?text={1}".format(
        clean_phone,
        urllib.parse.quote(message),
    )
    return {
        "channel": "WhatsApp",
        "communication": communication.name,
        "whatsapp_url": whatsapp_url,
        "message": message,
        "units": offer_units,
    }


def _apply_action_result_facts(lead_doc, action, payload):
    """Apply scoped domain facts. No global qualification change occurs on later actions."""
    outcome = payload.get("outcome")
    contact_result = payload.get("contact_result")
    result_note = payload.get("result_note")
    next_action = payload.get("next_action")
    next_action = next_action or None
    status_changed = False
    action_output = {}

    if outcome == "Cancelled" and action.action_type not in ("Showing", "Closing"):
        return next_action, status_changed, action_output

    if action.action_type == "Call":
        if contact_result not in ("Answered", "No Answer", "Wrong Number", "Invalid / Disconnected"):
            frappe.throw(_("Select a valid contact result."))
        _validate_call_result(action, contact_result, outcome)
        if contact_result == "No Answer":
            lead_doc.no_answer_consecutive_count = _to_int(lead_doc.get("no_answer_consecutive_count")) + 1
            lead_doc.no_answer_total_count = _to_int(lead_doc.get("no_answer_total_count")) + 1
        elif contact_result == "Answered":
            lead_doc.no_answer_consecutive_count = 0
        lead_doc.last_call_outcome = contact_result
        lead_doc.last_call_at = now_datetime()

        is_initial = action.purpose == "Initial Qualification" and not action.get("qualification_at_start") in ("Interested", "Not Interested")
        if is_initial and contact_result == "Answered":
            qualification = payload.get("qualification")
            if qualification not in ("Interested", "Not Interested"):
                frappe.throw(_("Initial answered call requires Interested or Not Interested qualification."))
            lead_doc.interest_status = qualification
        elif payload.get("qualification"):
            frappe.throw(_("Lead qualification can only be changed by an initial or explicit requalification action."), frappe.PermissionError)

        if action.purpose == "Offer Follow-up" and outcome in ("Viewed", "Offer Accepted", "Rejected", "Needs Alternatives"):
            result_rows = _result_interest_rows(lead_doc, action, payload, required=True)
            if outcome == "Needs Alternatives" and _standalone_interests_available():
                for interest_name in result_rows:
                    _transition_standalone_interest(
                        interest_name,
                        "Superseded",
                        action=action.name,
                        outcome=outcome,
                        reason=_("Buyer requested alternative units"),
                    )
            else:
                _update_unit_outcomes(lead_doc, result_rows, outcome, action=action)
        if action.purpose == "Negotiation Follow-up" and outcome == "Declined":
            selected = _result_interest_rows(lead_doc, action, payload, required=True)
            _update_unit_outcomes(lead_doc, selected, "Rejected", action=action)

    elif action.action_type == "Meeting":
        if outcome not in ("Done", "No Show", "Cancelled", "Rescheduled"):
            frappe.throw(_("Meeting outcome must be Done, No Show, Cancelled, or Rescheduled."))

    elif action.action_type == "Showing":
        if not action.get("unit"):
            frappe.throw(_("Showing action has no related unit."))
        if outcome == LEGACY_OWNER_UNAVAILABLE_OUTCOME:
            # Clients cached before the Owner rename still send the old label.
            outcome = OWNER_UNAVAILABLE_OUTCOME
            payload["outcome"] = outcome
        if outcome not in ("Completed", "Buyer No Show", OWNER_UNAVAILABLE_OUTCOME, "Cancelled", "Rescheduled"):
            frappe.throw(_("Invalid showing outcome."))
        row_names = _result_interest_rows(lead_doc, action, payload, required=True)
        if outcome == "Completed":
            unit_outcome = payload.get("unit_outcome")
            if unit_outcome not in ("Interested", "Considering", "Rejected", "No Feedback"):
                frappe.throw(_("Completed showing requires a unit outcome."))
            target = {
                "Interested": "Shown - Interested",
                "Considering": "Shown - Considering",
                "No Feedback": "Shown - Considering",
                "Rejected": "Rejected",
            }[unit_outcome]
            for interest_name in row_names:
                _transition_standalone_interest(
                    interest_name,
                    target,
                    action=action.name,
                    outcome=unit_outcome,
                    reason=_("Showing result recorded"),
                )
        elif outcome in ("Buyer No Show", OWNER_UNAVAILABLE_OUTCOME, "Cancelled") and _standalone_interests_available():
            status_before = {
                scope.lead_interest: scope.status_before
                for scope in (action.get("interest_scopes") or [])
                if scope.lead_interest
            }
            restorable = {
                "Offer Accepted",
                "Negotiating",
                "Shown - Considering",
                "Shown - Interested",
            }
            for interest_name in row_names:
                target = status_before.get(interest_name)
                if target not in restorable:
                    target = "Negotiating"
                _transition_standalone_interest(
                    interest_name,
                    target,
                    action=action.name,
                    outcome=outcome,
                    reason=_("Showing closed without a completed viewing"),
                    force=True,
                )

    elif action.action_type == "Offer Decision":
        decision = payload.get("decision") or outcome
        scoped_rows = _action_interest_rows(action)
        selected = _result_interest_rows(lead_doc, action, payload, required=True)
        if len(scoped_rows) != 1:
            frappe.throw(_("Each Offer Decision must target exactly one Interest."))
        if decision == "Select an Offer":
            if len(selected) != 1:
                frappe.throw(_("Select exactly one offered Interest."))
            _update_unit_outcomes(lead_doc, selected, "Offer Accepted", action=action)
            payload["outcome"] = "Offer Accepted"
        elif decision == "Change Requirements":
            if set(selected) != set(scoped_rows):
                frappe.throw(_("Change Requirements must target this action's Interest."))
            if _standalone_interests_available():
                for interest_name in selected:
                    _transition_standalone_interest(
                        interest_name,
                        "Superseded",
                        action=action.name,
                        outcome="Requirements Changed",
                        reason=_("Requirements changed for this offered unit"),
                    )
            else:
                _update_unit_outcomes(lead_doc, selected, "Rejected", action=action)
            payload["outcome"] = "Requirements Changed"
        else:
            frappe.throw(_("Choose Select an Offer or Change Requirements."))

    elif action.action_type == "Send Offer":
        if outcome != "Dispatched":
            frappe.throw(_("An offer action can be completed only after successful dispatch."))
        row_names = _result_interest_rows(lead_doc, action, payload, required=True)
        _validate_action_interest_scope(
            lead_doc,
            action.action_type,
            action.purpose,
            row_names,
            action.get("unit"),
        )
        action_output["dispatch"] = _prepare_offer_whatsapp_dispatch(
            lead_doc,
            row_names,
            result_note,
        )
        payload["dispatch_channel"] = "WhatsApp"
        payload["communication"] = action_output["dispatch"]["communication"]
        _update_unit_outcomes(lead_doc, row_names, "Sent", action=action)

    elif action.action_type == "Negotiation Follow-up":
        if outcome not in ("Continuing", "Terms Changed", "Accepted", "Declined"):
            frappe.throw(_("Invalid negotiation outcome."))
        selected = _result_interest_rows(lead_doc, action, payload, required=True)
        if outcome in ("Continuing", "Terms Changed", "Accepted"):
            if _standalone_interests_available():
                for interest_name in selected:
                    _transition_standalone_interest(
                        interest_name,
                        "Negotiating",
                        action=action.name,
                        outcome=outcome,
                        reason=_("Negotiation result recorded"),
                    )
            else:
                _update_unit_outcomes(lead_doc, selected, "Offer Accepted", action=action)
        elif outcome == "Declined":
            _update_unit_outcomes(lead_doc, selected, "Rejected", action=action)

    elif action.action_type == "Closing":
        names = _result_interest_rows(lead_doc, action, payload, required=True)
        if len(names) != 1:
            frappe.throw(_("Contract signing requires one Interest."))
        if outcome not in ("Contract Signed", "Pending Signing", "Return to Negotiation", "Buyer Withdrew", "Cancelled", "Rescheduled"):
            frappe.throw(_("Choose a valid contract signing result."))
        if outcome == "Contract Signed":
            closed = finalize_interest_contract(names[0], payload.get("contract_reference"),
                payload.get("contract_signed_date"), signed_contract=payload.get("signed_contract"), action=action.name)
            action_output["contract"] = {"interest": closed.name, "reference": closed.contract_reference,
                "signed_date": str(closed.contract_signed_date), "signed_contract": closed.get("signed_contract")}
        elif outcome in ("Return to Negotiation", "Buyer Withdrew", "Cancelled"):
            abandon_interest_closing(names[0], target="Rejected" if outcome == "Buyer Withdrew" else "Negotiating",
                action=action.name, reason=payload.get("result_note") or outcome)
        else:
            begin_interest_closing(names[0], action=action.name)

    elif action.action_type == "Add Interest":
        # Interest rows themselves are written by the atomic bundle before this result is finalized.
        if outcome not in ("Added", "Matched", "Updated", "No Change"):
            frappe.throw(_("Interest action outcome must be Added, Matched, Updated, or No Change."))

    if action.action_type == "Call":
        # Rollup reloads the Lead; persist contact counters/qualification in the
        # same request transaction first, or reload would discard those facts.
        _save_workflow_doc(lead_doc)
    if _standalone_interests_available():
        before_status = lead_doc.get("status")
        target_status = _reconcile_standalone_rollup(
            lead_doc.name,
            _("Interest workflow updated: {0}").format(action.action_type),
        )
        lead_doc.reload()
        status_changed = before_status != target_status
    elif not status_changed:
        status_changed = _apply_pipeline_from_facts(lead_doc, _("Workflow facts updated: {0}").format(action.action_type))
    return next_action, status_changed, action_output


def _sync_unit_showing_result(lead_doc, action, outcome, result_note=None):
    """Update the existing scheduled-showing row that belongs to this action's buyer and time."""
    if action.action_type != "Showing" or not action.get("unit"):
        return
    if not frappe.db.exists("Real Estate Unit", action.unit):
        return
    status_map = {
        "Completed": "Done",
        "Buyer No Show": "Cancelled",
        OWNER_UNAVAILABLE_OUTCOME: "Cancelled",
        LEGACY_OWNER_UNAVAILABLE_OUTCOME: "Cancelled",
        "Cancelled": "Cancelled",
        "Rescheduled": "Rescheduled",
    }
    target_status = status_map.get(outcome)
    if not target_status:
        return
    unit_doc = frappe.get_doc("Real Estate Unit", action.unit)
    for row in unit_doc.get("scheduled_showings") or []:
        if row.get("buyer_lead") != lead_doc.name or row.get("status") != "Scheduled":
            continue
        if str(row.get("showing_date")) != str(action.get("scheduled_start")):
            continue
        row.status = target_status
        if result_note:
            row.result_notes = result_note
        unit_doc.save(ignore_permissions=True)
        return


@frappe.whitelist()
def complete_lead_action(lead, action_name, result_data, client_request_id=None, expected_modified=None):
    """Atomically complete the current action, apply scoped facts, then optionally plan its successor."""
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    _lock_lead_workflow(lead)
    action = _get_action_doc(action_name, lead)
    # A network retry with the same request ID is safe even though the action
    # is no longer the current required action after its first completion.
    if action.workflow_status in ACTION_TERMINAL_STATUSES and client_request_id and action.get("client_request_id") == client_request_id:
        return {"action": _serialize_action(action), "context": get_lead_action_context(lead), "idempotent": True}
    _assert_action_is_current(action)
    if expected_modified and str(action.modified) != str(expected_modified):
        frappe.throw(_("This action was changed by another user. Reload the lead before submitting."), frappe.ValidationError)
    if action.workflow_status in ("Planned", "Due"):
        action.workflow_status = "In Progress"
        action.started_at = action.get("started_at") or now_datetime()
    elif action.workflow_status != "In Progress":
        frappe.throw(_("This action cannot accept a result in its current state."), frappe.ValidationError)
    if not _ensure_action_not_completed(action, client_request_id):
        return {"action": _serialize_action(action), "context": get_lead_action_context(lead), "idempotent": True}

    if isinstance(result_data, str):
        result_data = json.loads(result_data)
    result_data = result_data or {}
    next_action, status_changed, action_output = _apply_action_result_facts(
        lead_doc,
        action,
        result_data,
    )
    _validate_next_action_payload(lead_doc, action, result_data)

    outcome = result_data.get("outcome")
    result_note = result_data.get("result_note")
    contact_result = result_data.get("contact_result")
    closed_reason = result_data.get("closed_reason")
    action.contact_result = contact_result
    action.outcome = outcome
    action.result_note = result_note
    action.result_data = json.dumps(result_data)
    action.closed_reason = closed_reason
    action.completed_by = frappe.session.user
    action.completed_at = now_datetime()
    action.client_request_id = client_request_id

    if outcome == "Rescheduled":
        reschedule_to = result_data.get("reschedule_to")
        if not reschedule_to:
            frappe.throw(_("A reschedule date and time is required."))
        reschedule_to = _future_scheduled_time(reschedule_to, _("Rescheduled action"))
        action.workflow_status = "Rescheduled"
        _set_action_event_state(action, "Closed")
        successor = _create_action_execution(
            lead_doc=lead_doc,
            action_type=action.action_type,
            purpose=action.purpose,
            scheduled_start=reschedule_to,
            notes=result_note,
            unit=action.get("unit"),
            interest_rows=_action_interest_rows(action),
            source_action=action.name,
            is_required=1,
            offer_origin_status=action.get("offer_origin_status"),
            allow_existing_required_action=True,
        )
        action.successor_action = successor.name
    elif outcome in ("Cancelled", "Buyer No Show", "No Show"):
        action.workflow_status = "Cancelled" if outcome == "Cancelled" else "Missed"
        _set_action_event_state(action, "Cancelled" if outcome == "Cancelled" else "Closed")
    else:
        action.workflow_status = "Completed"
        _set_action_event_state(action, "Closed")

    action.save(ignore_permissions=True)
    if _standalone_interests_available() and action.get("interest_scopes"):
        _update_standalone_scope_results(action, outcome=outcome or contact_result)
    _sync_unit_showing_result(lead_doc, action, outcome, result_note)
    _add_lead_comment(lead_doc, _("Workflow action completed: {0} — {1}.").format(action.action_type, outcome or contact_result or "Completed"))
    _save_workflow_doc(lead_doc)

    successor = None
    if next_action and outcome != "Rescheduled":
        next_type = next_action.get("action_type")
        next_purpose = next_action.get("purpose")
        next_starts = (
            now_datetime()
            if bool(_to_int(next_action.get("execute_now")))
            else next_action.get("scheduled_start")
        )
        if not next_type or not next_starts:
            frappe.throw(_("A next action requires type and scheduled date/time."))
        successor = _create_action_execution(
            lead_doc=lead_doc,
            action_type=next_type,
            purpose=next_purpose,
            scheduled_start=next_starts,
            notes=next_action.get("notes"),
            unit=next_action.get("unit"),
            interest_rows=next_action.get("interest_rows"),
            source_action=action.name,
            is_required=1,
            offer_origin_status=_action_offer_origin(
                lead_doc,
                next_type,
                next_purpose,
                action,
            ),
            allow_existing_required_action=True,
            create_event=(
                next_type != "Add Interest"
                and not bool(_to_int(next_action.get("execute_now")))
            ),
        )
        _apply_planning_milestone(
            lead_doc,
            next_type,
            next_action.get("interest_rows"),
            successor,
        )
        action.successor_action = successor.name
        action.save(ignore_permissions=True)

    return {
        "action": _serialize_action(action),
        "successor_action": _serialize_action(successor),
        "status": lead_doc.status,
        "status_changed": status_changed,
        "dispatch": action_output.get("dispatch"),
        "contract": action_output.get("contract"),
        "context": get_lead_action_context(lead),
    }


@frappe.whitelist()
def cancel_lead_action(lead, action_name, reason, next_action=None):
    """Cancel a current action and, where qualification continues, atomically create its required successor."""
    if not reason:
        frappe.throw(_("Cancellation reason is mandatory."))
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    _lock_lead_workflow(lead)
    action = _get_action_doc(action_name, lead)
    _assert_action_is_current(action)
    _ensure_action_not_completed(action)
    if isinstance(next_action, str):
        next_action = json.loads(next_action)
    result_data = {"outcome": "Cancelled", "next_action": next_action}
    if action.action_type == "Closing":
        _apply_action_result_facts(lead_doc, action, result_data)
    _validate_next_action_payload(lead_doc, action, result_data)

    action.workflow_status = "Cancelled"
    action.closed_reason = reason
    action.completed_at = now_datetime()
    action.completed_by = frappe.session.user
    action.result_data = json.dumps(result_data)
    action.save(ignore_permissions=True)
    _set_action_event_state(action, "Cancelled")
    _add_lead_comment(lead_doc, _("Workflow action cancelled: {0}. Reason: {1}").format(action.action_type, reason))

    successor = None
    if next_action:
        successor = _create_action_execution(
            lead_doc=lead_doc,
            action_type=next_action.get("action_type"),
            purpose=next_action.get("purpose"),
            scheduled_start=next_action.get("scheduled_start"),
            notes=next_action.get("notes"),
            unit=next_action.get("unit"),
            interest_rows=next_action.get("interest_rows"),
            source_action=action.name,
            is_required=1,
            offer_origin_status=_action_offer_origin(
                lead_doc,
                next_action.get("action_type"),
                next_action.get("purpose"),
                action,
            ),
            allow_existing_required_action=True,
            create_event=(
                next_action.get("action_type") != "Add Interest"
                and not bool(_to_int(next_action.get("execute_now")))
            ),
        )
        _apply_planning_milestone(
            lead_doc,
            next_action.get("action_type"),
            next_action.get("interest_rows"),
            successor,
        )
        action.successor_action = successor.name
        action.save(ignore_permissions=True)
    return {
        "action": _serialize_action(action),
        "successor_action": _serialize_action(successor),
        "context": get_lead_action_context(lead),
    }


@frappe.whitelist()
def requalify_lead(lead, qualification, reason):
    """Explicitly change the global qualification; never performed implicitly by a later call."""
    if not _is_manager():
        frappe.throw(_("Only Sales Manager or System Manager can requalify a lead."), frappe.PermissionError)
    if qualification not in ("Interested", "Not Interested") or not reason:
        frappe.throw(_("Qualification and reason are mandatory."))
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    lead_doc.interest_status = qualification
    _add_lead_comment(lead_doc, _("Lead requalified by {0}: {1}. Reason: {2}").format(frappe.session.user, qualification, reason))
    _save_workflow_doc(lead_doc)
    return {"qualification": qualification, "context": get_lead_action_context(lead)}


# ---------------------------------------------------------------------------
# 17. Unified single-submit action bundle
# ---------------------------------------------------------------------------
def _parse_json_object(value, label):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            frappe.throw(_("{0} must be valid JSON.").format(label))
    if value is None:
        return {}
    if not isinstance(value, dict):
        frappe.throw(_("{0} must be an object.").format(label))
    return value


def _hydrate_bundle_action_scope(lead_doc, action_data):
    """Resolve draft units and legacy identifiers to authoritative Lead Interest names."""
    if not action_data:
        return action_data
    action_data = dict(action_data)
    selected_units = set(_parse_json_list(action_data.pop("units", None)))
    identifiers = _parse_json_list(action_data.get("interest_rows"))
    if _standalone_interests_available():
        if selected_units and not identifiers:
            identifiers = [
                row.name for row in _get_standalone_interests(lead_doc.name, include_closed=False)
                if row.unit in selected_units
            ]
        action_data["interest_rows"] = _resolve_standalone_interest_names(
            lead_doc.name,
            identifiers,
        )
        scoped_interests = [
            _get_standalone_interest(lead_doc.name, name)
            for name in action_data["interest_rows"]
        ]
        scoped_units = [row.unit for row in scoped_interests if row.unit]
    else:
        if selected_units and not identifiers:
            identifiers = [
                row.name for row in (lead_doc.get("interested_in_units") or [])
                if row.name and row.get("unit") in selected_units and row.get("unit_interest_status") != "Lost Interest"
            ]
        action_data["interest_rows"] = identifiers
        scoped_units = [
            row.get("unit") for row in (lead_doc.get("interested_in_units") or [])
            if row.name in set(identifiers) and row.get("unit")
        ]
    if (
        action_data.get("action_type") in ("Showing", "Meeting", "Closing")
        and action_data.get("purpose") in ("Showing Confirmation", "Owner Meeting", "Developer Meeting", "Contract Signing")
        and not action_data.get("unit")
        and len(scoped_units) == 1
    ):
        action_data["unit"] = scoped_units[0]
    return action_data


def _bundle_idempotent_result(lead, client_request_id):
    if not client_request_id or not frappe.db.exists("DocType", "Lead Action Execution"):
        return None
    matches = frappe.get_all(
        "Lead Action Execution",
        filters={"lead": lead, "client_request_id": client_request_id},
        fields=["name"],
        limit_page_length=1,
    )
    if not matches:
        return None
    action = _get_action_doc(matches[0].name, lead)
    return {
        "action": _serialize_action(action),
        "event": action.get("event"),
        "context": get_lead_action_context(lead),
        "idempotent": True,
    }


@frappe.whitelist()
def submit_lead_action_bundle(lead, bundle, client_request_id=None):
    """Submit one complete action form as one Frappe transaction.

    No explicit commit is performed. Any validation exception rolls back action,
    Lead, interest, Event, Communication, and successor changes together.
    """
    bundle = _parse_json_object(bundle, _("Action bundle"))
    client_request_id = client_request_id or bundle.get("client_request_id")
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    _require_action_doctype()
    _lock_lead_workflow(lead)
    replay = _bundle_idempotent_result(lead, client_request_id)
    if replay:
        return replay

    action_data = _parse_json_object(bundle.get("action"), _("Action"))
    result_data = _parse_json_object(bundle.get("result"), _("Action result"))
    interest_data = _parse_json_object(bundle.get("interest"), _("Interest"))
    action_name = bundle.get("action_name") or action_data.get("name")
    execute_now = bool(_to_int(action_data.get("execute_now", 1)))

    action = None
    if action_name:
        action = _get_action_doc(action_name, lead)
    else:
        action_data = _hydrate_bundle_action_scope(lead_doc, action_data)
        if not action_data.get("action_type"):
            frappe.throw(_("Select an action before submitting."))
        planned = plan_lead_action(
            lead=lead,
            action_type=action_data.get("action_type"),
            purpose=action_data.get("purpose"),
            scheduled_start=action_data.get("scheduled_start"),
            notes=action_data.get("notes"),
            unit=action_data.get("unit"),
            interest_rows=action_data.get("interest_rows"),
            execute_now=1 if execute_now else 0,
        )
        action = _get_action_doc(planned["action"]["name"], lead)
        if not execute_now:
            action.client_request_id = client_request_id
            action.save(ignore_permissions=True)
            return {
                "action": _serialize_action(action),
                "event": action.get("event"),
                "execution_mode": "Scheduled",
                "context": get_lead_action_context(lead),
            }

    if action.workflow_status in ACTION_TERMINAL_STATUSES:
        frappe.throw(_("This action is already closed."))
    _assert_action_is_current(action)

    decision = result_data.get("decision") or result_data.get("outcome")
    interest_allowed = (
        action.action_type == "Add Interest"
        or (
            action.action_type == "Call"
            and action.purpose == "Initial Qualification"
            and result_data.get("contact_result") == "Answered"
            and result_data.get("qualification") == "Interested"
        )
        or (action.action_type == "Meeting" and action.purpose == "Explore Meeting" and result_data.get("outcome") == "Done")
        or (action.action_type == "Offer Decision" and decision == "Change Requirements")
    )

    matching_request = None
    if action.action_type == "Add Interest" and action.purpose == "Match Requested Unit":
        source_names = _action_interest_rows(action)
        if len(source_names) != 1:
            frappe.throw(_("Match Requested Unit must target exactly one requested Interest."))
        matching_request = _get_standalone_interest(lead, source_names[0])
        if matching_request.workflow_status != "Requested":
            frappe.throw(_("Only a Requested Interest can be matched to inventory."))
        if matching_request.category not in ("Resale", "Primary"):
            frappe.throw(_("Only Resale or Primary requested units can be matched to inventory."))
        if not interest_data:
            frappe.throw(_("Matching a request requires complete interest criteria and selected inventory units."))
        if interest_data.get("interest_category") != matching_request.category:
            frappe.throw(_("Keep the original request category; use Both search scope for mixed inventory."))
        if _to_int(interest_data.get("requested_unit")) or not _parse_json_list(interest_data.get("units")):
            frappe.throw(_("Select at least one actual inventory unit to fulfill this request."))

    applied_interest = None
    promoted_interest_names = []
    if interest_data:
        if not interest_allowed:
            frappe.throw(_("This action cannot change Lead interests."), frappe.PermissionError)
        if matching_request:
            interest_data["inventory_search_scope"] = interest_data.get("inventory_search_scope") or matching_request.get("inventory_search_scope") or matching_request.category
        applied_interest = _apply_interest_payload(
            lead_doc,
            interest_data,
            require_inventory_requirements=True,
            apply_status=not (
                action.action_type == "Offer Decision"
                and decision == "Change Requirements"
            ),
        )
        _save_workflow_doc(lead_doc)
        with interest_creation_context("workflow"):
            promoted_interest_names = _promote_applied_interest_rows(lead_doc, applied_interest)
        if matching_request:
            if not promoted_interest_names:
                frappe.throw(_("No standalone inventory Interest was created for the matched request."))
            _transition_standalone_interest(
                matching_request.name,
                "Fulfilled",
                action=action.name,
                outcome="Matched to Inventory",
                reason=_("Requested Interest fulfilled by selected inventory"),
            )
            matching_request.reload()
            matching_request.replacement_interest = promoted_interest_names[0]
            matching_request.save(ignore_permissions=True)
            for replacement_name in promoted_interest_names:
                replacement = _get_standalone_interest(lead, replacement_name)
                replacement.replaced_interest = matching_request.name
                replacement.save(ignore_permissions=True)
        lead_doc = _get_lead_doc(lead)

    if (
        action.action_type == "Call"
        and action.purpose == "Initial Qualification"
        and result_data.get("contact_result") == "Answered"
        and result_data.get("qualification") == "Interested"
        and not interest_data
    ):
        frappe.throw(_("Interested qualification requires complete interest details in the same form."))
    if action.action_type == "Add Interest" and not interest_data:
        frappe.throw(_("Add Interest requires complete interest details."))
    if action.action_type == "Offer Decision" and decision == "Change Requirements" and not interest_data:
        frappe.throw(_("Changing requirements requires a new complete interest form."))

    if action.action_type == "Add Interest":
        result_data["outcome"] = "Matched" if matching_request else "Added"
    if action.action_type == "Offer Decision":
        scoped = _action_interest_rows(action)
        if decision == "Select an Offer":
            selected_offer = result_data.get("selected_offer")
            selected_rows = _parse_json_list(result_data.get("interest_rows"))
            if selected_offer:
                selected_rows = [selected_offer]
            result_data["interest_rows"] = selected_rows
        elif decision == "Change Requirements":
            # The approved start-over path closes the complete current offer batch
            # but retains every row as immutable history.
            result_data["interest_rows"] = scoped

    next_action = _parse_json_object(result_data.get("next_action"), _("Next action"))
    if next_action:
        result_data["next_action"] = _hydrate_bundle_action_scope(
            lead_doc,
            next_action,
        )

    action.reload()
    completion = complete_lead_action(
        lead=lead,
        action_name=action.name,
        result_data=result_data,
        client_request_id=client_request_id,
        expected_modified=str(action.modified),
    )
    if applied_interest:
        completion["interest"] = {
            "category": applied_interest["category"],
            "requested_unit": applied_interest["requested_unit"],
            "rows": promoted_interest_names,
        }
    return completion
