import frappe
from frappe import _
from frappe.utils import now_datetime


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

    unit = frappe.get_doc(
        {
            "doctype": "Real Estate Unit",
            "project": project,
            "unit_number": unit_number,
            "unit_type": "Resale",
            "status": "Available",
            "price": price,
            "owner_lead": owner_lead,
        }
    )
    unit.insert()
    return unit.as_dict()


def _validate_buyer_lead(lead_doc):
    if lead_doc.get("party_type") and lead_doc.get("party_type") != "Buyer":
        frappe.throw(_("Only Buyer leads can use buyer interest actions."), frappe.ValidationError)


def _validate_buyer_interest_unit(lead_doc, unit):
    if not frappe.db.exists("Real Estate Unit", unit):
        frappe.throw(_("Real Estate Unit {0} was not found.").format(unit), frappe.DoesNotExistError)

    status = frappe.db.get_value("Real Estate Unit", unit, "status")
    if status != "Available":
        frappe.throw(_("Only Available units can be linked as interested properties."), frappe.ValidationError)

    _validate_buyer_lead(lead_doc)


LEAD_NO_ANSWER_STATUS = "No Answer"
LEAD_ANSWERED_STATUS = "Contacted"


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

    doc = frappe.get_doc(
        {
            "doctype": "CRM Lead Status",
            "lead_status": status,
            "type": status_type,
            "color": color,
            "position": position,
        }
    )
    doc.insert(ignore_permissions=True)


def _set_lead_status(doc, status):
    if not status:
        return
    _ensure_lead_status(status, status_type="Ongoing", color="orange" if status == LEAD_NO_ANSWER_STATUS else "blue")
    doc.status = status


def _get_user_doc(user):
    if not user or not frappe.db.exists("User", user):
        return None
    return frappe.get_doc("User", user)


def _get_user_outreach_email(user_doc):
    if not user_doc:
        return None

    email = user_doc.get("real_estate_agent_outreach_email") or user_doc.get("email")
    if email:
        return email

    for row in user_doc.get("user_emails") or []:
        if row.get("email_id"):
            return row.get("email_id")
    return None


def _get_user_whatsapp_number(user_doc):
    if not user_doc:
        return None
    return (
        user_doc.get("real_estate_agent_whatsapp_number")
        or user_doc.get("mobile_no")
        or user_doc.get("phone")
    )


def _resolve_assigned_agent_identity(lead_doc):
    user = lead_doc.get("lead_owner") or frappe.session.user
    user_doc = _get_user_doc(user)

    return frappe._dict(
        {
            "user": user,
            "full_name": user_doc.get("full_name") if user_doc else user,
            "email": _get_user_outreach_email(user_doc),
            "whatsapp_number": _get_user_whatsapp_number(user_doc),
        }
    )


def _lead_contact_number(lead_doc):
    return lead_doc.get("whatsapp_number") or lead_doc.get("mobile_no") or lead_doc.get("phone")


def _lead_email(lead_doc):
    return lead_doc.get("email") or lead_doc.get("email_id")


def _add_lead_comment(lead_doc, text):
    try:
        lead_doc.add_comment("Comment", text=text)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Real Estate Lead Action Comment Failed")


def _create_manual_call_log(lead_doc, outcome, agent_identity, note=None):
    if not frappe.db.exists("DocType", "CRM Call Log"):
        return None

    timestamp = now_datetime()
    status = "No Answer" if outcome == "no_answer" else "Completed"
    call_log = frappe.get_doc(
        {
            "doctype": "CRM Call Log",
            "from": agent_identity.get("whatsapp_number") or agent_identity.get("email") or agent_identity.get("user") or "Manual",
            "to": _lead_contact_number(lead_doc) or "Unknown",
            "type": "Outgoing",
            "status": status,
            "caller": agent_identity.get("user"),
            "reference_doctype": "CRM Lead",
            "reference_docname": lead_doc.name,
            "medium": "Manual CRM Action",
            "start_time": timestamp,
            "end_time": timestamp,
            "duration": 0,
        }
    )
    call_log.insert(ignore_permissions=True)
    if hasattr(call_log, "link_with_reference_doc"):
        call_log.link_with_reference_doc("CRM Lead", lead_doc.name)
        call_log.save(ignore_permissions=True)
    return call_log.name


def _call_action_response(doc, agent_identity=None, call_log=None):
    return {
        "name": doc.name,
        "status": doc.get("status"),
        "no_answer_first_call": doc.get("no_answer_first_call") or 0,
        "no_answer_second_call": doc.get("no_answer_second_call") or 0,
        "no_answer_consecutive_count": doc.get("no_answer_consecutive_count") or 0,
        "no_answer_total_count": doc.get("no_answer_total_count") or 0,
        "last_call_outcome": doc.get("last_call_outcome"),
        "last_call_at": doc.get("last_call_at"),
        "assigned_agent": agent_identity or _resolve_assigned_agent_identity(doc),
        "call_log": call_log,
    }


@frappe.whitelist()
def get_assigned_agent_identity(lead):
    doc = _get_lead_doc(lead)
    return _resolve_assigned_agent_identity(doc)


@frappe.whitelist()
def record_lead_call_outcome(lead, outcome, note=None):
    doc = _get_lead_doc(lead)
    outcome_key = (outcome or "").strip().lower().replace("-", "_").replace(" ", "_")
    if outcome_key not in {"answered", "no_answer"}:
        frappe.throw(_("Call outcome must be Answered or No Answer."), frappe.ValidationError)

    agent_identity = _resolve_assigned_agent_identity(doc)
    current_time = now_datetime()

    if outcome_key == "no_answer":
        streak = _to_int(doc.get("no_answer_consecutive_count")) + 1
        total = _to_int(doc.get("no_answer_total_count")) + 1
        doc.set("no_answer_consecutive_count", streak)
        doc.set("no_answer_total_count", total)
        doc.set("no_answer_first_call", 1 if streak >= 1 else 0)
        doc.set("no_answer_second_call", 1 if streak >= 2 else 0)
        doc.set("last_call_outcome", "No Answer")
        _set_lead_status(doc, LEAD_NO_ANSWER_STATUS)
    else:
        streak = 0
        total = _to_int(doc.get("no_answer_total_count"))
        doc.set("no_answer_consecutive_count", 0)
        doc.set("no_answer_first_call", 0)
        doc.set("no_answer_second_call", 0)
        doc.set("last_call_outcome", "Answered")
        _set_lead_status(doc, LEAD_ANSWERED_STATUS)

    doc.set("last_call_at", current_time)
    doc.save(ignore_permissions=True)

    call_log = _create_manual_call_log(doc, outcome_key, agent_identity, note=note)
    action_text = _("No-answer call recorded") if outcome_key == "no_answer" else _("Answered call recorded")
    comment = (
        f"{action_text}. "
        f"Current no-answer streak: {streak}. "
        f"Total no-answer count: {total}. "
        f"Assigned agent: {agent_identity.get('full_name') or agent_identity.get('user')}."
    )
    if note:
        comment = f"{comment} Note: {note}"
    _add_lead_comment(doc, comment)

    return _call_action_response(doc, agent_identity=agent_identity, call_log=call_log)


@frappe.whitelist()
def record_no_answer_attempt(lead, attempt_number=None):
    return record_lead_call_outcome(lead=lead, outcome="No Answer")


def _create_whatsapp_message(doc, message):
    if not frappe.db.exists("DocType", "WhatsApp Message"):
        frappe.throw(_("WhatsApp Message DocType is not installed."), frappe.ValidationError)

    to_number = _lead_contact_number(doc)
    if not to_number:
        frappe.throw(_("The lead does not have a WhatsApp or mobile number."), frappe.ValidationError)

    whatsapp_message = frappe.get_doc(
        {
            "doctype": "WhatsApp Message",
            "reference_doctype": "CRM Lead",
            "reference_name": doc.name,
            "message": message,
            "to": to_number,
            "content_type": "text",
        }
    )
    whatsapp_message.insert(ignore_permissions=True)
    return whatsapp_message.name


def _send_email_message(doc, agent_identity, subject, message):
    recipient = _lead_email(doc)
    if not recipient:
        frappe.throw(_("The lead does not have an email address."), frappe.ValidationError)

    make_email = frappe.get_attr("frappe.core.doctype.communication.email.make")
    communication = make_email(
        recipients=recipient,
        sender=agent_identity.get("email"),
        sender_full_name=agent_identity.get("full_name"),
        subject=subject or _("Follow up for {0}").format(doc.get("lead_name") or doc.name),
        content=message,
        doctype="CRM Lead",
        name=doc.name,
        send_email=1,
    )
    return communication.name if hasattr(communication, "name") else communication


@frappe.whitelist()
def record_lead_outreach_action(lead, channel, message=None, subject=None, send=0):
    doc = _get_lead_doc(lead)
    channel_key = (channel or "").strip().lower()
    if channel_key not in {"whatsapp", "email"}:
        frappe.throw(_("Outreach channel must be WhatsApp or Email."), frappe.ValidationError)

    agent_identity = _resolve_assigned_agent_identity(doc)
    if channel_key == "whatsapp" and not agent_identity.get("whatsapp_number"):
        frappe.throw(_("The assigned agent does not have a WhatsApp number configured."), frappe.ValidationError)
    if channel_key == "email" and not agent_identity.get("email"):
        frappe.throw(_("The assigned agent does not have an outreach email configured."), frappe.ValidationError)

    message = message or _("Follow-up action from the real-estate CRM.")
    external_record = None
    if frappe.utils.cint(send):
        if channel_key == "whatsapp":
            external_record = _create_whatsapp_message(doc, message)
        else:
            external_record = _send_email_message(doc, agent_identity, subject, message)

    title = _("WhatsApp action sent") if channel_key == "whatsapp" and external_record else _("WhatsApp action recorded")
    if channel_key == "email":
        title = _("Email action sent") if external_record else _("Email action recorded")

    _add_lead_comment(
        doc,
        f"{title}. Source: "
        f"{agent_identity.get('whatsapp_number') if channel_key == 'whatsapp' else agent_identity.get('email')}. "
        f"Details: {message}",
    )

    return {
        "name": doc.name,
        "channel": "WhatsApp" if channel_key == "whatsapp" else "Email",
        "assigned_agent": agent_identity,
        "sent": bool(external_record),
        "external_record": external_record,
    }


@frappe.whitelist()
def add_interest_request(lead, request_notes, request_status="Open"):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)

    doc = frappe.get_doc("CRM Lead", lead)
    _validate_buyer_lead(doc)

    request_notes = (request_notes or "").strip()
    if not request_notes:
        frappe.throw(_("Request notes are required."), frappe.ValidationError)

    request_status = request_status or "Open"
    if request_status not in {"Open", "Fulfilled", "Cancelled"}:
        frappe.throw(_("Request status must be Open, Fulfilled, or Cancelled."), frappe.ValidationError)

    doc.append(
        "interested_in_units",
        {
            "doctype": "Lead Interested Unit",
            "interest_record_type": "Request",
            "request_status": request_status,
            "request_notes": request_notes,
        },
    )
    doc.save()
    return doc.as_dict()


@frappe.whitelist()
def link_interested_unit(lead, unit):
    return link_interested_units(lead, [unit])


@frappe.whitelist()
def link_interested_units(lead, units):
    if not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("Lead {0} was not found.").format(lead), frappe.DoesNotExistError)

    if isinstance(units, str):
        units = frappe.parse_json(units)

    normalized_units = []
    for unit in units or []:
        if isinstance(unit, dict):
            unit = unit.get("unit") or unit.get("value") or unit.get("name") or unit.get("real_estate_unit")
        if unit:
            normalized_units.append(unit)

    if not normalized_units:
        frappe.throw(_("Select at least one inventory unit."), frappe.ValidationError)

    doc = frappe.get_doc("CRM Lead", lead)
    existing_units = {row.unit for row in doc.get("interested_in_units") or [] if row.unit}
    added = 0

    for unit in dict.fromkeys(normalized_units):
        _validate_buyer_interest_unit(doc, unit)
        if unit in existing_units:
            continue
        doc.append(
            "interested_in_units",
            {
                "doctype": "Lead Interested Unit",
                "interest_record_type": "Inventory Unit",
                "unit": unit,
            },
        )
        existing_units.add(unit)
        added += 1

    if added:
        doc.save()

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
        frappe.throw(
            _("Unit {0} is already assigned to seller lead {1}.").format(unit, unit_doc.get("owner_lead")),
            frappe.ValidationError,
        )

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
        rows = frappe.get_all(
            "Real Estate Unit",
            filters={"name": ["in", list(names)]},
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
        )

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
        rows.append(
            frappe._dict(
                {
                    "name": row.name or f"request-{index}",
                    "sku": _("Request"),
                    "interest_record_type": "Request",
                    "request_status": row.get("request_status") or "Open",
                    "request_notes": row.get("request_notes"),
                    "relationship": _("Interest Request"),
                    "proposal_status": None,
                    "owner_lead": None,
                    "modified": row.modified,
                }
            )
        )

    return rows


@frappe.whitelist()
def get_available_units_for_selection(lead=None):
    """Return available inventory units with key details for the unit selection popup.

    Optionally filters out units already linked to the given lead.
    Returns SKU, project, developer, unit_type, floor, finishing_type, price, and status.
    """
    filters = {"status": "Available"}
    fields = [
        "name",
        "sku",
        "project",
        "developer",
        "unit_type",
        "floor",
        "finishing_type",
        "status",
        "price",
    ]

    units = frappe.get_all(
        "Real Estate Unit",
        filters=filters,
        fields=fields,
        order_by="modified desc",
        limit_page_length=200,
    )

    # Exclude units already linked to this lead
    if lead and frappe.db.exists("CRM Lead", lead):
        lead_doc = frappe.get_doc("CRM Lead", lead)
        already_linked = {row.unit for row in lead_doc.get("interested_in_units") or [] if row.unit}
        units = [u for u in units if u.name not in already_linked]

    return units
