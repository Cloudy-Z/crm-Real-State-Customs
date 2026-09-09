"""
Real Estate CRM Customs — Buyer Lead Action Web API (v2.1)
==========================================================
Gated workflow: Fresh Lead → Call/WhatsApp → Call Log → Interest → Next Action → Meeting/Showing → Result → Loop
"""
import json
import urllib.parse
import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime, time_diff_in_hours, add_to_date


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
    return frappe.get_doc("CRM Lead", lead)


def _to_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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
    for row in doc.get("interested_in_units") or []:
        if row.name == row_name:
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
            ["status", "owner_lead"],
            as_dict=True,
        )
        if unit.status != "Available" and unit_name not in allowed_unavailable:
            frappe.throw(_("Unit {0} is not available.").format(unit_name))
        if category == "Resale" and not unit.owner_lead:
            frappe.throw(_("Resale interest must link a seller-owned resale unit."))
        if category == "Primary" and unit.owner_lead:
            frappe.throw(_("Primary interest must link open developer inventory, not a seller-owned resale unit."))


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


def guard_crm_lead_workflow(doc, method=None):
    """Prevent agent-side manual status changes and direct child-row deletion."""
    if doc.is_new():
        if not doc.get("party_type"):
            doc.party_type = "Buyer"
        return

    previous = doc.get_doc_before_save()
    if not previous:
        return

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


def _validate_buyer_lead(lead_doc):
    if lead_doc.get("party_type") and lead_doc.get("party_type") != "Buyer":
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
    frappe.db.commit()


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
    _validate_inventory_interest(category, units)

    if category == "Brokerage Request" and not interest_data.get("request_notes"):
        frappe.throw(_("Brokerage requirements are mandatory."))
    if category == "International":
        if not interest_data.get("international_type"):
            frappe.throw(_("International category is mandatory."))
        if not interest_data.get("international_country"):
            frappe.throw(_("Country is mandatory for International requests."))

    doc.interest_status = "Interested"
    # Inventory details remain authoritative on Real Estate Unit. Manual lead
    # preferences are accepted only for unmatched request categories.
    if category in ("Brokerage Request", "International"):
        for field in (
            "area_unit",
            "preferred_unit_type",
            "preferred_area",
            "preferred_finishing_type",
            "preferred_delivery_time",
            "buyer_budget",
        ):
            if field in interest_data:
                doc.set(field, interest_data[field])

    for unit in units:
        if any(r.unit == unit and r.get("interest_category") == category for r in (doc.get("interested_in_units") or []) if r.unit):
            continue
        doc.append("interested_in_units", {
            "doctype": "Lead Interested Unit",
            "interest_record_type": "Inventory Unit",
            "interest_category": category,
            "unit": unit,
            "unit_interest_status": "Active",
            "proposal_status": "Pending",
        })

    if category == "Brokerage Request":
        doc.append("interested_in_units", {
            "doctype": "Lead Interested Unit",
            "interest_record_type": "Request",
            "interest_category": category,
            "request_notes": interest_data.get("request_notes"),
            "request_status": "Open",
            "unit_interest_status": "Active",
        })

    if category == "International":
        doc.append("interested_in_units", {
            "doctype": "Lead Interested Unit",
            "interest_record_type": "International",
            "interest_category": category,
            "international_type": interest_data.get("international_type"),
            "international_country": interest_data.get("international_country"),
            "international_details": interest_data.get("international_details"),
            "unit_interest_status": "Active",
        })

    doc.is_primary_buyer = int(any(
        row.get("interest_category") == "Primary"
        and row.get("unit_interest_status") != "Lost Interest"
        for row in (doc.get("interested_in_units") or [])
    ))

    if category in ("Brokerage Request", "International") and doc.status in (LEAD_STATUS_NEW, LEAD_STATUS_FRESH):
        doc.previous_status = doc.status
        _set_lead_status(doc, LEAD_STATUS_REQUESTED, _("Interest recorded: {0}").format(category))

    _add_lead_comment(doc, _("Call qualification outcome: Interested — {0}.").format(category))
    _save_workflow_doc(doc)
    return {
        "status": doc.status,
        "interested": True,
        "is_primary_buyer": doc.is_primary_buyer,
        "interest_status": "Interested",
        "interest_category": category,
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
def create_resale_unit(owner_lead, project, unit_number, price=None):
    if not frappe.db.exists("CRM Lead", owner_lead):
        frappe.throw(_("Lead {0} was not found.").format(owner_lead), frappe.DoesNotExistError)
    lead = frappe.get_doc("CRM Lead", owner_lead)
    if lead.get("party_type") and lead.get("party_type") != "Seller":
        frappe.throw(_("Only Seller leads can list resale units."), frappe.ValidationError)
    if not project:
        frappe.throw(_("Project is required."), frappe.ValidationError)
    if not unit_number:
        frappe.throw(_("Unit Number is required."), frappe.ValidationError)
    unit = frappe.get_doc({
        "doctype": "Real Estate Unit",
        "project": project,
        "unit_number": unit_number,
        "unit_type": "Resale",
        "status": "Available",
        "price": price,
        "owner_lead": owner_lead,
    })
    unit.insert()
    return unit.as_dict()


@frappe.whitelist()
def add_interest_request(lead, request_notes, request_status="Open"):
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    doc.append("interested_in_units", {
        "doctype": "Lead Interested Unit",
        "interest_record_type": "Request",
        "interest_category": "Brokerage Request",
        "request_notes": request_notes,
        "request_status": request_status or "Open",
        "unit_interest_status": "Active",
    })
    doc.interest_status = "Interested"
    if doc.status in (LEAD_STATUS_NEW, LEAD_STATUS_FRESH):
        doc.previous_status = doc.status
        _set_lead_status(doc, LEAD_STATUS_REQUESTED, _("Brokerage request added"))
    _save_workflow_doc(doc)
    return doc.as_dict()


@frappe.whitelist()
def link_interested_units(lead, units, interest_category="Resale"):
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    if isinstance(units, str):
        units = json.loads(units)
    units = list(dict.fromkeys(filter(None, units or [])))
    _validate_inventory_interest(interest_category, units)
    existing_units = {
        (row.unit, row.get("interest_category"))
        for row in doc.get("interested_in_units") or []
        if row.unit
    }
    added = 0
    for unit in units:
        if not unit or (unit, interest_category) in existing_units:
            continue
        if not frappe.db.exists("Real Estate Unit", unit):
            continue
        doc.append("interested_in_units", {
            "doctype": "Lead Interested Unit",
            "interest_record_type": "Inventory Unit",
            "interest_category": interest_category,
            "unit": unit,
            "unit_interest_status": "Active",
            "proposal_status": "Pending",
        })
        existing_units.add((unit, interest_category))
        added += 1
    if added:
        doc.save(ignore_permissions=True)
    return doc.as_dict()


@frappe.whitelist()
def assign_property_unit_to_seller(lead, unit):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)
    if not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit), frappe.DoesNotExistError)
    lead_doc = frappe.get_doc("CRM Lead", lead)
    if lead_doc.get("party_type") != "Seller":
        frappe.throw(_("Only Seller leads can be assigned property units."), frappe.ValidationError)
    unit_doc = frappe.get_doc("Real Estate Unit", unit)
    if unit_doc.get("status") != "Available":
        frappe.throw(_("Only Available units can be assigned to a seller lead."), frappe.ValidationError)
    if unit_doc.get("owner_lead") and unit_doc.get("owner_lead") != lead:
        frappe.throw(_("Unit {0} is already assigned to seller lead {1}.").format(unit, unit_doc.get("owner_lead")), frappe.ValidationError)
    unit_doc.owner_lead = lead
    unit_doc.save()
    return unit_doc.as_dict()


@frappe.whitelist()
def get_lead_linked_units(lead):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)
    lead_doc = frappe.get_doc("CRM Lead", lead)
    interest_table_rows = lead_doc.get("interested_in_units") or []
    interested_rows = [row for row in interest_table_rows if row.unit]
    non_unit_rows = [row for row in interest_table_rows if not row.unit]
    interested_units = [row.unit for row in interested_rows]
    interest_by_unit = {row.unit: row for row in interested_rows}
    names = set(interested_units)
    owner_rows = frappe.get_all("Real Estate Unit", filters={"owner_lead": lead}, pluck="name")
    names.update(owner_rows)
    rows = []
    if names:
        rows = frappe.get_all("Real Estate Unit", filters={"name": ["in", list(names)]}, fields=[
            "name", "sku", "project", "developer", "unit_type", "floor", "finishing_type", "status", "price", "owner_lead", "modified",
        ], order_by="modified desc")
    interested_set = set(interested_units)
    for row in rows:
        interest_row = interest_by_unit.get(row.name)
        row.interest_record_type = "Inventory Unit"
        row.interest_row_name = interest_row.name if interest_row else None
        row.interest_category = interest_row.get("interest_category") if interest_row else None
        row.unit_interest_status = interest_row.get("unit_interest_status") if interest_row else None
        row.offer_sent = interest_row.get("offer_sent") if interest_row else 0
        row.offer_sent_at = interest_row.get("offer_sent_at") if interest_row else None
        row.deletion_request_status = interest_row.get("deletion_request_status") if interest_row else None
        row.deletion_request = interest_row.get("deletion_request") if interest_row else None
        if row.name in interested_set and row.owner_lead == lead:
            row.relationship = _("Interested and Owned")
        elif row.owner_lead == lead:
            row.relationship = _("Seller Unit")
        else:
            row.relationship = _("Interested Unit")
        row.proposal_status = interest_row.get("proposal_status") if interest_row else None

    for index, interest_row in enumerate(non_unit_rows, start=1):
        record_type = interest_row.get("interest_record_type") or "Request"
        category = interest_row.get("interest_category") or (
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
            "international_type": interest_row.get("international_type"),
            "international_country": interest_row.get("international_country"),
            "international_details": interest_row.get("international_details"),
            "outsource_company": interest_row.get("outsource_company"),
            "outsource_broker_name": interest_row.get("outsource_broker_name"),
            "outsource_broker_number": interest_row.get("outsource_broker_number"),
            "outsource_unit_details": interest_row.get("outsource_unit_details"),
            "unit_interest_status": interest_row.get("unit_interest_status"),
            "deletion_request_status": interest_row.get("deletion_request_status"),
            "deletion_request": interest_row.get("deletion_request"),
            "relationship": _("Interest Request"),
            "proposal_status": interest_row.get("proposal_status"),
            "owner_lead": None,
            "modified": interest_row.modified,
        }))
    return rows


@frappe.whitelist()
def get_available_units_for_selection(
    lead=None,
    interest_category="Resale",
    search=None,
    project=None,
    developer=None,
    location=None,
    unit_type=None,
    min_price=None,
    max_price=None,
    include_unit=None,
    page_length=100,
):
    """Return category-valid inventory units with price and project-location details."""
    if interest_category not in ("Resale", "Primary"):
        frappe.throw(_("Inventory selection is available only for Resale or Primary interests."))
    if lead:
        lead_doc = _get_lead_doc(lead)
        _validate_buyer_lead(lead_doc)
    else:
        lead_doc = None

    filters = {"status": "Available"}
    filters["owner_lead"] = ["is", "set" if interest_category == "Resale" else "not set"]
    if project:
        filters["project"] = ["like", "%{0}%".format(project.strip())]
    if developer:
        filters["developer"] = ["like", "%{0}%".format(developer.strip())]
    if unit_type:
        filters["unit_type"] = unit_type

    try:
        minimum = float(min_price) if min_price not in (None, "") else None
        maximum = float(max_price) if max_price not in (None, "") else None
        if minimum is not None and maximum is not None and minimum > maximum:
            frappe.throw(_("Minimum price cannot be greater than maximum price."))
        if minimum is not None and maximum is not None:
            filters["price"] = ["between", [minimum, maximum]]
        elif minimum is not None:
            filters["price"] = [">=", minimum]
        elif maximum is not None:
            filters["price"] = ["<=", maximum]
    except (TypeError, ValueError):
        frappe.throw(_("Price filters must be valid numbers."))

    if location:
        location_projects = frappe.get_all(
            "Real Estate Project",
            filters={"location": ["like", "%{0}%".format(location.strip())]},
            pluck="name",
            limit_page_length=500,
        )
        if not location_projects:
            return []
        if project:
            location_projects = [
                project_name
                for project_name in location_projects
                if project.strip().lower() in project_name.lower()
            ]
            if not location_projects:
                return []
        filters["project"] = ["in", location_projects]

    or_filters = None
    if search and search.strip():
        pattern = "%{0}%".format(search.strip())
        or_filters = [
            ["Real Estate Unit", "name", "like", pattern],
            ["Real Estate Unit", "sku", "like", pattern],
            ["Real Estate Unit", "project", "like", pattern],
            ["Real Estate Unit", "developer", "like", pattern],
            ["Real Estate Unit", "unit_type", "like", pattern],
        ]

    units = frappe.get_all(
        "Real Estate Unit",
        filters=filters,
        or_filters=or_filters,
        fields=[
            "name",
            "sku",
            "project",
            "developer",
            "unit_type",
            "floor",
            "finishing_type",
            "status",
            "price",
            "owner_lead",
            "modified",
        ],
        order_by="modified desc",
        limit_page_length=max(10, min(_to_int(page_length) or 100, 200)),
    )

    allowed_existing = include_unit if include_unit and frappe.db.exists("Real Estate Unit", include_unit) else None
    has_active_filters = any((search, project, developer, location, unit_type, min_price, max_price))
    if allowed_existing and not has_active_filters and not any(unit.name == allowed_existing for unit in units):
        existing_unit = frappe.db.get_value(
            "Real Estate Unit",
            allowed_existing,
            [
                "name",
                "sku",
                "project",
                "developer",
                "unit_type",
                "floor",
                "finishing_type",
                "status",
                "price",
                "owner_lead",
                "modified",
            ],
            as_dict=True,
        )
        category_matches = existing_unit and (
            (interest_category == "Resale" and existing_unit.get("owner_lead"))
            or (interest_category == "Primary" and not existing_unit.get("owner_lead"))
        )
        if category_matches:
            units.insert(0, existing_unit)

    already_linked = set()
    if lead_doc:
        already_linked = {
            row.unit
            for row in lead_doc.get("interested_in_units") or []
            if row.unit and row.unit != allowed_existing
        }
    units = [unit for unit in units if unit.name not in already_linked]

    project_names = list({unit.project for unit in units if unit.project})
    project_locations = {}
    if project_names:
        project_rows = frappe.get_all(
            "Real Estate Project",
            filters={"name": ["in", project_names]},
            fields=["name", "project_name", "location", "status"],
            limit_page_length=500,
        )
        project_locations = {row.name: row for row in project_rows}

    for unit in units:
        project_row = project_locations.get(unit.project) or {}
        unit.location = project_row.get("location")
        unit.project_label = project_row.get("project_name") or unit.project
        unit.project_status = project_row.get("status")
        unit.inventory_category = "Resale" if unit.owner_lead else "Primary"
    return units


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

    doc.previous_status = doc.status
    _set_lead_status(doc, LEAD_STATUS_OFFER_SENT, _("Offer sent"))
    _add_lead_comment(doc, _("Offer sent: {0} unit(s) marked as sent.").format(sent_count))
    _save_workflow_doc(doc)

    return {
        "status": doc.status,
        "sent_count": sent_count,
        "offer_sent_total": sum(1 for r in doc.get("interested_in_units") or [] if r.get("offer_sent")),
    }


# ---------------------------------------------------------------------------
# 10. Rollback Offer Rejection — Roll back to previous status
# ---------------------------------------------------------------------------
@frappe.whitelist()
def rollback_offer_rejection(lead):
    """When lead rejects all offers, roll back status to the previous pipeline stage."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)

    previous = doc.get("previous_status")
    if not previous:
        # Default rollback: if no previous_status recorded, go to Fresh Lead or New
        previous = LEAD_STATUS_FRESH if doc.get("source") else LEAD_STATUS_NEW

    _set_lead_status(doc, previous, _("Offer rejected"))
    doc.previous_status = ""
    _add_lead_comment(doc, _("Offer rejected — status rolled back to: {0}").format(previous))
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

    doc.previous_status = doc.status
    _set_lead_status(doc, LEAD_STATUS_NEGOTIATING, _("Offer accepted for negotiation"))

    comment = _("Lead moved to Negotiating.")
    if unit:
        comment = _("Lead moved to Negotiating on unit: {0}").format(unit)
    _add_lead_comment(doc, comment)
    _save_workflow_doc(doc)

    return {"status": doc.status}


# ---------------------------------------------------------------------------
# 12. Add Outsource Interest Record
# ---------------------------------------------------------------------------
@frappe.whitelist()
def add_outsource_interest(lead, company_name, broker_name=None, broker_number=None, unit_details=None):
    """Add an outsource unit to the interest table (unit from external broker/developer)."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)

    doc.append("interested_in_units", {
        "doctype": "Lead Interested Unit",
        "interest_record_type": "Outsource",
        "source_type": "Outsource",
        "outsource_company": company_name,
        "outsource_broker_name": broker_name,
        "outsource_broker_number": broker_number,
        "outsource_unit_details": unit_details,
        "unit_interest_status": "Active",
    })
    _add_lead_comment(doc, _("Outsource unit added from {0}").format(company_name))
    doc.save(ignore_permissions=True)

    return {"added": True, "company": company_name}


# ---------------------------------------------------------------------------
# 13. Mark Unit Interest Lost
# ---------------------------------------------------------------------------
@frappe.whitelist()
def mark_unit_interest_lost(lead, row_name):
    """Mark a specific interest row as Lost Interest (without deleting it)."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)

    found = False
    for row in doc.get("interested_in_units") or []:
        if row.name == row_name:
            row.unit_interest_status = "Lost Interest"
            found = True
            break

    if not found:
        frappe.throw(_("Interest row not found."))

    _add_lead_comment(doc, _("Lost interest marked for row: {0}").format(row_name))
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
    row = _find_interest_row(doc, row_name)
    if isinstance(interest_data, str):
        interest_data = json.loads(interest_data)
    interest_data = interest_data or {}

    category = interest_data.get("interest_category") or row.get("interest_category")
    if category not in ("Resale", "Primary", "Brokerage Request", "International", "Outsource"):
        frappe.throw(_("Please select a valid interest category."))

    if category in ("Resale", "Primary"):
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
    if category in ("Brokerage Request", "International"):
        for field in ("preferred_area", "preferred_unit_type", "buyer_budget"):
            if field in interest_data:
                doc.set(field, interest_data.get(field))
    doc.is_primary_buyer = int(any(
        other.get("interest_category") == "Primary"
        and other.get("unit_interest_status") != "Lost Interest"
        for other in (doc.get("interested_in_units") or [])
    ))
    _add_lead_comment(doc, _("Interest record updated: {0}").format(row_name))
    doc.save(ignore_permissions=True)
    return {"updated": True, "row": row.as_dict()}


@frappe.whitelist()
def request_interest_deletion(lead, row_name, reason):
    """Create a manager approval task; the interest row remains untouched."""
    if not reason:
        frappe.throw(_("Deletion reason is mandatory."))
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    row = _find_interest_row(doc, row_name)
    if row.get("deletion_request_status") == "Pending Manager Approval":
        return {"requested": True, "request": row.get("deletion_request")}

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

    row.deletion_request_status = "Pending Manager Approval"
    row.deletion_request = task.name
    _add_lead_comment(doc, _("Interest deletion requested for manager approval: {0}").format(row_name))
    doc.save(ignore_permissions=True)
    return {"requested": True, "request": task.name, "allocated_to": allocated_to}


@frappe.whitelist()
def review_interest_deletion(lead, row_name, decision, request_name=None, note=None):
    """Sales Manager/System Manager approves or rejects a pending deletion."""
    if not _is_manager():
        frappe.throw(_("Only a Sales Manager or System Manager can review deletion requests."), frappe.PermissionError)
    if decision not in ("Approve", "Reject"):
        frappe.throw(_("Decision must be Approve or Reject."))

    doc = _get_lead_doc(lead)
    row = _find_interest_row(doc, row_name)
    linked_request = request_name or row.get("deletion_request")
    if row.get("deletion_request_status") != "Pending Manager Approval":
        frappe.throw(_("This interest record has no pending deletion request."))

    if decision == "Approve":
        doc.set("interested_in_units", [r for r in doc.get("interested_in_units") or [] if r.name != row_name])
        _add_lead_comment(doc, _("Interest deletion approved by {0}: {1}. {2}").format(
            frappe.session.user, row_name, note or ""
        ))
        _save_approved_interest_deletion(doc, [row_name])
    else:
        row.deletion_request_status = "Rejected"
        _add_lead_comment(doc, _("Interest deletion rejected by {0}: {1}. {2}").format(
            frappe.session.user, row_name, note or ""
        ))
        doc.save(ignore_permissions=True)

    if linked_request and frappe.db.exists("ToDo", linked_request):
        frappe.db.set_value("ToDo", linked_request, "status", "Closed")

    return {"reviewed": True, "decision": decision, "deleted": decision == "Approve"}


@frappe.whitelist()
def get_interest_workflow_context(lead):
    """Return child rows and role capabilities required by the interest page."""
    doc = _get_lead_doc(lead)
    return {
        "rows": [row.as_dict() for row in (doc.get("interested_in_units") or [])],
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
ACTION_TYPES = ("Call", "Add Interest", "Meeting", "Showing", "Send Offer", "Negotiation Follow-up")
ACTION_PURPOSES = (
    "Initial Qualification",
    "Requirements Discovery",
    "Meeting Confirmation",
    "Showing Confirmation",
    "Offer Follow-up",
    "Negotiation Follow-up",
    "General Follow-up",
    "Discovery Meeting",
    "Offer Review",
    "Negotiation Meeting",
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
    return _parse_json_list(action.get("interest_rows"))


def _serialize_action(action):
    if not action:
        return None
    result = action.as_dict()
    result["interest_rows"] = _action_interest_rows(action)
    return result


def _validate_action_type(action_type, purpose=None):
    if action_type not in ACTION_TYPES:
        frappe.throw(_("Invalid action type: {0}").format(action_type))
    if purpose and purpose not in ACTION_PURPOSES:
        frappe.throw(_("Invalid action purpose: {0}").format(purpose))
    if action_type == "Showing" and purpose and purpose not in ("Showing Confirmation", "General Follow-up"):
        frappe.throw(_("Showing actions must use Showing Confirmation or General Follow-up purpose."))
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
    _require_action_doctype()
    statuses = list(ACTION_OPEN_STATUSES if not include_terminal else ACTION_OPEN_STATUSES + ACTION_TERMINAL_STATUSES)
    actions = frappe.get_all(
        "Lead Action Execution",
        filters={"lead": lead, "is_required": 1, "workflow_status": ["in", statuses]},
        fields=["name", "scheduled_start", "workflow_status"],
        order_by="scheduled_start asc, creation asc",
        limit_page_length=2,
    )
    if not actions:
        return None
    return _get_action_doc(actions[0].name, lead)


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
        return "meeting.result.v1"
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
    """Calculate the only actions legal for the lead's present facts."""
    if lead_doc.get("party_type") == "Seller":
        return []
    if lead_doc.get("interest_status") != "Interested":
        return [{
            "action_type": "Call",
            "purpose": "Initial Qualification",
            "label": _("Start initial qualification call"),
            "requires_interest_rows": False,
            "requires_unit": False,
        }]

    rows = lead_doc.get("interested_in_units") or []
    active_rows = [row for row in rows if row.get("unit_interest_status") != "Lost Interest"]
    inventory_rows = [row for row in active_rows if row.get("unit")]
    unsent_inventory_rows = [row for row in inventory_rows if not row.get("offer_sent")]
    sent_rows = [
        row for row in inventory_rows
        if row.get("offer_sent") and row.get("proposal_status") != "Rejected"
    ]
    negotiating_rows = [row for row in sent_rows if row.get("proposal_status") == "Offer Accepted"]
    options = [
        {
            "action_type": "Add Interest",
            "purpose": "Requirements Discovery",
            "label": _("Add or update buyer interest"),
            "requires_interest_rows": False,
            "requires_unit": False,
        },
        {
            "action_type": "Call",
            "purpose": "General Follow-up",
            "label": _("Schedule follow-up call"),
            "requires_interest_rows": False,
            "requires_unit": False,
        },
        {
            "action_type": "Meeting",
            "purpose": "Discovery Meeting",
            "label": _("Schedule discovery meeting"),
            "requires_interest_rows": False,
            "requires_unit": False,
        },
    ]
    if unsent_inventory_rows:
        options.append({
            "action_type": "Send Offer",
            "purpose": "Offer Follow-up",
            "label": _("Send offer for selected interest units"),
            "requires_interest_rows": True,
            "requires_unit": False,
            "interest_row_names": [row.name for row in unsent_inventory_rows],
        })
    if sent_rows:
        options.append({
            "action_type": "Call",
            "purpose": "Offer Follow-up",
            "label": _("Schedule offer follow-up"),
            "requires_interest_rows": True,
            "requires_unit": False,
            "interest_row_names": [row.name for row in sent_rows],
        })
    if negotiating_rows:
        options.append({
            "action_type": "Negotiation Follow-up",
            "purpose": "Negotiation Follow-up",
            "label": _("Record negotiation follow-up"),
            "requires_interest_rows": True,
            "requires_unit": False,
            "interest_row_names": [row.name for row in negotiating_rows],
        })
        options.append({
            "action_type": "Showing",
            "purpose": "Showing Confirmation",
            "label": _("Schedule showing for negotiating unit"),
            "requires_interest_rows": True,
            "requires_unit": True,
            "interest_row_names": [row.name for row in negotiating_rows],
        })
    return options


def _current_workflow_snapshot(lead_doc):
    rows = lead_doc.get("interested_in_units") or []
    active_rows = [row for row in rows if row.get("unit_interest_status") != "Lost Interest"]
    sent_rows = [
        row for row in active_rows
        if row.get("offer_sent") and row.get("proposal_status") != "Rejected"
    ]
    accepted_rows = [row for row in sent_rows if row.get("proposal_status") == "Offer Accepted"]
    return {
        "active_interest_count": len(active_rows),
        "sent_offer_count": len(sent_rows),
        "negotiating_interest_count": len(accepted_rows),
        "has_unmatched_request": any(
            row.get("interest_category") in ("Brokerage Request", "International")
            and row.get("unit_interest_status") != "Lost Interest"
            and row.get("request_status") not in ("Fulfilled", "Cancelled")
            for row in rows
        ),
    }


def _eligible_interest_rows(lead_doc):
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
        })
    return rows


def _validate_action_interest_scope(lead_doc, action_type, purpose, row_names, unit=None):
    """Ensure the selected rows are commercially valid for the requested action."""
    selected = set(_parse_json_list(row_names))
    if not selected:
        return
    rows_by_name = {row.name: row for row in (lead_doc.get("interested_in_units") or []) if row.name}
    rows = [rows_by_name[name] for name in selected if name in rows_by_name]
    if len(rows) != len(selected):
        frappe.throw(_("One or more selected interest rows were not found."), frappe.PermissionError)
    if action_type == "Send Offer":
        if any(not row.get("unit") or row.get("unit_interest_status") == "Lost Interest" or row.get("offer_sent") for row in rows):
            frappe.throw(_("Offers can be planned only for active inventory interests not already sent."))
    if action_type == "Call" and purpose == "Offer Follow-up":
        if any(not row.get("unit") or not row.get("offer_sent") or row.get("proposal_status") == "Rejected" for row in rows):
            frappe.throw(_("Offer follow-up must use live sent inventory offers."))
    if action_type in ("Negotiation Follow-up", "Showing"):
        if any(not row.get("unit") or row.get("proposal_status") != "Offer Accepted" for row in rows):
            frappe.throw(_("Negotiation and showing actions require selected accepted offer units."))
    if action_type == "Showing":
        if len(rows) != 1:
            frappe.throw(_("A showing action must target exactly one accepted offer interest record."))
        selected_units = {row.get("unit") for row in rows}
        if unit not in selected_units:
            frappe.throw(_("Showing unit must match the selected accepted offer interest record."))


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
    if purpose in ("Offer Follow-up", "Negotiation Follow-up") or action_type in ("Showing", "Negotiation Follow-up"):
        return _latest_offer_origin_status(lead_doc)
    return lead_doc.get("status")


def _apply_planning_milestone(lead_doc, action_type):
    if action_type != "Showing":
        return False
    changed = _set_lead_status(
        lead_doc,
        LEAD_STATUS_OFFER_SELECTED,
        _("Showing scheduled for selected negotiating unit"),
    )
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
        frappe.throw(_("Scheduled date and time are required."))

    if is_required and not allow_existing_required_action and _get_required_action(lead_doc.name):
        frappe.throw(_("This lead already has a required action. Complete, reschedule, or cancel it before creating another."))

    normalized_rows = _parse_json_list(interest_rows)
    if unit and not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit))
    if action_type == "Showing" and not unit:
        frappe.throw(_("A Showing action requires one related unit."))

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
    action.insert(ignore_permissions=True)

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
        }
    _validate_buyer_lead(lead_doc)
    _require_action_doctype()
    action = _get_required_action(lead)
    if action:
        action = _transition_action_to_due(action)

    blockers = []
    warnings = []
    if action:
        if action.workflow_status in ACTION_TERMINAL_STATUSES:
            action = None
        elif action.workflow_status == "In Progress":
            primary_command = _("Complete {0}").format(action.action_type)
        else:
            primary_command = _("Start {0}").format(action.action_type)
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
):
    """Create the next required action from the policy-approved choices only."""
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    _require_action_doctype()
    _lock_lead_workflow(lead)

    current = _get_required_action(lead)
    if current:
        if expected_required_action and current.name == expected_required_action:
            frappe.throw(_("Complete or reschedule the current action before planning another."))
        frappe.throw(_("Lead already has required action {0}.").format(current.name))

    allowed = _allowed_action_definitions(lead_doc)
    requested = next(
        (item for item in allowed if item["action_type"] == action_type and item["purpose"] == (purpose or item["purpose"])),
        None,
    )
    if not requested:
        frappe.throw(_("This action is not permitted for the lead's current workflow context."), frappe.PermissionError)
    if requested["requires_unit"] and not unit:
        frappe.throw(_("This action requires a related unit."))

    selected_rows = _parse_json_list(interest_rows)
    if requested["requires_interest_rows"] and not selected_rows:
        frappe.throw(_("Select one or more interest records for this action."))
    valid_rows = {row.name for row in lead_doc.get("interested_in_units") or [] if row.name}
    if not set(selected_rows) <= valid_rows:
        frappe.throw(_("One or more selected interest records do not belong to this lead."), frappe.PermissionError)
    _validate_action_interest_scope(lead_doc, action_type, purpose or requested["purpose"], selected_rows, unit)

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
        create_event=action_type != "Add Interest",
    )
    _apply_planning_milestone(lead_doc, action_type)
    return {"action": _serialize_action(action), "context": get_lead_action_context(lead)}


@frappe.whitelist()
def start_lead_action(lead, action_name):
    """Mark the current required action In Progress, without changing the lead facts."""
    lead_doc = _get_lead_doc(lead)
    _validate_buyer_lead(lead_doc)
    action = _get_action_doc(action_name, lead)
    current = _get_required_action(lead)
    if not current or current.name != action.name:
        frappe.throw(_("Only the current required action can be started."), frappe.PermissionError)
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


def _update_unit_outcomes(lead_doc, row_names, outcome):
    """Apply a per-unit offer outcome only to the rows scoped by the action."""
    allowed_outcomes = {"Viewed", "Offer Accepted", "Rejected", "Pending", "Sent"}
    if outcome not in allowed_outcomes:
        frappe.throw(_("Invalid offer outcome: {0}").format(outcome))
    selected = set(_parse_json_list(row_names))
    if not selected:
        frappe.throw(_("Select one or more interest records for this offer outcome."))
    changed = 0
    for row in lead_doc.get("interested_in_units") or []:
        if row.name not in selected:
            continue
        if not row.get("unit"):
            frappe.throw(_("Offer outcomes can only be recorded against inventory-unit interests."))
        row.proposal_status = outcome
        if outcome == "Rejected":
            row.unit_interest_status = "Lost Interest"
        elif outcome == "Offer Accepted":
            row.unit_interest_status = "Active"
        changed += 1
    if not changed:
        frappe.throw(_("No matching interest rows were found."))
    return changed


def _all_rows_rejected(lead_doc, scoped_rows):
    scoped = set(_parse_json_list(scoped_rows))
    if not scoped:
        return False
    rows = [row for row in lead_doc.get("interested_in_units") or [] if row.name in scoped]
    return bool(rows) and all(row.get("proposal_status") == "Rejected" for row in rows)


def _reconcile_stage_after_unit_rejection(lead_doc, action, action_label):
    """Downgrade only when the rejected unit was the fact supporting the current stage."""
    target = _pipeline_target_from_facts(lead_doc)
    if not target:
        target = action.get("offer_origin_status") or _latest_offer_origin_status(lead_doc)
    return _set_lead_status(lead_doc, target, action_label)


def _has_live_sent_offer(lead_doc):
    return any(
        row.get("offer_sent") and row.get("proposal_status") != "Rejected"
        for row in (lead_doc.get("interested_in_units") or [])
    )


def _next_action_is_required(lead_doc, action, payload):
    if payload.get("outcome") == "Rescheduled":
        return False
    if action.action_type == "Call" and action.purpose == "Initial Qualification":
        return payload.get("contact_result") != "Answered" or payload.get("qualification") == "Interested"
    return lead_doc.get("interest_status") == "Interested"


def _validate_next_action_payload(lead_doc, action, result_data):
    next_action = result_data.get("next_action") or None
    if not _next_action_is_required(lead_doc, action, result_data):
        return
    if not next_action:
        frappe.throw(_("This action requires one next action before it can be completed."))
    action_type = next_action.get("action_type")
    purpose = next_action.get("purpose")
    scheduled_start = next_action.get("scheduled_start")
    if not action_type or not scheduled_start:
        frappe.throw(_("Next action type and scheduled date/time are mandatory."))
    _validate_action_type(action_type, purpose)
    allowed = _allowed_action_definitions(lead_doc)
    if not any(item["action_type"] == action_type and item["purpose"] == purpose for item in allowed):
        frappe.throw(_("This next action is not permitted for the lead's updated workflow context."), frappe.PermissionError)
    if action_type == "Showing" and not next_action.get("unit"):
        frappe.throw(_("A next Showing action requires one related unit."))
    selected_rows = _parse_json_list(next_action.get("interest_rows"))
    if action_type in ("Send Offer", "Negotiation Follow-up", "Showing") and not selected_rows:
        frappe.throw(_("Select one or more interest rows for the next action."))
    valid_rows = {row.name for row in lead_doc.get("interested_in_units") or [] if row.name}
    if not set(selected_rows) <= valid_rows:
        frappe.throw(_("One or more next-action interest rows do not belong to this lead."), frappe.PermissionError)
    _validate_action_interest_scope(lead_doc, action_type, purpose, selected_rows, next_action.get("unit"))


def _result_interest_rows(lead_doc, action, payload, required=False):
    """Resolve result rows while preventing an action from mutating interests outside its stored scope."""
    action_scope = set(_action_interest_rows(action))
    payload_scope = set(_parse_json_list(payload.get("interest_rows")))
    if payload_scope and action_scope and not payload_scope <= action_scope:
        frappe.throw(_("The result contains an interest record outside this action's scope."), frappe.PermissionError)
    selected = payload_scope or action_scope
    valid_rows = {row.name for row in (lead_doc.get("interested_in_units") or []) if row.name}
    if not selected <= valid_rows:
        frappe.throw(_("One or more result interest records no longer belong to this lead."), frappe.PermissionError)
    if required and not selected:
        frappe.throw(_("This action result requires at least one scoped interest record."))
    if action.action_type == "Showing":
        if len(selected) != 1:
            frappe.throw(_("A showing result must target exactly one interest record."))
        selected_row = next(
            row for row in (lead_doc.get("interested_in_units") or [])
            if row.name in selected
        )
        if selected_row.get("unit") != action.get("unit"):
            frappe.throw(_("The showing result interest does not match the action unit."))
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


def _apply_action_result_facts(lead_doc, action, payload):
    """Apply scoped domain facts. No global qualification change occurs on later actions."""
    outcome = payload.get("outcome")
    contact_result = payload.get("contact_result")
    result_note = payload.get("result_note")
    next_action = payload.get("next_action")
    next_action = next_action or None
    status_changed = False

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
            if qualification == "Not Interested" and not payload.get("closed_reason"):
                frappe.throw(_("A reason is mandatory when marking a lead Not Interested."))
        elif payload.get("qualification"):
            frappe.throw(_("Lead qualification can only be changed by an initial or explicit requalification action."), frappe.PermissionError)

        if action.purpose == "Offer Follow-up" and outcome in ("Viewed", "Offer Accepted", "Rejected"):
            result_rows = _result_interest_rows(lead_doc, action, payload, required=True)
            _update_unit_outcomes(lead_doc, result_rows, outcome)
            if outcome == "Rejected":
                status_changed = _reconcile_stage_after_unit_rejection(
                    lead_doc,
                    action,
                    _("Offer rejected for scoped unit"),
                ) or status_changed
        if action.purpose == "Negotiation Follow-up" and outcome == "Declined":
            selected = _result_interest_rows(lead_doc, action, payload, required=True)
            _update_unit_outcomes(lead_doc, selected, "Rejected")

    elif action.action_type == "Meeting":
        if outcome not in ("Done", "No Show", "Cancelled", "Rescheduled"):
            frappe.throw(_("Meeting outcome must be Done, No Show, Cancelled, or Rescheduled."))
        if outcome == "Done" and not result_note:
            frappe.throw(_("Meeting result note is mandatory when the meeting is Done."))
        if outcome in ("No Show", "Cancelled") and not payload.get("closed_reason"):
            frappe.throw(_("A reason is mandatory when a meeting is missed or cancelled."))

    elif action.action_type == "Showing":
        if not action.get("unit"):
            frappe.throw(_("Showing action has no related unit."))
        if outcome not in ("Completed", "Buyer No Show", "Seller/Unit Unavailable", "Cancelled", "Rescheduled"):
            frappe.throw(_("Invalid showing outcome."))
        if outcome == "Completed":
            unit_outcome = payload.get("unit_outcome")
            if unit_outcome not in ("Interested", "Considering", "Rejected", "No Feedback"):
                frappe.throw(_("Completed showing requires a unit outcome."))
            row_names = _result_interest_rows(lead_doc, action, payload, required=True)
            if unit_outcome == "Rejected":
                _update_unit_outcomes(lead_doc, row_names, "Rejected")
                status_changed = _reconcile_stage_after_unit_rejection(
                    lead_doc,
                    action,
                    _("Unit rejected after showing"),
                ) or status_changed
            elif unit_outcome == "Interested":
                _update_unit_outcomes(lead_doc, row_names, "Offer Accepted")
                status_changed = _set_lead_status(lead_doc, LEAD_STATUS_OFFER_SELECTED, _("Showing completed for selected unit")) or status_changed
        elif outcome in ("Buyer No Show", "Seller/Unit Unavailable", "Cancelled") and not payload.get("closed_reason"):
            frappe.throw(_("A reason is mandatory for this showing outcome."))

    elif action.action_type == "Send Offer":
        if outcome != "Dispatched":
            frappe.throw(_("An offer action can be completed only after successful dispatch."))
        row_names = _result_interest_rows(lead_doc, action, payload, required=True)
        _update_unit_outcomes(lead_doc, row_names, "Sent")
        for row in lead_doc.get("interested_in_units") or []:
            if row.name in set(_parse_json_list(row_names)):
                row.offer_sent = 1
                row.offer_sent_at = now_datetime()
        status_changed = _set_lead_status(lead_doc, LEAD_STATUS_OFFER_SENT, _("Offer dispatched")) or status_changed

    elif action.action_type == "Negotiation Follow-up":
        if outcome not in ("Continuing", "Terms Changed", "Accepted", "Declined"):
            frappe.throw(_("Invalid negotiation outcome."))
        selected = _result_interest_rows(lead_doc, action, payload, required=True)
        if outcome in ("Continuing", "Terms Changed", "Accepted"):
            _update_unit_outcomes(lead_doc, selected, "Offer Accepted")
            status_changed = _set_lead_status(lead_doc, LEAD_STATUS_NEGOTIATING, _("Negotiation updated")) or status_changed
        elif outcome == "Declined":
            _update_unit_outcomes(lead_doc, selected, "Rejected")
            status_changed = _reconcile_stage_after_unit_rejection(
                lead_doc,
                action,
                _("Negotiation declined for scoped unit"),
            ) or status_changed

    elif action.action_type == "Add Interest":
        # Interest rows themselves are written by record_interest_determination/update_interest_record.
        if outcome not in ("Added", "Updated", "No Change"):
            frappe.throw(_("Interest action outcome must be Added, Updated, or No Change."))

    if not status_changed:
        status_changed = _apply_pipeline_from_facts(lead_doc, _("Workflow facts updated: {0}").format(action.action_type))
    return next_action, status_changed


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
    current = _get_required_action(lead)
    if not current or current.name != action.name:
        frappe.throw(_("Only the current required action can be completed."), frappe.PermissionError)
    if expected_modified and str(action.modified) != str(expected_modified):
        frappe.throw(_("This action was changed by another user. Reload the lead before submitting."), frappe.ValidationError)
    if action.workflow_status != "In Progress":
        frappe.throw(_("Start the current action before submitting its result."), frappe.ValidationError)
    if not _ensure_action_not_completed(action, client_request_id):
        return {"action": _serialize_action(action), "context": get_lead_action_context(lead), "idempotent": True}

    if isinstance(result_data, str):
        result_data = json.loads(result_data)
    result_data = result_data or {}
    next_action, status_changed = _apply_action_result_facts(lead_doc, action, result_data)
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
    _sync_unit_showing_result(lead_doc, action, outcome, result_note)
    _add_lead_comment(lead_doc, _("Workflow action completed: {0} — {1}.").format(action.action_type, outcome or contact_result or "Completed"))
    _save_workflow_doc(lead_doc)

    successor = None
    if next_action and outcome != "Rescheduled":
        next_type = next_action.get("action_type")
        next_purpose = next_action.get("purpose")
        next_starts = next_action.get("scheduled_start")
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
            create_event=next_type != "Add Interest",
        )
        _apply_planning_milestone(lead_doc, next_type)
        action.successor_action = successor.name
        action.save(ignore_permissions=True)

    return {
        "action": _serialize_action(action),
        "successor_action": _serialize_action(successor),
        "status": lead_doc.status,
        "status_changed": status_changed,
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
    current = _get_required_action(lead)
    if not current or current.name != action.name:
        frappe.throw(_("Only the current required action can be cancelled."), frappe.PermissionError)
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
            create_event=next_action.get("action_type") != "Add Interest",
        )
        _apply_planning_milestone(lead_doc, next_action.get("action_type"))
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
