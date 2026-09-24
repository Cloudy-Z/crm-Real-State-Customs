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
from real_estate_crm_customs.party_roles import normalize_party_role as _normalize_party_role


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LEAD_STATUS_NEW = "New"
LEAD_STATUS_FRESH = "Fresh Lead"
LEAD_STATUS_REQUESTED = "Requested"
LEAD_STATUS_OFFER_SENT = "Offer Sent"
LEAD_STATUS_NEGOTIATING = "Negotiating"
LEAD_STATUS_OFFER_SELECTED = "Offer Selected"


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
    if raw_party_role and raw_party_role not in ("Buyer", "Seller"):
        frappe.throw(_("Party Role must be Buyer or Seller."), frappe.ValidationError)
    doc.party_type = raw_party_role or "Buyer"


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
        LEAD_STATUS_OFFER_SENT: "blue",
        LEAD_STATUS_NEGOTIATING: "yellow",
        LEAD_STATUS_OFFER_SELECTED: "green",
    }
    _ensure_lead_status(status, color=color_map.get(status, "blue"))
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
        unit = frappe.db.get_value(
            "Real Estate Unit",
            unit_name,
            ["status", "owner_lead", "inventory_type"],
            as_dict=True,
        )
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
        if category == "Resale" and not unit.owner_lead:
            frappe.throw(_("Resale interest must link a seller-owned resale unit."))
        if category == "Primary" and unit.owner_lead:
            frappe.throw(_("Primary interest must link open developer inventory, not a seller-owned resale unit."))


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
    LEAD_STATUS_OFFER_SELECTED: 7,
}

REAL_ESTATE_FORM_DOCTYPES = {
    "Real Estate Unit",
    "Real Estate Project",
    "Property Developer",
    "Real Estate Destination",
    "Real Estate Unit Type",
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
        return

    previous = doc.get_doc_before_save()
    if not previous:
        return

    previous_role = _normalize_party_role(previous.get("party_type"))
    current_role = doc.get("party_type")

    if (
        previous_role == "Seller"
        and current_role != "Seller"
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
def _apply_interest_payload(
    doc,
    interest_data,
    require_inventory_requirements=False,
    apply_status=True,
):
    """Apply one interest draft to a Lead without saving, so callers can commit atomically."""
    if isinstance(interest_data, str):
        interest_data = json.loads(interest_data)
    interest_data = interest_data or {}
    category = interest_data.get("interest_category")
    allowed_categories = ("Resale", "Primary", "Brokerage Request", "International")
    if category not in allowed_categories:
        frappe.throw(_("Please select a valid interest category."))

    raw_units = interest_data.get("units") or []
    if isinstance(raw_units, str):
        raw_units = [raw_units]
    units = list(dict.fromkeys(filter(None, raw_units)))
    requested_unit = bool(_to_int(interest_data.get("requested_unit")))

    if category in ("Resale", "Primary"):
        preferred_destination = interest_data.get("preferred_destination")
        if not preferred_destination and interest_data.get("preferred_area"):
            preferred_destination = _resolve_destination_alias(
                location=interest_data.get("preferred_area")
            )
            if preferred_destination:
                interest_data["preferred_destination"] = preferred_destination
        _validate_active_destination(preferred_destination)
        if requested_unit and units:
            frappe.throw(_("Choose matched inventory units or Requested Unit, not both."))
        if requested_unit and not preferred_destination:
            frappe.throw(_("Requested inventory requires a canonical Destination."))
        if not requested_unit:
            _validate_inventory_interest(category, units)
        if require_inventory_requirements:
            missing = [
                label
                for fieldname, label in (
                    ("preferred_destination", _("Preferred Destination")),
                    ("preferred_unit_type", _("Preferred Unit Type")),
                    ("buyer_budget", _("Maximum Budget")),
                )
                if not (
                    interest_data.get(fieldname)
                )
            ]
            if missing:
                frappe.throw(_("Complete the interest requirements: {0}.").format(", ".join(missing)))
    if category == "Brokerage Request" and not interest_data.get("request_notes"):
        frappe.throw(_("Brokerage requirements are mandatory."))
    if category == "International":
        if not interest_data.get("international_type"):
            frappe.throw(_("International category is mandatory."))
        if not interest_data.get("international_country"):
            frappe.throw(_("Country is mandatory for International requests."))

    doc.interest_status = "Interested"
    for field in (
        "area_unit",
        "preferred_unit_type",
        "preferred_destination",
        "preferred_area",
        "preferred_developer",
        "preferred_compound",
        "preferred_finishing_type",
        "preferred_delivery_time",
        "buyer_budget",
    ):
        if field in interest_data:
            doc.set(field, interest_data[field])

    interest_values = []
    existing_units = set()
    existing_interest_names = []
    if _standalone_interests_available() and units:
        existing_interests = frappe.get_all(
            "Lead Interest",
            filters={
                "lead": doc.name,
                "record_type": "Inventory Unit",
                "category": category,
                "unit": ["in", units],
            },
            fields=["name", "unit"],
        )
        existing_units = {row.unit for row in existing_interests}
        existing_interest_names = [row.name for row in existing_interests]
    for unit in units:
        if unit in existing_units:
            continue
        interest_values.append({
            "record_type": "Inventory Unit",
            "category": category,
            "unit": unit,
            "workflow_status": "Matched",
        })

    if requested_unit or category == "Brokerage Request":
        request_note = interest_data.get("request_notes") or _(
            "Requested {0} unit — no suitable inventory match."
        ).format(category)
        interest_values.append({
            "record_type": "Request",
            "category": category,
            "workflow_status": "Requested",
            "request_notes": request_note,
            "request_status": "Open",
            "requested_destination": interest_data.get("preferred_destination"),
            "requested_area": interest_data.get("preferred_area"),
            "requested_unit_type": interest_data.get("preferred_unit_type"),
            "requested_budget": interest_data.get("buyer_budget"),
            "requested_project": interest_data.get("preferred_compound"),
            "requested_developer": interest_data.get("preferred_developer"),
            "requested_finishing_type": interest_data.get("preferred_finishing_type"),
            "requested_delivery_time": interest_data.get("preferred_delivery_time"),
        })

    if category == "International":
        interest_values.append({
            "record_type": "International",
            "category": category,
            "workflow_status": "Requested",
            "international_type": interest_data.get("international_type"),
            "international_country": interest_data.get("international_country"),
            "international_details": interest_data.get("international_details"),
        })

    existing_primary = (
        frappe.db.exists(
            "Lead Interest",
            {"lead": doc.name, "category": "Primary", "is_active": 1},
        )
        if _standalone_interests_available()
        else None
    )
    doc.is_primary_buyer = int(category == "Primary" or bool(existing_primary))

    creates_request = requested_unit or category in ("Brokerage Request", "International")
    if apply_status and creates_request and doc.status in (LEAD_STATUS_NEW, LEAD_STATUS_FRESH, LEAD_STATUS_OFFER_SENT):
        doc.previous_status = doc.status
        _set_lead_status(doc, LEAD_STATUS_REQUESTED, _("Interest request recorded: {0}").format(category))

    return {
        "category": category,
        "requested_unit": requested_unit,
        "interest_values": interest_values,
        "existing_interest_names": existing_interest_names,
    }


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
    """Schedule next action: Call, Meeting, Showing, or Send Offer.
    Showing records on Unit child table and creates event on Seller Lead."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)

    if action_type not in ("Call", "Meeting", "Showing", "Send Offer"):
        frappe.throw(_("Invalid action type: {0}").format(action_type))
    if not starts_on:
        frappe.throw(_("Date/time is required for scheduling."))

    event_subject = subject or _("{0} — {1}").format(action_type, doc.get("lead_name") or lead)

    if action_type == "Send Offer":
        event_name = _create_lead_event(
            lead=lead,
            subject=event_subject,
            starts_on=starts_on,
            meeting_type=action_type,
            notes=notes,
        )
        doc.previous_status = doc.status
        _set_lead_status(doc, LEAD_STATUS_OFFER_SENT, _("Send Offer scheduled"))
        _add_lead_comment(doc, _("Next action: Send Offer scheduled for {0}. Notes: {1}").format(starts_on, notes or ""))
        _save_workflow_doc(doc)
        return {
            "action_type": action_type,
            "event": event_name,
            "scheduled": True,
            "status": doc.status,
        }

    event_name = _create_lead_event(lead=lead, subject=event_subject, starts_on=starts_on, meeting_type=action_type, notes=notes)
    result = {"action_type": action_type, "event": event_name, "scheduled": True}
    status_changed = False
    if doc.get("interest_status") == "Interested":
        target_status = None
        if action_type == "Meeting" and doc.status in (LEAD_STATUS_NEW, LEAD_STATUS_FRESH):
            target_status = LEAD_STATUS_REQUESTED
        elif action_type == "Showing" and doc.status != LEAD_STATUS_OFFER_SELECTED:
            target_status = LEAD_STATUS_OFFER_SELECTED
        if target_status:
            doc.previous_status = doc.status
            status_changed = _set_lead_status(
                doc,
                target_status,
                _("{0} scheduled").format(action_type),
            )

    if action_type == "Showing" and target_unit:
        if not frappe.db.exists("Real Estate Unit", target_unit):
            frappe.throw(_("Unit {0} not found.").format(target_unit))
        unit_doc = frappe.get_doc("Real Estate Unit", target_unit)
        agent = doc.get("lead_owner") or frappe.session.user
        unit_doc.append("scheduled_showings", {
            "showing_date": starts_on,
            "buyer_lead": lead,
            "buyer_name": doc.get("lead_name"),
            "agent": agent,
            "status": "Scheduled",
        })
        unit_doc.save(ignore_permissions=True)
        result["unit_showing_recorded"] = True

        seller_lead = unit_doc.get("owner_lead")
        if seller_lead and frappe.db.exists("CRM Lead", seller_lead):
            seller_event = _create_lead_event(
                lead=seller_lead,
                subject=_("Showing scheduled on your unit {0}").format(target_unit),
                starts_on=starts_on,
                meeting_type="Showing",
                notes=_("Buyer: {0}, Agent: {1}").format(doc.get("lead_name") or lead, agent),
            )
            result["seller_event"] = seller_event

        if doc.status == LEAD_STATUS_NEGOTIATING:
            doc.previous_status = doc.status
            status_changed = _set_lead_status(
                doc,
                LEAD_STATUS_OFFER_SELECTED,
                _("Showing scheduled after negotiation"),
            ) or status_changed

    if status_changed:
        _add_lead_comment(doc, _("Status changed automatically after scheduling {0}.").format(action_type))
        _save_workflow_doc(doc)
        result["status"] = doc.status

    return result


# ---------------------------------------------------------------------------
# 6. Meeting/Showing Result Logging
# ---------------------------------------------------------------------------
@frappe.whitelist()
def log_meeting_result(lead, event_name, result, result_note=None, reschedule_to=None, target_unit=None):
    """Log meeting/showing result: Done, Cancelled, or Rescheduled."""
    doc = _get_lead_doc(lead)

    if result not in ("Done", "Cancelled", "Rescheduled"):
        frappe.throw(_("Invalid result. Must be Done, Cancelled, or Rescheduled."))

    if event_name and frappe.db.exists("Event", event_name):
        event_doc = frappe.get_doc("Event", event_name)
        if result_note:
            event_doc.description = (event_doc.description or "") + f"\n\nResult: {result}\n{result_note}"
        if result == "Cancelled":
            event_doc.status = "Cancelled"
        else:
            event_doc.status = "Closed"
        event_doc.save(ignore_permissions=True)

    if result == "Done" and result_note:
        if frappe.db.exists("DocType", "FCRM Note"):
            frappe.get_doc({
                "doctype": "FCRM Note",
                "title": _("Meeting Result \u2014 {0}").format(doc.get("lead_name") or lead),
                "content": result_note,
                "reference_doctype": "CRM Lead",
                "reference_docname": lead,
            }).insert(ignore_permissions=True)

    if target_unit and frappe.db.exists("Real Estate Unit", target_unit):
        unit_doc = frappe.get_doc("Real Estate Unit", target_unit)
        for row in unit_doc.get("scheduled_showings") or []:
            if row.buyer_lead == lead and row.status == "Scheduled":
                row.status = result
                if result_note:
                    row.result_notes = result_note
                break
        unit_doc.save(ignore_permissions=True)

    new_event = None
    if result == "Rescheduled" and reschedule_to:
        new_event = _create_lead_event(
            lead=lead,
            subject=_("Rescheduled: {0}").format(doc.get("lead_name") or lead),
            starts_on=reschedule_to,
            meeting_type="Meeting",
        )
        if target_unit and frappe.db.exists("Real Estate Unit", target_unit):
            unit_doc = frappe.get_doc("Real Estate Unit", target_unit)
            unit_doc.append("scheduled_showings", {
                "showing_date": reschedule_to,
                "buyer_lead": lead,
                "buyer_name": doc.get("lead_name"),
                "agent": doc.get("lead_owner") or frappe.session.user,
                "status": "Scheduled",
            })
            unit_doc.save(ignore_permissions=True)

    _add_lead_comment(doc, _("Meeting/Showing result: {0}. Note: {1}").format(result, result_note or "—"))
    return {"result": result, "new_event": new_event}


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
    project,
    unit_number,
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
):
    if not frappe.db.exists("CRM Lead", owner_lead):
        frappe.throw(_("Lead {0} was not found.").format(owner_lead), frappe.DoesNotExistError)
    lead = frappe.get_doc("CRM Lead", owner_lead)
    _assert_write_permission(lead)
    if lead.get("party_type") != "Seller":
        frappe.throw(_("Only Seller leads can list resale units."), frappe.ValidationError)
    if not project:
        frappe.throw(_("Compound is required."), frappe.ValidationError)
    if not unit_number:
        frappe.throw(_("Unit Number is required."), frappe.ValidationError)
    if not physical_unit_type:
        frappe.throw(_("A physical Unit Type is required."), frappe.ValidationError)
    if not finishing_type or not delivery_date:
        frappe.throw(_("Finishing Type and Delivery Date are required."), frappe.ValidationError)
    if price not in (None, "") and all(value in (None, "") for value in (paid, over_price, remaining)):
        frappe.throw(
            _("Legacy Asking Price is ambiguous. Enter Paid, Over Price, and Remaining so Total Gross can be calculated."),
            frappe.ValidationError,
        )
    unit = frappe.get_doc({
        "doctype": "Real Estate Unit",
        "project": project,
        "unit_number": unit_number,
        "inventory_type": "Resale",
        "physical_unit_type": physical_unit_type,
        "floor": floor,
        "bua": bua,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "finishing_type": finishing_type,
        "delivery_date": delivery_date,
        "status": "Available",
        "paid": paid,
        "over_price": over_price,
        "over_is_gross": _to_int(over_is_gross),
        "remaining": remaining,
        # Keep the old argument as an explicitly labelled compatibility value.
        "price": price,
        "owner_lead": owner_lead,
    })
    unit.insert()
    return unit.as_dict()


@frappe.whitelist()
def add_interest_request(lead, request_notes, request_status="Open"):
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
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)
    if not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit), frappe.DoesNotExistError)
    lead_doc = frappe.get_doc("CRM Lead", lead)
    _assert_write_permission(lead_doc)
    if lead_doc.get("party_type") != "Seller":
        frappe.throw(_("Only Seller leads can be assigned property units."), frappe.ValidationError)
    unit_doc = frappe.get_doc("Real Estate Unit", unit)
    if unit_doc.get("status") != "Available":
        frappe.throw(_("Only Available units can be assigned to a seller lead."), frappe.ValidationError)
    if unit_doc.get("owner_lead") and unit_doc.get("owner_lead") != lead:
        frappe.throw(_("Unit {0} is already assigned to seller lead {1}.").format(unit, unit_doc.get("owner_lead")), frappe.ValidationError)
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
    unit_doc.save()
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
            row.relationship = _("Seller Unit")
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
            "requested_budget": interest_row.get("requested_budget"),
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


def _effective_unit_price(unit):
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
):
    """Return category-valid inventory using canonical Compound, Destination, and Total Gross fields."""
    if interest_category not in ("Resale", "Primary"):
        frappe.throw(_("Inventory selection is available only for Resale or Primary interests."))
    if lead:
        lead_doc = _get_lead_doc(lead)
        _validate_buyer_lead(lead_doc)
    else:
        lead_doc = None

    destination = _resolve_destination_alias(destination, location)
    physical_unit_type = physical_unit_type or unit_type
    filters = {"status": "Available", "inventory_type": interest_category}
    if project:
        filters["project"] = project
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
        "status",
        "total_gross",
        "price",
        "owner_lead",
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
            physical_unit_type,
            finishing_type,
            min_price,
            max_price,
        )
    )
    if allowed_existing and not has_active_filters and not any(unit.name == allowed_existing for unit in units):
        existing_unit = frappe.db.get_value("Real Estate Unit", allowed_existing, fields, as_dict=True)
        if existing_unit and existing_unit.get("inventory_type") == interest_category:
            units.insert(0, existing_unit)

    already_linked = set()
    if lead_doc:
        already_linked.update(
            row.unit
            for row in lead_doc.get("interested_in_units") or []
            if row.unit and row.unit != allowed_existing
        )
        if _standalone_interests_available():
            already_linked.update(
                row.unit
                for row in _get_standalone_interests(lead_doc.name, include_closed=False)
                if row.unit and row.unit != allowed_existing
            )
    units = [unit for unit in units if unit.name not in already_linked]

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
        unit.unit_type = unit.physical_unit_type or unit.unit_type
        unit.effective_price, unit.price_source = _effective_unit_price(unit)
        if minimum is not None and (unit.effective_price is None or unit.effective_price < minimum):
            continue
        if maximum is not None and (unit.effective_price is None or unit.effective_price > maximum):
            continue
        filtered_units.append(unit)

    result_limit = max(10, min(_to_int(page_length) or 100, 500))
    return filtered_units[:result_limit]


@frappe.whitelist()
def get_property_match_filter_options(interest_category="Resale"):
    """Return inventory-backed physical Unit Type and finishing options."""
    if interest_category not in ("Resale", "Primary"):
        frappe.throw(_("Inventory filters are available only for Resale or Primary interests."))
    unit_filters = {"status": "Available", "inventory_type": interest_category}
    destinations = frappe.get_all(
        "Real Estate Destination",
        filters={"is_active": 1},
        fields=["name", "destination_name"],
        order_by="destination_name asc",
        limit_page_length=500,
    )
    unit_types = frappe.get_all(
        "Real Estate Unit",
        filters=unit_filters,
        pluck="physical_unit_type",
        limit_page_length=500,
    )
    finishing_types = frappe.get_all(
        "Real Estate Unit",
        filters=unit_filters,
        pluck="finishing_type",
        limit_page_length=500,
    )
    return {
        "destinations": [dict(row) for row in destinations],
        "locations": [row.destination_name for row in destinations],
        "unit_types": sorted(set(filter(None, unit_types))),
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
    """Build criteria from one scoped Interest, then fill only missing fields from Lead defaults."""
    profile = frappe._dict(
        {
            "destination": lead_doc.get("preferred_destination"),
            "location": lead_doc.get("preferred_destination") or lead_doc.get("preferred_area"),
            "unit_type": lead_doc.get("preferred_unit_type"),
            "developer": lead_doc.get("preferred_developer"),
            "project": lead_doc.get("preferred_compound"),
            "finishing_type": lead_doc.get("preferred_finishing_type"),
            "budget_min": None,
            "budget": lead_doc.get("buyer_budget"),
            "source": "Lead Defaults",
        }
    )

    scoped_interest = None
    if interest and _standalone_interests_available():
        scoped_interest = _get_standalone_interest(lead_doc.name, interest)
        if interest_category and scoped_interest.category != interest_category:
            frappe.throw(
                _("Interest {0} belongs to category {1}, not {2}.").format(
                    scoped_interest.name,
                    scoped_interest.category,
                    interest_category,
                ),
                frappe.ValidationError,
            )

    if scoped_interest:
        scoped_values = {
            "destination": scoped_interest.get("requested_destination"),
            "location": scoped_interest.get("requested_destination") or scoped_interest.get("requested_area"),
            "unit_type": scoped_interest.get("requested_unit_type"),
            "developer": scoped_interest.get("requested_developer"),
            "project": scoped_interest.get("requested_project"),
            "finishing_type": scoped_interest.get("requested_finishing_type"),
            "budget": scoped_interest.get("requested_budget"),
        }
        if scoped_interest.unit:
            reference_unit = frappe.db.get_value(
                "Real Estate Unit",
                scoped_interest.unit,
                [
                    "name",
                    "project",
                    "destination",
                    "developer",
                    "physical_unit_type",
                    "unit_type",
                    "finishing_type",
                    "total_gross",
                    "price",
                ],
                as_dict=True,
            ) or {}
            effective_price, _ = _effective_unit_price(reference_unit)
            unit_values = {
                "destination": reference_unit.get("destination"),
                "location": reference_unit.get("destination"),
                "unit_type": reference_unit.get("physical_unit_type") or reference_unit.get("unit_type"),
                "developer": reference_unit.get("developer"),
                "project": reference_unit.get("project"),
                "finishing_type": reference_unit.get("finishing_type"),
                "budget": effective_price,
            }
            scoped_values = {
                key: scoped_values.get(key) if scoped_values.get(key) not in (None, "") else value
                for key, value in unit_values.items()
            }
        for fieldname, value in scoped_values.items():
            if value not in (None, ""):
                profile[fieldname] = value
        profile.source = "Scoped Lead Interest"
        profile.interest = scoped_interest.name

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
    destination = _resolve_destination_alias(destination, location)
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
        if value not in (None, ""):
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
        ("Compound", profile.get("project"), unit.get("project"), 16),
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
            price_value = float(unit.get("effective_price") or 0)
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
            reasons.append(_("Within preferred price range"))
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
def get_smart_matched_units(
    lead,
    interest_category="Resale",
    interest=None,
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
    strict_filters=0,
):
    """Return eligible units ranked against one scoped Interest or new-interest draft."""
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    strict_filters = _to_int(strict_filters)
    resolved_type = physical_unit_type or unit_type
    units = get_available_units_for_selection(
        lead=lead,
        interest_category=interest_category,
        search=search,
        project=project if strict_filters else None,
        developer=developer if strict_filters else None,
        destination=destination if strict_filters else None,
        location=location if strict_filters else None,
        physical_unit_type=resolved_type if strict_filters else None,
        finishing_type=finishing_type if strict_filters else None,
        min_price=min_price if strict_filters else None,
        max_price=max_price if strict_filters else None,
        include_unit=include_unit,
        page_length=500,
    )
    profile = _apply_match_profile_overrides(
        _lead_property_match_profile(lead_doc, interest_category, interest=interest),
        project=project,
        developer=developer,
        destination=destination,
        location=location,
        unit_type=resolved_type,
        min_price=min_price,
        max_price=max_price,
    )
    if finishing_type not in (None, ""):
        profile.finishing_type = finishing_type
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
    for unit in units:
        score, level, reasons, gaps = _score_property_match(unit, profile)
        unit.match_score = score
        unit.match_level = level
        unit.match_reasons = reasons
        unit.match_gaps = gaps

    units.sort(
        key=lambda unit: (
            unit.get("match_score") is not None,
            unit.get("match_score") or 0,
            unit.get("modified") or "",
        ),
        reverse=True,
    )
    result_limit = max(10, min(_to_int(page_length) or 100, 200))
    return {
        "units": units[:result_limit],
        "profile": profile,
        "smart_match_active": bool(profile.get("criteria_count")),
        "match_mode": "Exact Filters" if strict_filters else "Smart Match",
    }


# ---------------------------------------------------------------------------
# 9. Send Offer — Mark units as sent and prepare WhatsApp message
# ---------------------------------------------------------------------------
@frappe.whitelist()
def send_offer_to_lead(lead, unit_rows):
    """Mark selected interest table rows as offer_sent=1, update status to Offer Sent,
    and return the WhatsApp URL with unit details for the agent to send."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)

    if isinstance(unit_rows, str):
        unit_rows = json.loads(unit_rows)

    if not unit_rows:
        frappe.throw(_("Please select at least one unit to send as offer."))

    if _standalone_interests_available():
        identifiers = []
        for value in unit_rows:
            if frappe.db.exists("Lead Interest", value):
                identifiers.append(value)
                continue
            by_unit = frappe.db.get_value(
                "Lead Interest",
                {"lead": lead, "unit": value, "workflow_status": "Matched"},
                "name",
            )
            identifiers.append(by_unit or value)
        names = _resolve_standalone_interest_names(lead, identifiers, required=True)
        _update_unit_outcomes(doc, names, "Sent")
        _reconcile_standalone_rollup(lead, _("Offer sent through compatibility endpoint"))
        doc.reload()
        return {
            "status": doc.status,
            "sent_count": len(names),
            "offer_sent_total": len([
                row for row in _get_standalone_interests(lead, include_closed=True)
                if row.workflow_status in ("Offer Sent", "Offer Viewed", "Offer Accepted", "Negotiating", "Showing Scheduled", "Shown - Interested", "Shown - Considering")
            ]),
            "interests": names,
        }

    sent_count = 0
    for row_name in unit_rows:
        for row in doc.get("interested_in_units") or []:
            if row.name == row_name or row.unit == row_name:
                row.offer_sent = 1
                row.offer_sent_at = now_datetime()
                row.proposal_status = "Sent"
                sent_count += 1
    if sent_count == 0:
        frappe.throw(_("No matching rows found to mark as sent."))
    _set_lead_status(doc, LEAD_STATUS_OFFER_SENT, _("Offer sent"))
    _save_workflow_doc(doc)
    return {"status": doc.status, "sent_count": sent_count}



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
    """Agent marks that the lead has accepted an offer and is negotiating on a specific unit."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)

    if _standalone_interests_available():
        if not unit:
            frappe.throw(_("Select the offered unit whose interest is entering negotiation."))
        interest_name = frappe.db.get_value(
            "Lead Interest",
            {"lead": lead, "unit": unit, "is_active": 1},
            "name",
        )
        if not interest_name:
            frappe.throw(_("No active Lead Interest was found for unit {0}.").format(unit))
        _transition_standalone_interest(
            interest_name,
            "Negotiating",
            outcome="Negotiation Started",
            reason=_("Compatibility endpoint"),
            force=True,
        )
        target = _reconcile_standalone_rollup(lead, _("Negotiation started"))
        return {"status": target, "interest": interest_name}

    _set_lead_status(doc, LEAD_STATUS_NEGOTIATING, _("Offer accepted for negotiation"))
    _save_workflow_doc(doc)
    return {"status": doc.status}


# ---------------------------------------------------------------------------
# 12. Add Outsource Interest Record
# ---------------------------------------------------------------------------
@frappe.whitelist()
def add_outsource_interest(lead, company_name, broker_name=None, broker_number=None, unit_details=None):
    """Add an authoritative standalone outsource Interest."""
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
    """Mark a specific interest row as Lost Interest (without deleting it)."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)

    if _standalone_interests_available():
        interest = _get_standalone_interest(lead, row_name)
        _transition_standalone_interest(
            interest.name,
            "Rejected",
            outcome="Lost Interest",
            reason=_("Marked lost through compatibility endpoint"),
            force=True,
        )
        target = _reconcile_standalone_rollup(lead, _("Interest marked lost"))
        return {"marked": True, "interest": interest.name, "status": target}

    row = _find_interest_row(doc, row_name)
    row.unit_interest_status = "Lost Interest"
    doc.save(ignore_permissions=True)
    return {"marked": True}


# ---------------------------------------------------------------------------
# 14. Interest Record Editing and Manager-approved Deletion
# ---------------------------------------------------------------------------
@frappe.whitelist()
def update_interest_record(lead, row_name, interest_data):
    """Edit one existing interest row without mutating unrelated interests."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    standalone_existing = (
        _get_standalone_interest(lead, row_name)
        if _standalone_interests_available()
        else None
    )
    if standalone_existing and standalone_existing.workflow_status != "Requested":
        frappe.throw(
            _("Only unmatched Requested interests may be edited directly. Use the row actions for linked or offered units."),
            frappe.PermissionError,
        )
    if isinstance(interest_data, str):
        interest_data = json.loads(interest_data)
    interest_data = interest_data or {}

    if standalone_existing:
        category = interest_data.get("interest_category") or standalone_existing.category
        if category != standalone_existing.category:
            frappe.throw(
                _("An existing Interest category cannot be changed. Supersede it and create a new Interest instead."),
                frappe.PermissionError,
            )
        if category in ("Resale", "Primary"):
            requested_destination = (
                interest_data.get("preferred_destination")
                or standalone_existing.get("requested_destination")
            )
            requested_area = (
                interest_data.get("preferred_area")
                or standalone_existing.get("requested_area")
            )
            if not requested_destination and requested_area:
                requested_destination = _resolve_destination_alias(location=requested_area)
            _validate_active_destination(requested_destination)
            requested_type = (
                interest_data.get("preferred_unit_type")
                or standalone_existing.get("requested_unit_type")
            )
            requested_budget = (
                interest_data.get("buyer_budget")
                or standalone_existing.get("requested_budget")
            )
            if not requested_destination or not requested_type or not requested_budget:
                frappe.throw(_("Requested destination, unit type, and budget are mandatory."))
            standalone_existing.request_status = "Open"
            standalone_existing.requested_destination = requested_destination
            standalone_existing.requested_area = requested_area
            standalone_existing.requested_unit_type = requested_type
            standalone_existing.requested_budget = requested_budget
            standalone_existing.requested_project = (
                interest_data.get("preferred_compound")
                or standalone_existing.get("requested_project")
            )
            standalone_existing.requested_developer = (
                interest_data.get("preferred_developer")
                or standalone_existing.get("requested_developer")
            )
            standalone_existing.requested_finishing_type = (
                interest_data.get("preferred_finishing_type")
                or standalone_existing.get("requested_finishing_type")
            )
            standalone_existing.requested_delivery_time = (
                interest_data.get("preferred_delivery_time")
                or standalone_existing.get("requested_delivery_time")
            )
        elif category == "Brokerage Request":
            notes = interest_data.get("request_notes")
            if not notes:
                frappe.throw(_("Brokerage requirements are mandatory."))
            standalone_existing.request_notes = notes
            standalone_existing.request_status = "Open"
        elif category == "International":
            international_type = interest_data.get("international_type")
            country = interest_data.get("international_country")
            if not international_type or not country:
                frappe.throw(_("International category and country are mandatory."))
            standalone_existing.international_type = international_type
            standalone_existing.international_country = country
            standalone_existing.international_details = interest_data.get("international_details")
        else:
            frappe.throw(_("This Interest type cannot be edited through the request editor."))

        for field in (
            "preferred_destination",
            "preferred_area",
            "preferred_unit_type",
            "preferred_developer",
            "preferred_compound",
            "preferred_finishing_type",
            "preferred_delivery_time",
            "buyer_budget",
        ):
            if field in interest_data:
                doc.set(field, interest_data.get(field))
        _add_lead_comment(doc, _("Interest record updated: {0}").format(row_name))
        doc.save(ignore_permissions=True)
        standalone_existing.save(ignore_permissions=True)
        _mirror_standalone_interest(standalone_existing)
        _reconcile_standalone_rollup(doc.name, _("Interest details updated"))
        standalone_existing.reload()
        return {
            "updated": True,
            "row": standalone_existing.as_dict(),
            "interest": standalone_existing.name,
        }

    row = _find_interest_row(doc, row_name)

    category = interest_data.get("interest_category") or row.get("interest_category")
    if category not in ("Resale", "Primary", "Brokerage Request", "International", "Outsource"):
        frappe.throw(_("Please select a valid interest category."))
    if standalone_existing and category != standalone_existing.category:
        frappe.throw(
            _("An existing Interest category cannot be changed. Supersede it and create a new Interest instead."),
            frappe.PermissionError,
        )

    is_unmatched_inventory_request = (
        category in ("Resale", "Primary")
        and (standalone_existing.record_type if standalone_existing else row.get("interest_record_type")) == "Request"
    )
    if is_unmatched_inventory_request:
        requested_destination = (
            interest_data.get("preferred_destination")
            or (standalone_existing.get("requested_destination") if standalone_existing else row.get("requested_destination"))
        )
        requested_area = interest_data.get("preferred_area") or row.get("requested_area")
        if not requested_destination and requested_area:
            requested_destination = _resolve_destination_alias(location=requested_area)
        _validate_active_destination(requested_destination)
        requested_type = interest_data.get("preferred_unit_type") or row.get("requested_unit_type")
        requested_budget = interest_data.get("buyer_budget") or row.get("requested_budget")
        if not (requested_destination or requested_area) or not requested_type or not requested_budget:
            frappe.throw(_("Requested destination, unit type, and budget are mandatory."))
        row.interest_record_type = "Request"
        row.interest_category = category
        row.unit = None
        row.request_status = "Open"
        row.requested_destination = requested_destination
        row.requested_area = requested_area
        row.requested_unit_type = requested_type
        row.requested_budget = requested_budget
        row.requested_project = interest_data.get("preferred_compound") or row.get("requested_project")
        row.requested_developer = interest_data.get("preferred_developer") or row.get("requested_developer")
        row.requested_finishing_type = interest_data.get("preferred_finishing_type") or row.get("requested_finishing_type")
        row.requested_delivery_time = interest_data.get("preferred_delivery_time") or row.get("requested_delivery_time")
    elif category in ("Resale", "Primary"):
        unit = interest_data.get("unit") or row.get("unit")
        current_unit = row.get("unit")
        _validate_inventory_interest(
            category,
            [unit] if unit else [],
            allow_unavailable_units=[current_unit] if unit == current_unit else None,
        )
        duplicate = any(
            other.name != row.name
            and other.get("unit") == unit
            and other.get("interest_category") == category
            for other in (doc.get("interested_in_units") or [])
        )
        if duplicate:
            frappe.throw(_("This unit is already recorded under the selected interest category."))
        row.interest_record_type = "Inventory Unit"
        row.interest_category = category
        row.unit = unit
        row.request_notes = None
        row.request_status = None
        row.international_type = None
        row.international_country = None
        row.international_details = None
    elif category == "Brokerage Request":
        notes = interest_data.get("request_notes")
        if not notes:
            frappe.throw(_("Brokerage requirements are mandatory."))
        row.interest_record_type = "Request"
        row.interest_category = category
        row.unit = None
        row.request_notes = notes
        row.request_status = interest_data.get("request_status") or row.get("request_status") or "Open"
        row.international_type = None
        row.international_country = None
        row.international_details = None
    elif category == "International":
        international_type = interest_data.get("international_type")
        country = interest_data.get("international_country")
        if not international_type or not country:
            frappe.throw(_("International category and country are mandatory."))
        row.interest_record_type = "International"
        row.interest_category = category
        row.unit = None
        row.request_notes = None
        row.request_status = None
        row.international_type = international_type
        row.international_country = country
        row.international_details = interest_data.get("international_details")

    row.unit_interest_status = interest_data.get("unit_interest_status") or row.get("unit_interest_status") or "Active"
    for field in (
        "preferred_destination",
        "preferred_area",
        "preferred_unit_type",
        "preferred_developer",
        "preferred_compound",
        "preferred_finishing_type",
        "preferred_delivery_time",
        "buyer_budget",
    ):
        if field in interest_data:
            doc.set(field, interest_data.get(field))
    doc.is_primary_buyer = int(any(
        other.get("interest_category") == "Primary"
        and other.get("unit_interest_status") != "Lost Interest"
        for other in (doc.get("interested_in_units") or [])
    ))
    _add_lead_comment(doc, _("Interest record updated: {0}").format(row_name))
    doc.save(ignore_permissions=True)
    return {
        "updated": True,
        "row": row.as_dict(),
        "interest": row.name,
    }


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
ACTION_TYPES = ("Call", "Add Interest", "Meeting", "Showing", "Send Offer", "Offer Decision", "Negotiation Follow-up")
ACTION_PURPOSES = (
    "Initial Qualification",
    "Requirements Discovery",
    "Meeting Confirmation",
    "Showing Confirmation",
    "Offer Follow-up",
    "Negotiation Follow-up",
    "General Follow-up",
    "General Meeting",
    "Explore Meeting",
    "Meeting with Owner",
    "Discovery Meeting",
    "Offer Review",
    "Offer Decision",
    "Negotiation Meeting",
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
    if action_type not in ACTION_TYPES:
        frappe.throw(_("Invalid action type: {0}").format(action_type))
    if purpose and purpose not in ACTION_PURPOSES:
        frappe.throw(_("Invalid action purpose: {0}").format(purpose))
    if action_type == "Showing" and purpose and purpose not in ("Showing Confirmation", "General Follow-up"):
        frappe.throw(_("Showing actions must use Showing Confirmation or General Follow-up purpose."))
    if action_type == "Meeting" and purpose and purpose not in (
        "General Meeting",
        "Explore Meeting",
        "Meeting with Owner",
        "Discovery Meeting",
        "Offer Review",
        "Negotiation Meeting",
    ):
        frappe.throw(_("Invalid meeting purpose."))
    if action_type == "Offer Decision" and purpose and purpose != "Offer Decision":
        frappe.throw(_("Offer Decision actions must use the Offer Decision purpose."))
    if action_type == "Negotiation Follow-up" and purpose and purpose != "Negotiation Follow-up":
        frappe.throw(_("Negotiation actions must use Negotiation Follow-up purpose."))


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
            "General Meeting": "meeting.general.v2",
            "Explore Meeting": "meeting.explore.v2",
            "Meeting with Owner": "meeting.owner.v2",
        }
        return meeting_schema.get(action.purpose, "meeting.result.v1")
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
    if lead_doc.get("party_type") == "Seller":
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
            "purpose": "General Meeting",
            "label": _("General meeting"),
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
    """Return actions legal for one independent Lead Interest row."""
    if not interest.is_active:
        return []
    scoped = {
        "interest_row_names": [interest.name],
        "interest_names": [interest.name],
        "requires_interest_rows": True,
        "scope_type": "Interest",
        "unit": interest.unit,
        "requires_unit": bool(interest.unit),
    }
    actions = []
    if interest.workflow_status == "Requested":
        if interest.category in ("Resale", "Primary"):
            actions.append({
                **scoped,
                "action_type": "Add Interest",
                "purpose": "Match Requested Unit",
                "label": _("Run Smart Match and link inventory"),
            })
        actions.extend([
            {**scoped, "action_type": "Call", "purpose": "Requirements Discovery", "label": _("Follow up this request")},
            {**scoped, "action_type": "Meeting", "purpose": "Explore Meeting", "label": _("Explore this requirement")},
        ])
    if interest.workflow_status == "Matched":
        actions.extend([
            {**scoped, "action_type": "Call", "purpose": "General Follow-up", "label": _("Discuss this matched unit")},
            {**scoped, "action_type": "Meeting", "purpose": "Explore Meeting", "label": _("Explore this matched unit")},
        ])
    if interest.workflow_status in ("Offer Sent", "Offer Viewed"):
        actions.extend([
            {**scoped, "action_type": "Offer Decision", "purpose": "Offer Decision", "label": _("Record this offer decision")},
            {**scoped, "action_type": "Call", "purpose": "Offer Follow-up", "label": _("Follow up this offer")},
        ])
    if interest.workflow_status in ("Offer Accepted", "Negotiating", "Shown - Considering", "Shown - Interested"):
        actions.extend([
            {**scoped, "action_type": "Negotiation Follow-up", "purpose": "Negotiation Follow-up", "label": _("Record negotiation for this unit")},
            {**scoped, "action_type": "Showing", "purpose": "Showing Confirmation", "label": _("Schedule or record showing for this unit")},
        ])
    if (
        interest.category == "Resale"
        and interest.unit
        and interest.workflow_status in (
            "Offer Accepted",
            "Negotiating",
            "Showing Scheduled",
        )
        and frappe.db.get_value("Real Estate Unit", interest.unit, "owner_lead")
    ):
        actions.append({
            **scoped,
            "action_type": "Meeting",
            "purpose": "Meeting with Owner",
            "label": _("Meet this Resale unit owner"),
        })
    return actions


def _current_workflow_snapshot(lead_doc):
    if _standalone_interests_available():
        interests = _get_standalone_interests(lead_doc.name, include_closed=True)
        active = [row for row in interests if row.is_active]
        return {
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
            rows.append({
                "name": interest.name,
                "legacy_row_name": interest.legacy_child_row,
                "label": _("{0}: {1}").format(interest.category or "Interest", label),
                "unit": interest.unit,
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
                "requested_area": interest.requested_area,
                "requested_unit_type": interest.requested_unit_type,
                "requested_budget": interest.requested_budget,
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


def _validate_action_interest_scope(lead_doc, action_type, purpose, row_names, unit=None):
    """Ensure standalone interest scopes are valid and independent for the requested action."""
    selected = _resolve_standalone_interest_names(lead_doc.name, row_names) if _standalone_interests_available() else _parse_json_list(row_names)
    if not selected:
        return
    if _standalone_interests_available():
        rows = [_get_standalone_interest(lead_doc.name, name) for name in selected]
        if action_type == "Send Offer" and any(row.record_type != "Inventory Unit" or not row.unit or row.workflow_status != "Matched" for row in rows):
            frappe.throw(_("Offers can be sent only for Matched inventory interests."))
        if action_type == "Call" and purpose == "Offer Follow-up" and any(row.workflow_status not in ("Offer Sent", "Offer Viewed") for row in rows):
            frappe.throw(_("Offer follow-up must target sent or viewed offers."))
        if action_type == "Offer Decision":
            if len(rows) != 1 or rows[0].workflow_status not in ("Offer Sent", "Offer Viewed"):
                frappe.throw(_("Offer Decision must target exactly one sent or viewed offer."))
        if action_type == "Meeting" and purpose == "Meeting with Owner":
            if (
                len(rows) != 1
                or rows[0].category != "Resale"
                or not rows[0].unit
                or rows[0].workflow_status not in (
                    "Offer Accepted",
                    "Negotiating",
                    "Showing Scheduled",
                )
            ):
                frappe.throw(
                    _("Meeting with Owner requires one accepted, negotiating, or showing-stage Resale interest.")
                )
            if not frappe.db.get_value("Real Estate Unit", rows[0].unit, "owner_lead"):
                frappe.throw(_("The selected Resale unit has no linked owner Lead."))
            if unit and unit != rows[0].unit:
                frappe.throw(_("Owner Meeting unit must match the selected Resale interest."))
        if action_type in ("Negotiation Follow-up", "Showing") and any(
            row.record_type != "Inventory Unit" or not row.unit or row.workflow_status not in (
                "Offer Accepted", "Negotiating", "Shown - Considering", "Shown - Interested"
            )
            for row in rows
        ):
            frappe.throw(_("Negotiation and Showing require accepted or negotiating inventory interests."))
        if action_type == "Showing":
            if len(rows) != 1 or unit != rows[0].unit:
                frappe.throw(_("Showing must target exactly one selected interest and its linked unit."))
        return

    selected_set = set(selected)
    rows_by_name = {row.name: row for row in (lead_doc.get("interested_in_units") or []) if row.name}
    if any(name not in rows_by_name for name in selected_set):
        frappe.throw(_("One or more selected interest rows were not found."), frappe.PermissionError)


def _pipeline_target_from_facts(lead_doc):
    """Resolve the highest verified commercial milestone without relying on the last dialog."""
    facts = _current_workflow_snapshot(lead_doc)
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
    if purpose in ("Offer Follow-up", "Offer Decision", "Negotiation Follow-up") or action_type in (
        "Offer Decision",
        "Showing",
        "Negotiation Follow-up",
    ):
        return _latest_offer_origin_status(lead_doc)
    return lead_doc.get("status")


def _apply_planning_milestone(lead_doc, action_type, interest_rows=None, action=None):
    if action_type != "Showing":
        return False
    if _standalone_interests_available():
        names = _resolve_standalone_interest_names(lead_doc.name, interest_rows)
        for name in names:
            _transition_standalone_interest(
                name,
                "Showing Scheduled",
                action=action.name if action else None,
                outcome="Scheduled",
                reason=_("Showing scheduled for this interest"),
            )
        _reconcile_standalone_rollup(lead_doc.name, _("Showing scheduled"))
        return bool(names)
    changed = _set_lead_status(lead_doc, LEAD_STATUS_OFFER_SELECTED, _("Showing scheduled for selected negotiating unit"))
    if changed:
        _save_workflow_doc(lead_doc)
    return changed


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
        LEAD_STATUS_OFFER_SELECTED: 4,
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
    if action_type == "Showing" and not unit:
        frappe.throw(_("A Showing action requires one related unit."))
    if action_type == "Meeting" and purpose == "Meeting with Owner" and not unit:
        frappe.throw(_("Meeting with Owner requires one related Resale unit."))

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

    if create_event and action_type == "Meeting" and purpose == "Meeting with Owner" and unit and seller_lead:
        _create_lead_event(
            seller_lead,
            _("Owner meeting scheduled for unit {0}").format(unit),
            scheduled_start,
            meeting_type="Meeting with Owner",
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
            "request_status": interest.request_status,
            "request_notes": interest.request_notes,
            "requested_destination": interest.requested_destination,
            "requested_area": interest.requested_area,
            "requested_unit_type": interest.requested_unit_type,
            "requested_budget": interest.requested_budget,
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
    if lead_doc.get("party_type") == "Seller":
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
    if not action_type or not scheduled_start:
        frappe.throw(_("Next action type and scheduled date/time are mandatory."))
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
    requires_rows = action_type in ("Send Offer", "Offer Decision", "Negotiation Follow-up", "Showing") or (
        action_type == "Meeting" and purpose == "Meeting with Owner"
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
        effective_price, price_source = _effective_unit_price(unit)
        message_lines.extend([
            "{0}. {1} — {2}".format(index, project_label, destination),
            " | ".join(filter(None, facts)),
            _("Total Gross: {0}").format(_format_offer_price(effective_price)),
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

    if outcome == "Cancelled" and action.action_type != "Showing":
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
        if outcome not in ("Completed", "Buyer No Show", "Seller/Unit Unavailable", "Cancelled", "Rescheduled"):
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
        elif outcome in ("Buyer No Show", "Seller/Unit Unavailable", "Cancelled") and _standalone_interests_available():
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
            payload["outcome"] = "Offer Selected"
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

    elif action.action_type == "Add Interest":
        # Interest rows themselves are written by the atomic bundle before this result is finalized.
        if outcome not in ("Added", "Matched", "Updated", "No Change"):
            frappe.throw(_("Interest action outcome must be Added, Matched, Updated, or No Change."))

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
        "Seller/Unit Unavailable": "Cancelled",
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
        action_data.get("action_type") in ("Showing", "Meeting")
        and action_data.get("purpose") in ("Showing Confirmation", "Meeting with Owner")
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
    replay = _bundle_idempotent_result(lead, client_request_id)
    if replay:
        return replay

    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    _require_action_doctype()
    _lock_lead_workflow(lead)

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

    decision = result_data.get("decision") or result_data.get("outcome")
    interest_allowed = (
        action.action_type == "Add Interest"
        or (
            action.action_type == "Call"
            and action.purpose == "Initial Qualification"
            and result_data.get("contact_result") == "Answered"
            and result_data.get("qualification") == "Interested"
        )
        or (action.action_type == "Meeting" and action.purpose == "Explore Meeting")
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
            frappe.throw(_("The matched inventory must keep the original request category."))
        if _to_int(interest_data.get("requested_unit")) or not _parse_json_list(interest_data.get("units")):
            frappe.throw(_("Select at least one actual inventory unit to fulfill this request."))

    applied_interest = None
    promoted_interest_names = []
    if interest_data:
        if not interest_allowed:
            frappe.throw(_("This action cannot change Lead interests."), frappe.PermissionError)
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
