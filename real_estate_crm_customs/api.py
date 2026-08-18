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


def _set_lead_status(doc, status):
    if not status:
        return
    color_map = {
        LEAD_STATUS_FRESH: "blue",
        LEAD_STATUS_NO_ANSWER: "orange",
        LEAD_STATUS_CONTACTED: "blue",
        LEAD_STATUS_INTERESTED: "green",
        LEAD_STATUS_NOT_INTERESTED: "red",
    }
    _ensure_lead_status(status, color=color_map.get(status, "blue"))
    doc.status = status


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
        if consecutive == 1:
            doc.no_answer_first_call = 1
        if consecutive >= 2:
            doc.no_answer_second_call = 1
        # No Answer does NOT change the lead status — it only updates flags
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
            "no_answer_first_call": doc.no_answer_first_call,
            "no_answer_second_call": doc.no_answer_second_call,
            "no_answer_consecutive_count": doc.no_answer_consecutive_count,
            "no_answer_total_count": doc.no_answer_total_count,
            "last_call_outcome": doc.last_call_outcome,
            "last_call_at": str(doc.last_call_at),
            "scheduled_event": event_name,
        }

    elif outcome == "Answered":
        doc.no_answer_consecutive_count = 0
        doc.no_answer_first_call = 0
        doc.no_answer_second_call = 0
        doc.last_call_outcome = "Answered"
        doc.last_call_at = now_datetime()
        # Answered does NOT change the lead status — it only resets no-answer flags
        _add_lead_comment(doc, _("Call answered — streak reset (total history: {0})").format(total))
        doc.save(ignore_permissions=True)
        return {
            "status": doc.status,
            "no_answer_first_call": 0,
            "no_answer_second_call": 0,
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
    """After answered call, record interest status. If interested, save preferences and link units/requests."""
    doc = _get_lead_doc(lead)
    _validate_buyer_lead(doc)
    interested = int(interested or 0)

    if not interested:
        # Set flag, NOT status — status stays in the pipeline
        doc.is_not_interested = 1
        doc.is_interested = 0
        _add_lead_comment(doc, _("Lead marked as Not Interested after call."))
        doc.save(ignore_permissions=True)
        return {"status": doc.status, "interested": False, "is_not_interested": 1}

    # Set interest flag
    doc.is_interested = 1
    doc.is_not_interested = 0
    doc.is_primary_buyer = int(is_primary_buyer or 0)

    if interest_data:
        if isinstance(interest_data, str):
            interest_data = json.loads(interest_data)
        for field in ["area_unit", "preferred_unit_type", "preferred_area", "preferred_developer",
                      "preferred_compound", "preferred_finishing_type", "preferred_delivery_time", "buyer_budget"]:
            if field in interest_data:
                doc.set(field, interest_data[field])

        for unit in (interest_data.get("units") or []):
            if unit and not any(r.unit == unit for r in (doc.get("interested_in_units") or []) if r.unit):
                doc.append("interested_in_units", {
                    "doctype": "Lead Interested Unit",
                    "interest_record_type": "Inventory Unit",
                    "unit": unit,
                })

        request_notes = interest_data.get("request_notes")
        if request_notes:
            doc.append("interested_in_units", {
                "doctype": "Lead Interested Unit",
                "interest_record_type": "Request",
                "request_notes": request_notes,
                "request_status": "Open",
            })

    # If there's a request (not in inventory), move status to Requested
    has_request = interest_data and interest_data.get("request_notes") if isinstance(interest_data, dict) else False
    if has_request:
        _set_lead_status(doc, LEAD_STATUS_REQUESTED)

    _add_lead_comment(doc, _("Lead marked as Interested. Primary buyer: {0}").format(
        _("Yes") if doc.is_primary_buyer else _("No")))
    doc.save(ignore_permissions=True)
    return {
        "status": doc.status,
        "interested": True,
        "is_primary_buyer": doc.is_primary_buyer,
        "is_interested": 1,
        "is_not_interested": 0,
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
        _add_lead_comment(doc, _("Next action: Send Offer scheduled for {0}. Notes: {1}").format(starts_on, notes or ""))
        return {"action_type": action_type, "scheduled": True}

    event_name = _create_lead_event(lead=lead, subject=event_subject, starts_on=starts_on, meeting_type=action_type, notes=notes)
    result = {"action_type": action_type, "event": event_name, "scheduled": True}

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
    request_rows = [row for row in interest_table_rows if row.get("interest_record_type") == "Request" or not row.unit]
    interested_units = [row.unit for row in interested_rows]
    proposal_status_by_unit = {row.unit: row.get("proposal_status") for row in interested_rows}
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
        row.interest_record_type = "Inventory Unit"
        if row.name in interested_set and row.owner_lead == lead:
            row.relationship = _("Interested and Owned")
        elif row.owner_lead == lead:
            row.relationship = _("Seller Unit")
        else:
            row.relationship = _("Interested Unit")
        row.proposal_status = proposal_status_by_unit.get(row.name)
    for index, row in enumerate(request_rows, start=1):
        rows.append(frappe._dict({
            "name": row.name or f"request-{index}",
            "sku": _("Request"),
            "interest_record_type": "Request",
            "request_status": row.get("request_status") or "Open",
            "request_notes": row.get("request_notes"),
            "relationship": _("Interest Request"),
            "proposal_status": None,
            "owner_lead": None,
            "modified": row.modified,
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
