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


def _validate_inventory_interest(category, units):
    if category not in ("Resale", "Primary"):
        return
    if not units:
        frappe.throw(_("At least one inventory unit is required for {0} interest.").format(category))

    for unit_name in units:
        if not frappe.db.exists("Real Estate Unit", unit_name):
            frappe.throw(_("Real Estate Unit {0} was not found.").format(unit_name))
        unit = frappe.db.get_value(
            "Real Estate Unit",
            unit_name,
            ["status", "owner_lead"],
            as_dict=True,
        )
        if unit.status != "Available":
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
    """Record call outcome. No Answer updates flags + optional next call scheduling.
    Answered resets consecutive counter and moves status to Contacted."""
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
def record_interest_determination(lead, interested, is_primary_buyer=0, interest_data=None):
    """Record the mandatory Interested/Not Interested outcome and its payload."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    interested = int(interested or 0)

    if not interested:
        doc.interest_status = "Not Interested"
        _add_lead_comment(doc, _("Call qualification outcome: Not Interested."))
        doc.save(ignore_permissions=True)
        return {"status": doc.status, "interested": False, "interest_status": "Not Interested"}

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
    doc.is_primary_buyer = int(is_primary_buyer or category == "Primary")
    for field in [
        "area_unit", "preferred_unit_type", "preferred_area", "preferred_developer",
        "preferred_compound", "preferred_finishing_type", "preferred_delivery_time", "buyer_budget",
    ]:
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

    if category in ("Brokerage Request", "International"):
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
        doc.previous_status = doc.status
        _set_lead_status(doc, LEAD_STATUS_OFFER_SENT, _("Send Offer scheduled"))
        _add_lead_comment(doc, _("Next action: Send Offer scheduled for {0}. Notes: {1}").format(starts_on, notes or ""))
        _save_workflow_doc(doc)
        return {"action_type": action_type, "scheduled": True, "status": doc.status}

    event_name = _create_lead_event(lead=lead, subject=event_subject, starts_on=starts_on, meeting_type=action_type, notes=notes)
    result = {"action_type": action_type, "event": event_name, "scheduled": True}
    status_changed = False
    if action_type in ("Meeting", "Showing") and doc.get("interest_status") == "Interested":
        doc.previous_status = doc.status
        status_changed = _set_lead_status(
            doc,
            LEAD_STATUS_OFFER_SELECTED,
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
        "request_notes": request_notes,
        "request_status": request_status or "Open",
    })
    doc.save(ignore_permissions=True)
    return doc.as_dict()


@frappe.whitelist()
def link_interested_units(lead, units):
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    if isinstance(units, str):
        units = json.loads(units)
    existing_units = {row.unit for row in doc.get("interested_in_units") or [] if row.unit}
    added = 0
    for unit in units:
        if not unit or unit in existing_units:
            continue
        if not frappe.db.exists("Real Estate Unit", unit):
            continue
        doc.append("interested_in_units", {
            "doctype": "Lead Interested Unit",
            "interest_record_type": "Inventory Unit",
            "unit": unit,
        })
        existing_units.add(unit)
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
def get_available_units_for_selection(lead=None):
    """Return available inventory units for the unit selection popup."""
    units = frappe.get_all("Real Estate Unit", filters={"status": "Available"}, fields=[
        "name", "sku", "project", "developer", "unit_type", "floor", "finishing_type", "status", "price",
    ], order_by="modified desc", limit_page_length=200)
    if lead and frappe.db.exists("CRM Lead", lead):
        lead_doc = frappe.get_doc("CRM Lead", lead)
        already_linked = {row.unit for row in lead_doc.get("interested_in_units") or [] if row.unit}
        units = [u for u in units if u.name not in already_linked]
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
        _validate_inventory_interest(category, [unit] if unit else [])
        row.interest_record_type = "Inventory Unit"
        row.interest_category = category
        row.unit = unit
        row.request_notes = None
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
    elif category == "International":
        international_type = interest_data.get("international_type")
        country = interest_data.get("international_country")
        if not international_type or not country:
            frappe.throw(_("International category and country are mandatory."))
        row.interest_record_type = "International"
        row.interest_category = category
        row.unit = None
        row.international_type = international_type
        row.international_country = country
        row.international_details = interest_data.get("international_details")

    row.unit_interest_status = interest_data.get("unit_interest_status") or row.get("unit_interest_status") or "Active"
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
