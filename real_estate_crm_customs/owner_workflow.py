"""Owner Lead acquisition and Unit listing workflow.

This module deliberately does **not** reuse the Buyer action engine.  Buyer
commercial actions are authoritative per ``Lead Interest``; Owner actions are
about acquiring, pricing, documenting, listing and periodically verifying an
Owner's Unit.  Both use ``Lead Action Execution`` as the immutable activity
record, but Owner Unit actions use ``scope_type == 'Unit'`` and never receive
an Interest scope.
"""

from __future__ import annotations

import json
from contextlib import nullcontext

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, getdate, now_datetime, nowdate

from real_estate_crm_customs.party_roles import is_owner_role
from real_estate_crm_customs.workflow_context import unit_mutation_context


# Owner Lead statuses are intentionally small and roll up from the Owner's
# Units.  They are distinct from the Buyer Interest-derived statuses.
OWNER_LEAD_STATUS_PRICING = "Pricing"
OWNER_LEAD_STATUS_DOCUMENTS = "Documents Pending"
OWNER_LEAD_STATUS_LISTED = "Listed"
OWNER_LEAD_STATUSES = (
    OWNER_LEAD_STATUS_PRICING,
    OWNER_LEAD_STATUS_DOCUMENTS,
    OWNER_LEAD_STATUS_LISTED,
)

LISTING_PRICING_REQUESTED = "Pricing Requested"
LISTING_PRICE_PROPOSED = "Price Proposed"
LISTING_DOCUMENTS_PENDING = "Documents Pending"
LISTING_LISTED = "Listed"
LISTING_VERIFICATION_DUE = "Verification Due"
LISTING_OFF_MARKET = "Off Market"
LISTING_WITHDRAWN = "Withdrawn"
OWNER_LISTING_STATUSES = (
    LISTING_PRICING_REQUESTED,
    LISTING_PRICE_PROPOSED,
    LISTING_DOCUMENTS_PENDING,
    LISTING_LISTED,
    LISTING_VERIFICATION_DUE,
    LISTING_OFF_MARKET,
    LISTING_WITHDRAWN,
)

OWNER_ACTION_OPEN_STATUSES = ("Planned", "Due", "In Progress")
OWNER_ACTION_TERMINAL_STATUSES = ("Completed", "Cancelled", "Rescheduled", "Missed")
OWNER_CONTACT_RESULTS = ("Answered", "No Answer", "Wrong Number", "Invalid / Disconnected")
OWNER_QUALIFICATIONS = ("Interested", "Not Interested")
REQUIRED_OWNERSHIP_DOCUMENTS = {"Ownership Contract", "Title Deed / Registration"}
REQUIRED_OWNER_ID_DOCUMENT = "Owner National ID / Passport"
ACCEPTED_DOCUMENT_STATUSES = {"Received", "Verified"}
NEGOTIATING_INTEREST_STATUSES = {
    "Offer Accepted",
    "Negotiating",
    "Showing Scheduled",
    "Shown - Considering",
    "Shown - Interested",
}

# Never accept system projections, availability locks, commercial records or
# an arbitrary workflow state from the Add Property payload.
UNIT_CREATION_FORBIDDEN_FIELDS = {
    "name",
    "owner",
    "creation",
    "modified",
    "modified_by",
    "docstatus",
    "idx",
    "sku",
    "status",
    "closing_interest",
    "sold_interest",
    "master_compound",
    "destination",
    "developer",
    "unit_type",
    "created_by",
    "price",
    "over_net",
    "down_payment",
    "property_tax",
    "commission",
    "total_price_net",
    "total_gross",
    "listing_status",
    "listing_status_reason",
    "proposed_over_price",
    "proposed_monthly_rent",
    "proposed_daily_rent",
    "pricing_basis",
    "price_proposed_on",
    "listed_on",
    "last_verified_on",
    "next_verification_due",
    "verification_alert_sent_on",
    "owner_documents",
    "listing_agreement_type",
    "listing_agreement_reference",
    "listing_agreement_signed_on",
    "listing_agreement_expires_on",
    "listing_commission_percent",
    "listing_agreement_file",
    "active_buyer_interest_count",
    "total_buyer_interest_count",
    "scheduled_showings",
}


def _api():
    """Late import prevents circular imports while reusing audited Lead helpers."""
    from real_estate_crm_customs import api

    return api


def _parse_object(value, label="payload"):
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            frappe.throw(_("{0} must be valid JSON.").format(label), frappe.ValidationError)
    if not isinstance(value, dict):
        frappe.throw(_("{0} must be an object.").format(label), frappe.ValidationError)
    return value


def _owner_lead(lead, write=True):
    api = _api()
    doc = api._get_lead_doc(lead)
    if write:
        api._assert_write_permission(doc)
    if not is_owner_role(doc.get("party_type")):
        frappe.throw(_("Only Owner leads can use the Owner listing workflow."), frappe.ValidationError)
    return doc


def _is_manager():
    return _api()._is_manager()


def _lock_lead(lead):
    frappe.db.sql("SELECT `name` FROM `tabCRM Lead` WHERE `name` = %s FOR UPDATE", (lead,))


def _lock_unit(unit):
    frappe.db.sql("SELECT `name` FROM `tabReal Estate Unit` WHERE `name` = %s FOR UPDATE", (unit,))


def _action_doc(action_name, lead=None):
    if not frappe.db.exists("Lead Action Execution", action_name):
        frappe.throw(_("Owner workflow action {0} was not found.").format(action_name), frappe.DoesNotExistError)
    action = frappe.get_doc("Lead Action Execution", action_name)
    if lead and action.get("lead") != lead:
        frappe.throw(_("This Owner workflow action belongs to another lead."), frappe.PermissionError)
    if action.get("scope_type") not in ("Lead", "Unit"):
        frappe.throw(_("This is not an Owner workflow action."), frappe.ValidationError)
    return action


def _owner_unit(unit_name, lead=None, write=True):
    if not frappe.db.exists("Real Estate Unit", unit_name):
        frappe.throw(_("Unit {0} was not found.").format(unit_name), frappe.DoesNotExistError)
    unit = frappe.get_doc("Real Estate Unit", unit_name)
    if lead and unit.get("owner_lead") != lead:
        frappe.throw(_("Unit {0} is not owned by this Owner lead.").format(unit_name), frappe.PermissionError)
    if unit.get("owner_lead"):
        party_type = frappe.db.get_value("CRM Lead", unit.owner_lead, "party_type")
        if not is_owner_role(party_type):
            frappe.throw(_("Unit {0} does not have a valid Owner lead.").format(unit_name), frappe.ValidationError)
    if write and frappe.session.user != "Administrator" and not frappe.has_permission("Real Estate Unit", ptype="write", doc=unit):
        frappe.throw(_("You do not have permission to update Unit {0}.").format(unit_name), frappe.PermissionError)
    return unit


def _is_owner_action(action):
    return action.get("scope_type") in ("Lead", "Unit") and action.get("action_type") in {
        "Call", "Meeting", "Add Property", "Agent Pricing", "Price Decision", "Listing Documents"
    }


def _transition_due(action):
    if action.get("workflow_status") == "Planned" and get_datetime(action.scheduled_start) <= now_datetime():
        action.workflow_status = "Due"
        action.save(ignore_permissions=True)
    return action


def _serialize_action(action):
    if not action:
        return None
    result = action.as_dict()
    result["is_owner_action"] = 1
    return result


def _open_lead_action(lead):
    names = frappe.get_all(
        "Lead Action Execution",
        filters={
            "lead": lead,
            "scope_type": "Lead",
            "is_required": 1,
            "workflow_status": ["in", list(OWNER_ACTION_OPEN_STATUSES)],
        },
        pluck="name",
        order_by="scheduled_start asc, creation asc",
        limit_page_length=2,
    )
    if len(names) > 1:
        frappe.throw(_("Owner lead has multiple open lead-level actions. Resolve the duplicate actions."), frappe.ValidationError)
    return _action_doc(names[0], lead) if names else None


def _open_unit_action(unit):
    names = frappe.get_all(
        "Lead Action Execution",
        filters={
            "unit": unit,
            "scope_type": "Unit",
            "is_required": 1,
            "workflow_status": ["in", list(OWNER_ACTION_OPEN_STATUSES)],
        },
        pluck="name",
        order_by="scheduled_start asc, creation asc",
        limit_page_length=2,
    )
    if len(names) > 1:
        frappe.throw(_("Unit {0} has multiple open Owner workflow actions. Resolve the duplicate actions.").format(unit), frappe.ValidationError)
    return _action_doc(names[0]) if names else None


def _create_event(lead, action_type, purpose, scheduled_start, unit=None, notes=None):
    subject = _("Owner: {0}").format(action_type)
    if purpose:
        subject = _("{0} — {1}").format(subject, purpose)
    if unit:
        subject = _("{0} ({1})").format(subject, unit)
    starts = get_datetime(scheduled_start)
    event = frappe.get_doc(
        {
            "doctype": "Event",
            "subject": subject,
            "starts_on": starts,
            "ends_on": add_to_date(starts, hours=1),
            "event_type": "Private",
            "status": "Open",
            "description": notes or "",
        }
    )
    event.append("event_participants", {"reference_doctype": "CRM Lead", "reference_docname": lead})
    event.insert(ignore_permissions=True)
    return event.name


def _set_event_state(action, status):
    if not action.get("event") or not frappe.db.exists("Event", action.event):
        return
    event = frappe.get_doc("Event", action.event)
    event.status = status
    event.save(ignore_permissions=True)


def _assert_action_definition(action_type, purpose):
    definitions = {
        "Call": {"Initial Qualification", "General Follow-up", "Pricing Follow-up", "Availability Verification"},
        "Meeting": {"Explore Meeting"},
        "Add Property": {"Listing Intake"},
        "Agent Pricing": {"Price Proposal"},
        "Price Decision": {"Price Decision"},
        "Listing Documents": {"Documents Collection"},
    }
    if action_type not in definitions or purpose not in definitions[action_type]:
        frappe.throw(_("Invalid Owner workflow action: {0} — {1}.").format(action_type, purpose), frappe.ValidationError)


def _create_owner_action(
    lead_doc,
    action_type,
    purpose,
    scheduled_start=None,
    unit=None,
    notes=None,
    source_action=None,
    required=1,
    allow_existing=False,
):
    """Create an Owner action without invoking Buyer Interest action helpers."""
    _assert_action_definition(action_type, purpose)
    scheduled_start = get_datetime(scheduled_start or now_datetime())
    if unit:
        _lock_unit(unit)
        _owner_unit(unit, lead_doc.name)
        existing = _open_unit_action(unit)
        if existing and not allow_existing:
            frappe.throw(_("Unit {0} already has open Owner action {1}.").format(unit, existing.name), frappe.ValidationError)
        scope_type = "Unit"
    else:
        existing = _open_lead_action(lead_doc.name)
        if existing and not allow_existing:
            frappe.throw(_("Owner lead already has open action {0}.").format(existing.name), frappe.ValidationError)
        scope_type = "Lead"
    event = _create_event(lead_doc.name, action_type, purpose, scheduled_start, unit=unit, notes=notes)
    action = frappe.get_doc(
        {
            "doctype": "Lead Action Execution",
            "lead": lead_doc.name,
            "action_type": action_type,
            "purpose": purpose,
            "workflow_status": "Planned",
            "is_required": int(bool(required)),
            "scope_type": scope_type,
            "scheduled_start": scheduled_start,
            "pipeline_status_at_start": lead_doc.get("status"),
            "qualification_at_start": lead_doc.get("interest_status"),
            "event": event,
            "unit": unit,
            "seller_lead": lead_doc.name,
            "source_action": source_action,
        }
    )
    action.insert(ignore_permissions=True)
    _api()._add_lead_comment(lead_doc, _("Owner workflow action planned: {0} — {1}.").format(action_type, purpose))
    return action


def _set_listing_status(unit, status, reason=None):
    if status not in OWNER_LISTING_STATUSES:
        frappe.throw(_("Invalid Owner listing status."), frappe.ValidationError)
    unit.listing_status = status
    unit.listing_status_reason = reason or None


def _listing_price_present(unit):
    sale_required = unit.get("listing_purpose") in ("For Sale", "For Sale or Rent", None, "")
    rent_required = unit.get("listing_purpose") in ("For Rent", "For Sale or Rent")
    sale_ok = not sale_required or float(unit.get("total_price_net") or 0) > 0 or float(unit.get("price") or 0) > 0
    rent_ok = not rent_required or float(unit.get("rental_monthly_rate") or 0) > 0 or float(unit.get("rental_daily_rate") or 0) > 0
    return sale_ok and rent_ok


def _is_private_file(file_url, label):
    if not file_url:
        return
    file_row = frappe.db.get_value("File", {"file_url": file_url}, ["name", "is_private"], as_dict=True)
    if not file_row or not file_row.get("is_private"):
        frappe.throw(_("{0} must be uploaded as a private file.").format(label), frappe.ValidationError)


def _listing_gate_errors(unit):
    errors = []
    if not _listing_price_present(unit):
        errors.append(_("Set the applicable sale and/or rent price."))
    acceptable_documents = {
        row.get("document_type")
        for row in (unit.get("owner_documents") or [])
        if row.get("document_status") in ACCEPTED_DOCUMENT_STATUSES and row.get("file")
    }
    if not acceptable_documents.intersection(REQUIRED_OWNERSHIP_DOCUMENTS):
        errors.append(_("Collect and privately upload an ownership contract or title deed / registration."))
    if REQUIRED_OWNER_ID_DOCUMENT not in acceptable_documents:
        errors.append(_("Collect and privately upload the Owner National ID / Passport."))
    for row in unit.get("owner_documents") or []:
        if row.get("file"):
            _is_private_file(row.file, _("Owner document"))
    if not unit.get("listing_agreement_type"):
        errors.append(_("Select the listing agreement type."))
    if not unit.get("listing_agreement_signed_on"):
        errors.append(_("Record the listing agreement signed date."))
    if not unit.get("listing_agreement_file"):
        errors.append(_("Privately upload the signed listing agreement."))
    else:
        _is_private_file(unit.listing_agreement_file, _("Signed listing agreement"))
    if unit.get("listing_agreement_expires_on") and getdate(unit.listing_agreement_expires_on) < getdate(nowdate()):
        errors.append(_("The listing agreement has expired."))
    return errors


def _apply_unit_fields(unit, values, *, proposal=False):
    values = _parse_object(values, _("Unit values"))
    if proposal:
        mapping = {
            "over_price": "proposed_over_price",
            "rental_monthly_rate": "proposed_monthly_rent",
            "rental_daily_rate": "proposed_daily_rent",
        }
    else:
        mapping = {
            "paid": "paid",
            "over_price": "over_price",
            "remaining": "remaining",
            "over_is_gross": "over_is_gross",
            "maintenance": "maintenance",
            "rental_monthly_rate": "rental_monthly_rate",
            "rental_daily_rate": "rental_daily_rate",
        }
    for source, target in mapping.items():
        if source in values:
            unit.set(target, values[source])


def _replace_owner_documents(unit, rows):
    if rows is None:
        return
    if not isinstance(rows, list):
        frappe.throw(_("Owner documents must be a list."), frappe.ValidationError)
    unit.set("owner_documents", [])
    allowed = {"document_type", "document_status", "file", "received_on", "notes"}
    for row in rows:
        if not isinstance(row, dict):
            frappe.throw(_("Every Owner document must be an object."), frappe.ValidationError)
        clean = {key: value for key, value in row.items() if key in allowed}
        if not clean.get("document_type"):
            frappe.throw(_("Owner document type is mandatory."), frappe.ValidationError)
        if clean.get("file"):
            _is_private_file(clean["file"], _("Owner document"))
        unit.append("owner_documents", clean)


def _apply_agreement_fields(unit, payload):
    agreement_fields = (
        "listing_agreement_type",
        "listing_agreement_reference",
        "listing_agreement_signed_on",
        "listing_agreement_expires_on",
        "listing_commission_percent",
        "listing_agreement_file",
    )
    for fieldname in agreement_fields:
        if fieldname in payload:
            unit.set(fieldname, payload[fieldname])
    if unit.get("listing_agreement_file"):
        _is_private_file(unit.listing_agreement_file, _("Signed listing agreement"))


def _save_owner_unit(unit):
    with unit_mutation_context("owner_workflow"):
        unit.save(ignore_permissions=True)
    refresh_unit_interest_counters(unit.name)
    return unit


def _initial_listing_status(unit):
    if unit.get("pricing_method") == "Agent Pricing" or not _listing_price_present(unit):
        return LISTING_PRICING_REQUESTED
    return LISTING_DOCUMENTS_PENDING


def _create_owner_unit(lead_doc, unit_data, pricing_method=None):
    data = _parse_object(unit_data, _("Unit data"))
    meta = frappe.get_meta("Real Estate Unit")
    allowed = {
        field.fieldname
        for field in meta.fields
        if field.fieldtype not in ("Section Break", "Column Break", "Table", "Table MultiSelect")
        and not field.read_only
        and field.fieldname not in UNIT_CREATION_FORBIDDEN_FIELDS
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        frappe.throw(_("Unit data includes unavailable or system-managed fields: {0}.").format(", ".join(unknown)), frappe.PermissionError)
    doc_data = {key: value for key, value in data.items() if key in allowed}
    doc_data.update({
        "doctype": "Real Estate Unit",
        "owner_lead": lead_doc.name,
        "inventory_type": "Resale",
        "pricing_method": pricing_method or data.get("pricing_method") or "Owner Price",
    })
    if doc_data["pricing_method"] not in ("Owner Price", "Agent Pricing"):
        frappe.throw(_("Pricing Method must be Owner Price or Agent Pricing."), frappe.ValidationError)
    # The controller validates the status too; set it before insert so Agent
    # Pricing can legitimately collect Unit data before a price exists.
    doc_data["listing_status"] = LISTING_PRICING_REQUESTED if doc_data["pricing_method"] == "Agent Pricing" else LISTING_DOCUMENTS_PENDING
    with unit_mutation_context("owner_intake"):
        unit = frappe.get_doc(doc_data)
        unit.insert(ignore_permissions=True)
    refresh_unit_interest_counters(unit.name)
    return unit


def _apply_call_result(lead_doc, action, payload):
    contact_result = payload.get("contact_result")
    if contact_result not in OWNER_CONTACT_RESULTS:
        frappe.throw(_("Select a valid call result."), frappe.ValidationError)
    if contact_result == "No Answer":
        lead_doc.no_answer_consecutive_count = _as_int(lead_doc.get("no_answer_consecutive_count")) + 1
        lead_doc.no_answer_total_count = _as_int(lead_doc.get("no_answer_total_count")) + 1
    elif contact_result == "Answered":
        lead_doc.no_answer_consecutive_count = 0
    lead_doc.last_call_outcome = contact_result
    lead_doc.last_call_at = now_datetime()
    if action.get("purpose") == "Initial Qualification" and contact_result == "Answered":
        qualification = payload.get("qualification")
        if qualification not in OWNER_QUALIFICATIONS:
            frappe.throw(_("An answered initial Owner call requires Interested or Not Interested."), frappe.ValidationError)
        lead_doc.interest_status = qualification
    elif payload.get("qualification"):
        frappe.throw(_("Owner qualification may only be set by the initial qualification call."), frappe.PermissionError)
    return contact_result


def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _complete_owner_action(lead_doc, action, payload):
    """Apply one Owner action result and return its optional successor specification."""
    unit = _owner_unit(action.unit, lead_doc.name) if action.get("scope_type") == "Unit" else None
    outcome = payload.get("outcome")
    result_note = payload.get("result_note")
    next_action = None

    if action.action_type == "Call":
        outcome = _apply_call_result(lead_doc, action, payload)
        if action.purpose == "Initial Qualification" and outcome == "Answered" and lead_doc.get("interest_status") == "Interested":
            if payload.get("unit_data"):
                unit = _create_owner_unit(lead_doc, payload["unit_data"], payload.get("pricing_method"))
                next_action = _automatic_unit_successor(lead_doc, unit, source_action=action.name)
            else:
                next_action = _next_action_payload(payload, fallback={"action_type": "Add Property", "purpose": "Listing Intake"}, required=True)
        elif lead_doc.get("interest_status") == "Not Interested":
            next_action = None
        else:
            next_action = _next_action_payload(payload)

    elif action.action_type == "Meeting":
        outcome = outcome or "Completed"
        next_action = _next_action_payload(payload)

    elif action.action_type == "Add Property":
        if not payload.get("unit_data"):
            frappe.throw(_("Add Property requires the complete Unit data."), frappe.ValidationError)
        unit = _create_owner_unit(lead_doc, payload["unit_data"], payload.get("pricing_method"))
        outcome = "Unit Added"
        next_action = _automatic_unit_successor(lead_doc, unit, source_action=action.name)

    elif action.action_type == "Agent Pricing":
        if not unit:
            frappe.throw(_("Agent Pricing requires a Unit."), frappe.ValidationError)
        basis = str(payload.get("pricing_basis") or "").strip()
        if not basis:
            frappe.throw(_("Provide a pricing basis (comparables and recommendation)."), frappe.ValidationError)
        _apply_unit_fields(unit, payload.get("proposed_prices") or {}, proposal=True)
        if not any(float(unit.get(fieldname) or 0) > 0 for fieldname in ("proposed_over_price", "proposed_monthly_rent", "proposed_daily_rent")):
            frappe.throw(_("Enter at least one agent-proposed sale or rent price."), frappe.ValidationError)
        unit.pricing_basis = basis
        unit.price_proposed_on = nowdate()
        _set_listing_status(unit, LISTING_PRICE_PROPOSED, _("Agent price proposal recorded."))
        _save_owner_unit(unit)
        outcome = "Price Proposed"
        next_action = _next_action_payload(payload, fallback={"action_type": "Price Decision", "purpose": "Price Decision", "unit": unit.name}, required=True)

    elif action.action_type == "Price Decision":
        if not unit:
            frappe.throw(_("Price Decision requires a Unit."), frappe.ValidationError)
        allowed = {"Accepted", "Owner Set Price", "Re-price", "Declined"}
        if outcome not in allowed:
            frappe.throw(_("Select Accepted, Owner Set Price, Re-price, or Declined."), frappe.ValidationError)
        if outcome in {"Accepted", "Owner Set Price"}:
            if outcome == "Accepted":
                proposed = {
                    "over_price": unit.get("proposed_over_price"),
                    "rental_monthly_rate": unit.get("proposed_monthly_rent"),
                    "rental_daily_rate": unit.get("proposed_daily_rent"),
                }
                _apply_unit_fields(unit, proposed)
            else:
                _apply_unit_fields(unit, payload.get("owner_prices") or {})
            unit.pricing_method = "Owner Price"
            _set_listing_status(unit, LISTING_DOCUMENTS_PENDING, _("Owner price confirmed."))
            _save_owner_unit(unit)
            next_action = _next_action_payload(payload, fallback={"action_type": "Listing Documents", "purpose": "Documents Collection", "unit": unit.name}, required=True)
        elif outcome == "Re-price":
            unit.pricing_method = "Agent Pricing"
            _set_listing_status(unit, LISTING_PRICING_REQUESTED, _("Owner requested repricing."))
            _save_owner_unit(unit)
            next_action = _next_action_payload(payload, fallback={"action_type": "Agent Pricing", "purpose": "Price Proposal", "unit": unit.name}, required=True)
        else:
            _assert_not_reserved(unit, outcome)
            _set_listing_status(unit, LISTING_WITHDRAWN, _("Owner declined the proposed price."))
            _save_owner_unit(unit)
            next_action = None

    elif action.action_type == "Listing Documents":
        if not unit:
            frappe.throw(_("Listing Documents requires a Unit."), frappe.ValidationError)
        _replace_owner_documents(unit, payload.get("owner_documents"))
        _apply_agreement_fields(unit, payload)
        mark_listed = bool(payload.get("mark_listed"))
        if mark_listed:
            errors = _listing_gate_errors(unit)
            if errors:
                frappe.throw(_("Cannot list this Unit yet: {0}").format("; ".join(str(error) for error in errors)), frappe.ValidationError)
            _mark_listed(unit, _("Owner documents and listing agreement verified."))
            _save_owner_unit(unit)
            next_action = _automatic_unit_successor(lead_doc, unit, source_action=action.name)
            outcome = "Listed"
        else:
            _set_listing_status(unit, LISTING_DOCUMENTS_PENDING, _("Documents updated; listing approval still pending."))
            _save_owner_unit(unit)
            outcome = outcome or "Documents Updated"
            next_action = _next_action_payload(payload)

    else:
        frappe.throw(_("Unsupported Owner workflow action."), frappe.ValidationError)

    return outcome or "Completed", result_note, next_action


def _assert_not_reserved(unit, outcome):
    if unit.get("status") == "Reserved":
        frappe.throw(_("Cannot mark this Unit {0} while it is reserved by a buyer closing.").format(outcome), frappe.ValidationError)


def _mark_listed(unit, reason=None):
    _set_listing_status(unit, LISTING_LISTED, reason)
    unit.listed_on = unit.get("listed_on") or nowdate()
    unit.last_verified_on = nowdate()
    unit.next_verification_due = add_to_date(getdate(nowdate()), months=3)
    unit.verification_alert_sent_on = None


def _complete_availability_action(lead_doc, action, payload):
    unit = _owner_unit(action.unit, lead_doc.name)
    outcome = payload.get("outcome")
    note = payload.get("result_note")
    allowed = {"Still Available", "Temporarily Unavailable", "Re-price", "Withdrawn", "Sold Elsewhere"}
    if outcome not in allowed:
        frappe.throw(_("Select an availability verification result."), frappe.ValidationError)
    if outcome == "Still Available":
        _apply_unit_fields(unit, payload.get("updated_prices") or {})
        _mark_listed(unit, _("Availability confirmed by Owner."))
        _save_owner_unit(unit)
        next_action = _automatic_unit_successor(lead_doc, unit, source_action=action.name)
    elif outcome == "Temporarily Unavailable":
        next_due = payload.get("next_verification_due")
        if not next_due or getdate(next_due) <= getdate(nowdate()):
            frappe.throw(_("Temporarily Unavailable requires a future verification date."), frappe.ValidationError)
        _set_listing_status(unit, LISTING_OFF_MARKET, _("Temporarily unavailable. {0}").format(note or ""))
        unit.next_verification_due = getdate(next_due)
        unit.verification_alert_sent_on = None
        _save_owner_unit(unit)
        next_action = {"action_type": "Call", "purpose": "Availability Verification", "unit": unit.name, "scheduled_start": unit.next_verification_due}
    elif outcome == "Re-price":
        unit.pricing_method = "Agent Pricing"
        _set_listing_status(unit, LISTING_PRICING_REQUESTED, _("Owner requested repricing during availability verification."))
        _save_owner_unit(unit)
        next_action = {"action_type": "Agent Pricing", "purpose": "Price Proposal", "unit": unit.name, "scheduled_start": now_datetime()}
    else:
        _assert_not_reserved(unit, outcome)
        _set_listing_status(unit, LISTING_WITHDRAWN, _("{0}. {1}").format(outcome, note or ""))
        _save_owner_unit(unit)
        next_action = None
    return outcome, note, next_action


def _automatic_unit_successor(lead_doc, unit, source_action=None):
    status = unit.get("listing_status")
    mapping = {
        LISTING_PRICING_REQUESTED: ("Agent Pricing", "Price Proposal", now_datetime()),
        LISTING_PRICE_PROPOSED: ("Price Decision", "Price Decision", now_datetime()),
        LISTING_DOCUMENTS_PENDING: ("Listing Documents", "Documents Collection", now_datetime()),
        LISTING_LISTED: ("Call", "Availability Verification", unit.get("next_verification_due")),
        LISTING_VERIFICATION_DUE: ("Call", "Availability Verification", now_datetime()),
        LISTING_OFF_MARKET: ("Call", "Availability Verification", unit.get("next_verification_due")),
    }
    selected = mapping.get(status)
    if not selected:
        return None
    action_type, purpose, scheduled_start = selected
    return {"action_type": action_type, "purpose": purpose, "unit": unit.name, "scheduled_start": scheduled_start, "source_action": source_action}


def _next_action_payload(payload, fallback=None, required=False):
    data = payload.get("next_action")
    if data is None:
        data = fallback
    if not data:
        if required:
            frappe.throw(_("Select the next Owner workflow action before submitting."), frappe.ValidationError)
        return None
    data = _parse_object(data, _("Next action"))
    if not data.get("action_type") or not data.get("purpose"):
        frappe.throw(_("Next action type and purpose are mandatory."), frappe.ValidationError)
    _assert_action_definition(data["action_type"], data["purpose"])
    return data


def _create_successor(lead_doc, specification, source_action=None):
    if not specification:
        return None
    unit = specification.get("unit")
    starts = specification.get("scheduled_start") or now_datetime()
    return _create_owner_action(
        lead_doc,
        specification["action_type"],
        specification["purpose"],
        scheduled_start=starts,
        unit=unit,
        notes=specification.get("notes"),
        source_action=source_action or specification.get("source_action"),
        allow_existing=False,
    )


def _complete_action_record(action, outcome, result_note, payload, client_request_id=None):
    action.workflow_status = "Completed"
    action.started_at = action.get("started_at") or now_datetime()
    action.completed_at = now_datetime()
    action.completed_by = frappe.session.user
    action.outcome = outcome
    action.result_note = result_note
    action.result_data = json.dumps(payload)
    action.client_request_id = client_request_id
    action.save(ignore_permissions=True)
    _set_event_state(action, "Closed")


def _owner_origin_status(lead_doc):
    for fieldname in ("workflow_origin_status", "previous_status"):
        value = lead_doc.get(fieldname)
        if value in ("New", "Fresh Lead"):
            return value
    return lead_doc.get("status") if lead_doc.get("status") in ("New", "Fresh Lead") else "New"


def reconcile_owner_lead_rollup(lead, action_label="Owner listing workflow rollup"):
    """Derive only an Owner Lead's status from its owned Units and buyer facts."""
    if not frappe.db.exists("CRM Lead", lead):
        return None
    lead_doc = frappe.get_doc("CRM Lead", lead)
    if not is_owner_role(lead_doc.get("party_type")):
        return lead_doc.get("status")
    units = frappe.get_all(
        "Real Estate Unit",
        filters={"owner_lead": lead},
        fields=["name", "status", "sold_interest", "listing_status"],
        limit_page_length=0,
    )
    target = None
    if any(unit.status in ("Sold", "Rented") and unit.sold_interest for unit in units):
        target = "Closed Won"
    elif any(unit.status == "Reserved" for unit in units):
        target = "Closing"
    else:
        names = [unit.name for unit in units]
        negotiating = False
        if names and frappe.db.exists("DocType", "Lead Interest"):
            negotiating = bool(
                frappe.db.exists(
                    "Lead Interest",
                    {"unit": ["in", names], "is_active": 1, "workflow_status": ["in", list(NEGOTIATING_INTEREST_STATUSES)]},
                )
            )
        if negotiating:
            target = "Negotiating"
        elif any(unit.listing_status in (LISTING_LISTED, LISTING_VERIFICATION_DUE, LISTING_OFF_MARKET) for unit in units):
            target = OWNER_LEAD_STATUS_LISTED
        elif any(unit.listing_status == LISTING_DOCUMENTS_PENDING for unit in units):
            target = OWNER_LEAD_STATUS_DOCUMENTS
        elif any(unit.listing_status in (LISTING_PRICING_REQUESTED, LISTING_PRICE_PROPOSED) for unit in units):
            target = OWNER_LEAD_STATUS_PRICING
        else:
            target = _owner_origin_status(lead_doc)
    if lead_doc.get("status") != target:
        api = _api()
        api._set_lead_status(lead_doc, target, action_label)
        api._save_workflow_doc(lead_doc)
    return target


def refresh_unit_interest_counters(unit):
    """Refresh distinct Buyer-lead counts without making Buyer transitions fragile."""
    if not unit or not frappe.db.exists("DocType", "Lead Interest") or not frappe.db.exists("Real Estate Unit", unit):
        return {"active": 0, "total": 0}
    rows = frappe.db.sql(
        """
        SELECT
            COUNT(DISTINCT li.lead) AS total,
            COUNT(DISTINCT CASE WHEN li.is_active = 1 THEN li.lead END) AS active
        FROM `tabLead Interest` li
        INNER JOIN `tabCRM Lead` lead ON lead.name = li.lead
        WHERE li.unit = %s AND lead.party_type = 'Buyer'
        """,
        (unit,),
        as_dict=True,
    )
    row = rows[0] if rows else {}
    result = {"active": int(row.get("active") or 0), "total": int(row.get("total") or 0)}
    frappe.db.set_value(
        "Real Estate Unit",
        unit,
        {"active_buyer_interest_count": result["active"], "total_buyer_interest_count": result["total"]},
        update_modified=False,
    )
    owner = frappe.db.get_value("Real Estate Unit", unit, "owner_lead")
    if owner:
        reconcile_owner_lead_rollup(owner, "Buyer interest change on owned Unit")
    return result


def safely_refresh_unit_interest_counters(unit):
    """Never roll back an authoritative Buyer lifecycle operation on an insight failure."""
    if not unit:
        return
    savepoint = "owner_counter_refresh"
    try:
        frappe.db.savepoint(savepoint)
        refresh_unit_interest_counters(unit)
        frappe.db.release_savepoint(savepoint)
    except Exception:
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            pass
        frappe.log_error(frappe.get_traceback(), "Owner Unit buyer-interest counter refresh failed")


def _owner_action_definitions(lead_doc):
    if lead_doc.get("interest_status") == "Not Interested":
        return []
    if lead_doc.get("interest_status") != "Interested":
        return [{"action_type": "Call", "purpose": "Initial Qualification", "scope_type": "Lead"}]
    return [
        {"action_type": "Add Property", "purpose": "Listing Intake", "scope_type": "Lead"},
        {"action_type": "Call", "purpose": "General Follow-up", "scope_type": "Lead"},
        {"action_type": "Meeting", "purpose": "Explore Meeting", "scope_type": "Lead"},
    ]


def _unit_action_definitions(unit):
    if unit.get("status") != "Available":
        return []
    listing_status = unit.get("listing_status")
    mapping = {
        LISTING_PRICING_REQUESTED: [
            {"action_type": "Agent Pricing", "purpose": "Price Proposal"},
            {"action_type": "Call", "purpose": "Pricing Follow-up"},
        ],
        LISTING_PRICE_PROPOSED: [
            {"action_type": "Price Decision", "purpose": "Price Decision"},
            {"action_type": "Call", "purpose": "Pricing Follow-up"},
        ],
        LISTING_DOCUMENTS_PENDING: [
            {"action_type": "Listing Documents", "purpose": "Documents Collection"},
            {"action_type": "Call", "purpose": "General Follow-up"},
        ],
        LISTING_LISTED: [{"action_type": "Call", "purpose": "Availability Verification"}],
        LISTING_VERIFICATION_DUE: [{"action_type": "Call", "purpose": "Availability Verification"}],
        LISTING_OFF_MARKET: [{"action_type": "Call", "purpose": "Availability Verification"}],
    }
    values = mapping.get(listing_status, [])
    return [{**value, "scope_type": "Unit", "unit": unit.name} for value in values]


def _unit_card(unit, lead):
    action = _open_unit_action(unit.name)
    if action:
        action = _transition_due(action)
    return {
        "name": unit.name,
        "unit_number": unit.get("unit_number"),
        "sku": unit.get("sku"),
        "project": unit.get("project"),
        "listing_purpose": unit.get("listing_purpose"),
        "availability_status": unit.get("status"),
        "listing_status": unit.get("listing_status"),
        "pricing_method": unit.get("pricing_method"),
        "total_gross": unit.get("total_gross"),
        "rental_monthly_rate": unit.get("rental_monthly_rate"),
        "next_verification_due": unit.get("next_verification_due"),
        "active_buyer_interest_count": unit.get("active_buyer_interest_count") or 0,
        "total_buyer_interest_count": unit.get("total_buyer_interest_count") or 0,
        "current_action": _serialize_action(action),
        "allowed_actions": [] if action else _unit_action_definitions(unit),
        "listing_gate_errors": _listing_gate_errors(unit) if unit.get("listing_status") == LISTING_DOCUMENTS_PENDING else [],
    }


@frappe.whitelist()
def get_owner_workflow_context(lead):
    """Return the guided Owner Lead / owned Unit workflow context."""
    lead_doc = _owner_lead(lead, write=False)
    open_action = _open_lead_action(lead)
    if open_action:
        open_action = _transition_due(open_action)
    units = [
        _owner_unit(row.name, lead, write=False)
        for row in frappe.get_all("Real Estate Unit", filters={"owner_lead": lead}, pluck="name", order_by="modified desc", limit_page_length=0)
    ]
    return {
        "lead": lead,
        "lead_status": lead_doc.get("status"),
        "qualification": lead_doc.get("interest_status") or "Unknown",
        "current_action": _serialize_action(open_action),
        "allowed_actions": [] if open_action else _owner_action_definitions(lead_doc),
        "units": [_unit_card(unit, lead) for unit in units],
        "call_facts": {
            "no_answer_consecutive_count": _as_int(lead_doc.get("no_answer_consecutive_count")),
            "no_answer_total_count": _as_int(lead_doc.get("no_answer_total_count")),
            "last_call_outcome": lead_doc.get("last_call_outcome"),
            "last_call_at": lead_doc.get("last_call_at"),
        },
    }


@frappe.whitelist()
def create_owner_unit(lead, unit_data, pricing_method=None, client_request_id=None):
    """Create a full Resale Unit for an Owner from the guided Add Property step."""
    lead_doc = _owner_lead(lead)
    _lock_lead(lead)
    if client_request_id:
        previous = frappe.db.get_value(
            "Lead Action Execution",
            {"lead": lead, "client_request_id": client_request_id, "action_type": "Add Property"},
            "unit",
        )
        if previous:
            return {"unit": previous, "context": get_owner_workflow_context(lead), "idempotent": True}
    unit = _create_owner_unit(lead_doc, unit_data, pricing_method)
    action = frappe.get_doc(
        {
            "doctype": "Lead Action Execution",
            "lead": lead,
            "action_type": "Add Property",
            "purpose": "Listing Intake",
            "workflow_status": "Completed",
            "is_required": 0,
            "scope_type": "Unit",
            "scheduled_start": now_datetime(),
            "started_at": now_datetime(),
            "completed_at": now_datetime(),
            "completed_by": frappe.session.user,
            "pipeline_status_at_start": lead_doc.get("status"),
            "qualification_at_start": lead_doc.get("interest_status"),
            "unit": unit.name,
            "seller_lead": lead,
            "outcome": "Unit Added",
            "client_request_id": client_request_id,
        }
    )
    action.insert(ignore_permissions=True)
    successor = _create_successor(lead_doc, _automatic_unit_successor(lead_doc, unit, source_action=action.name), source_action=action.name)
    if successor:
        action.successor_action = successor.name
        action.save(ignore_permissions=True)
    _api()._add_lead_comment(lead_doc, _("Owner Unit added: {0}.").format(unit.name))
    reconcile_owner_lead_rollup(lead, "Owner Unit added")
    return {"unit": unit.name, "action": _serialize_action(action), "context": get_owner_workflow_context(lead)}


@frappe.whitelist()
def submit_owner_action(lead, payload, client_request_id=None, expected_modified=None):
    """Atomically record an Owner action and its result, then plan one successor."""
    lead_doc = _owner_lead(lead)
    _lock_lead(lead)
    payload = _parse_object(payload, _("Owner action payload"))
    if client_request_id:
        previous = frappe.get_all(
            "Lead Action Execution",
            filters={"lead": lead, "client_request_id": client_request_id},
            fields=["name", "workflow_status"],
            limit_page_length=1,
        )
        if previous and previous[0].workflow_status in OWNER_ACTION_TERMINAL_STATUSES:
            return {"action": _serialize_action(_action_doc(previous[0].name, lead)), "context": get_owner_workflow_context(lead), "idempotent": True}
    action_name = payload.get("action_name")
    if action_name:
        action = _action_doc(action_name, lead)
        if not _is_owner_action(action):
            frappe.throw(_("This action cannot be completed as an Owner workflow action."), frappe.ValidationError)
        if expected_modified and str(action.modified) != str(expected_modified):
            frappe.throw(_("This action was changed by another user. Reload the Owner lead."), frappe.ValidationError)
        if action.workflow_status not in OWNER_ACTION_OPEN_STATUSES:
            frappe.throw(_("This Owner action cannot accept a result in its current state."), frappe.ValidationError)
    else:
        action_type = payload.get("action_type")
        purpose = payload.get("purpose")
        unit = payload.get("unit")
        _assert_action_definition(action_type, purpose)
        allowed = _unit_action_definitions(_owner_unit(unit, lead)) if unit else _owner_action_definitions(lead_doc)
        if not any(item["action_type"] == action_type and item["purpose"] == purpose for item in allowed):
            frappe.throw(_("This action is not allowed at the current Owner workflow stage."), frappe.PermissionError)
        action = _create_owner_action(lead_doc, action_type, purpose, now_datetime(), unit=unit, notes=payload.get("notes"))
    if action.get("scope_type") == "Lead" and _open_lead_action(lead).name != action.name:
        frappe.throw(_("This is not the current Owner lead action."), frappe.ValidationError)
    if action.get("scope_type") == "Unit" and _open_unit_action(action.unit).name != action.name:
        frappe.throw(_("This is not the current Owner Unit action."), frappe.ValidationError)
    action.workflow_status = "In Progress"
    action.started_at = action.get("started_at") or now_datetime()
    action.save(ignore_permissions=True)
    if action.action_type == "Call" and action.purpose == "Availability Verification":
        outcome, result_note, successor_spec = _complete_availability_action(lead_doc, action, payload)
    else:
        outcome, result_note, successor_spec = _complete_owner_action(lead_doc, action, payload)
    _complete_action_record(action, outcome, result_note, payload, client_request_id)
    successor = _create_successor(lead_doc, successor_spec, source_action=action.name)
    if successor:
        action.successor_action = successor.name
        action.save(ignore_permissions=True)
    _api()._add_lead_comment(lead_doc, _("Owner workflow action completed: {0} — {1}.").format(action.action_type, outcome))
    _api()._save_workflow_doc(lead_doc)
    reconcile_owner_lead_rollup(lead, _("Owner action: {0}").format(action.action_type))
    return {"action": _serialize_action(action), "successor_action": _serialize_action(successor), "context": get_owner_workflow_context(lead)}


@frappe.whitelist()
def reschedule_owner_action(lead, action_name, scheduled_start, reason=None):
    """Close an Owner action as rescheduled and create exactly one replacement."""
    lead_doc = _owner_lead(lead)
    _lock_lead(lead)
    action = _action_doc(action_name, lead)
    if not _is_owner_action(action) or action.workflow_status not in OWNER_ACTION_OPEN_STATUSES:
        frappe.throw(_("Only an open Owner action can be rescheduled."), frappe.ValidationError)
    starts = get_datetime(scheduled_start)
    if starts <= now_datetime():
        frappe.throw(_("The rescheduled time must be in the future."), frappe.ValidationError)
    action.workflow_status = "Rescheduled"
    action.completed_at = now_datetime()
    action.completed_by = frappe.session.user
    action.outcome = "Rescheduled"
    action.result_note = reason
    action.save(ignore_permissions=True)
    _set_event_state(action, "Closed")
    successor = _create_owner_action(
        lead_doc,
        action.action_type,
        action.purpose,
        starts,
        unit=action.get("unit"),
        notes=reason,
        source_action=action.name,
    )
    action.successor_action = successor.name
    action.save(ignore_permissions=True)
    return {"action": _serialize_action(action), "successor_action": _serialize_action(successor), "context": get_owner_workflow_context(lead)}


@frappe.whitelist()
def reactivate_owner_unit(unit, reason):
    """Manager-only restart for a Withdrawn Owner Unit; never reopens a buyer closing."""
    if not _is_manager():
        frappe.throw(_("Only Sales Manager or System Manager can reactivate a withdrawn Owner Unit."), frappe.PermissionError)
    unit_doc = _owner_unit(unit)
    _lock_unit(unit)
    if unit_doc.get("status") in ("Reserved", "Sold", "Rented"):
        frappe.throw(_("A reserved, sold, or rented Unit cannot be reactivated as an Owner listing."), frappe.ValidationError)
    if unit_doc.get("listing_status") != LISTING_WITHDRAWN:
        frappe.throw(_("Only a Withdrawn Owner Unit can be reactivated."), frappe.ValidationError)
    if not str(reason or "").strip():
        frappe.throw(_("Record the reason for reactivating this Unit."), frappe.ValidationError)
    target = LISTING_DOCUMENTS_PENDING if _listing_price_present(unit_doc) else LISTING_PRICING_REQUESTED
    _set_listing_status(unit_doc, target, _("Reactivated by {0}: {1}").format(frappe.session.user, reason))
    _save_owner_unit(unit_doc)
    lead_doc = _owner_lead(unit_doc.owner_lead)
    successor = _create_successor(lead_doc, _automatic_unit_successor(lead_doc, unit_doc))
    reconcile_owner_lead_rollup(lead_doc.name, "Owner Unit reactivated")
    return {"unit": unit_doc.name, "successor_action": _serialize_action(successor), "context": get_owner_workflow_context(lead_doc.name)}


def unit_is_buyer_eligible(unit):
    """Authoritative visibility gate for a Unit owned by an Owner Lead.

    Blank lifecycle status is deliberately allowed for historical/primary data.
    New Owner Units receive a nonblank status before they can be selected.
    """
    if not unit:
        return False
    if isinstance(unit, str):
        unit = frappe.db.get_value(
            "Real Estate Unit", unit, ["owner_lead", "listing_status", "next_verification_due"], as_dict=True
        )
    if not unit or not unit.get("owner_lead"):
        return True
    status = unit.get("listing_status")
    if not status:
        return True
    if status == LISTING_LISTED:
        return True
    if status != LISTING_VERIFICATION_DUE:
        return False
    due = unit.get("next_verification_due")
    # Grace period intentionally applies only while the agent resolves a due
    # verification; an overdue stale listing is never matched to new Buyers.
    return bool(due and getdate(due) >= add_to_date(getdate(nowdate()), days=-14))


@frappe.whitelist()
def get_unit_insights(unit):
    """Return view and Buyer-interest metrics for the Unit form."""
    unit_doc = _owner_unit(unit, write=False) if frappe.db.get_value("Real Estate Unit", unit, "owner_lead") else frappe.get_doc("Real Estate Unit", unit)
    refresh_unit_interest_counters(unit_doc.name)
    views = 0
    if frappe.db.exists("DocType", "View Log"):
        views = frappe.db.count("View Log", {"reference_doctype": "Real Estate Unit", "reference_name": unit_doc.name})
    return {
        "unit": unit_doc.name,
        "view_count": views,
        "active_buyer_interest_count": frappe.db.get_value("Real Estate Unit", unit_doc.name, "active_buyer_interest_count") or 0,
        "total_buyer_interest_count": frappe.db.get_value("Real Estate Unit", unit_doc.name, "total_buyer_interest_count") or 0,
        "listing_status": unit_doc.get("listing_status"),
        "next_verification_due": unit_doc.get("next_verification_due"),
    }


@frappe.whitelist()
def log_unit_view(unit):
    """Persist a unique current-user Unit view for CRM portal reads."""
    if not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Unit {0} was not found.").format(unit), frappe.DoesNotExistError)
    doc = frappe.get_doc("Real Estate Unit", unit)
    if frappe.session.user != "Administrator" and not frappe.has_permission("Real Estate Unit", ptype="read", doc=doc):
        frappe.throw(_("You do not have permission to view this Unit."), frappe.PermissionError)
    doc.add_viewed(unique_views=True)
    return get_unit_insights(unit)


def _ensure_verification_action(unit):
    if unit.get("status") != "Available" or unit.get("listing_status") not in (LISTING_LISTED, LISTING_VERIFICATION_DUE, LISTING_OFF_MARKET):
        return None
    if not unit.get("next_verification_due"):
        return None
    lead_doc = _owner_lead(unit.owner_lead, write=False)
    existing = _open_unit_action(unit.name)
    if existing:
        return existing
    return _create_owner_action(
        lead_doc,
        "Call",
        "Availability Verification",
        scheduled_start=unit.next_verification_due,
        unit=unit.name,
        notes=_("Three-month Owner availability and Unit details validation."),
    )


def run_daily_listing_verification():
    """Move due listings to Verification Due and create one idempotent calendar action."""
    if not frappe.db.exists("DocType", "Real Estate Unit") or not frappe.db.exists("DocType", "Lead Action Execution"):
        return {"due": 0, "actions": 0, "errors": 0}
    due = actions = errors = 0
    units = frappe.get_all(
        "Real Estate Unit",
        filters={"owner_lead": ["is", "set"], "status": "Available", "listing_status": ["in", [LISTING_LISTED, LISTING_VERIFICATION_DUE, LISTING_OFF_MARKET]]},
        pluck="name",
        limit_page_length=0,
    )
    for index, name in enumerate(units):
        savepoint = "owner_verify_{0}".format(index)
        try:
            frappe.db.savepoint(savepoint)
            _lock_unit(name)
            unit = _owner_unit(name, write=False)
            next_due = unit.get("next_verification_due")
            if not next_due:
                frappe.db.release_savepoint(savepoint)
                continue
            if getdate(next_due) <= getdate(nowdate()) and unit.get("listing_status") == LISTING_LISTED:
                with unit_mutation_context("daily_verification"):
                    unit.listing_status = LISTING_VERIFICATION_DUE
                    unit.listing_status_reason = _("Availability verification is due.")
                    unit.save(ignore_permissions=True)
                due += 1
                _api()._add_lead_comment(
                    _owner_lead(unit.owner_lead, write=False),
                    _("Availability verification is due for Unit {0}.").format(unit.name),
                )
            action = _ensure_verification_action(unit)
            if action:
                actions += 1
            if unit.get("listing_status") == LISTING_VERIFICATION_DUE and getdate(next_due) < add_to_date(getdate(nowdate()), days=-7) and not unit.get("verification_alert_sent_on"):
                # A persistent in-document marker makes the escalation idempotent;
                # the comment is visible to managers without silently changing availability.
                with unit_mutation_context("daily_verification"):
                    unit.verification_alert_sent_on = nowdate()
                    unit.save(ignore_permissions=True)
                _api()._add_lead_comment(
                    _owner_lead(unit.owner_lead, write=False),
                    _("Manager attention: Unit {0} availability verification is overdue by more than seven days.").format(unit.name),
                )
            frappe.db.release_savepoint(savepoint)
        except Exception:
            errors += 1
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                pass
            frappe.log_error(frappe.get_traceback(), "Daily Owner listing verification failed")
    return {"due": due, "actions": actions, "errors": errors}


def reconcile_owner_workflow():
    """Idempotent after-migrate reconciliation: counters, Owner statuses, and due actions."""
    if not frappe.db.exists("DocType", "Real Estate Unit"):
        return {"units": 0, "owners": 0}
    units = frappe.get_all("Real Estate Unit", filters={"owner_lead": ["is", "set"]}, pluck="name", limit_page_length=0)
    owners = set()
    for index, unit in enumerate(units):
        savepoint = "owner_reconcile_{0}".format(index)
        try:
            frappe.db.savepoint(savepoint)
            refresh_unit_interest_counters(unit)
            owner = frappe.db.get_value("Real Estate Unit", unit, "owner_lead")
            if owner:
                owners.add(owner)
            frappe.db.release_savepoint(savepoint)
        except Exception:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                pass
            frappe.log_error(frappe.get_traceback(), "Owner workflow reconciliation failed")
    for owner in owners:
        try:
            reconcile_owner_lead_rollup(owner, "Owner workflow reconciliation")
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Owner lead rollup reconciliation failed")
    return {"units": len(units), "owners": len(owners)}
