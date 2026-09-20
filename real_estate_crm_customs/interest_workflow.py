"""Standalone Lead Interest lifecycle and migration services.

The legacy CRM Lead child table remains a read-only mirror during the transition.
New workflow code uses Lead Interest records and relational action scopes.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from real_estate_crm_customs.migration_policy import (
    LegacyInterestDataError,
    normalize_legacy_interest_binding,
    parse_legacy_scope_identifiers,
)


LEAD_STATUS_NEW = "New"
LEAD_STATUS_FRESH = "Fresh Lead"
LEAD_STATUS_REQUESTED = "Requested"
LEAD_STATUS_OFFER_SENT = "Offer Sent"
LEAD_STATUS_NEGOTIATING = "Negotiating"
LEAD_STATUS_OFFER_SELECTED = "Offer Selected"

INTEREST_STATUSES = (
    "Requested",
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
    "Fulfilled",
    "Cancelled",
)
TERMINAL_INTEREST_STATUSES = {"Rejected", "Superseded", "Fulfilled", "Cancelled"}
ACTION_OPEN_STATUSES = ("Planned", "Due", "In Progress")

ALLOWED_TRANSITIONS = {
    "Requested": {"Matched", "Superseded", "Fulfilled", "Cancelled"},
    "Matched": {"Offer Sent", "Superseded", "Cancelled"},
    "Offer Sent": {"Offer Viewed", "Offer Accepted", "Rejected", "Superseded"},
    "Offer Viewed": {"Offer Accepted", "Rejected", "Superseded"},
    "Offer Accepted": {"Negotiating", "Showing Scheduled", "Rejected", "Superseded"},
    "Negotiating": {"Showing Scheduled", "Rejected", "Superseded"},
    "Showing Scheduled": {"Shown - Interested", "Shown - Considering", "Negotiating", "Rejected"},
    "Shown - Considering": {"Negotiating", "Showing Scheduled", "Shown - Interested", "Rejected"},
    "Shown - Interested": {"Negotiating", "Showing Scheduled", "Rejected"},
    "Rejected": set(),
    "Superseded": set(),
    "Fulfilled": set(),
    "Cancelled": set(),
}

STATUS_RANK = {
    "Requested": 1,
    "Matched": 0,
    "Offer Sent": 2,
    "Offer Viewed": 2,
    "Offer Accepted": 3,
    "Negotiating": 3,
    "Showing Scheduled": 3,
    "Shown - Considering": 3,
    "Shown - Interested": 4,
    "Rejected": -1,
    "Superseded": -1,
    "Fulfilled": -1,
    "Cancelled": -1,
}

LEGACY_COPY_FIELDS = (
    "request_status",
    "request_notes",
    "requested_destination",
    "requested_area",
    "requested_unit_type",
    "requested_budget",
    "requested_project",
    "requested_developer",
    "requested_finishing_type",
    "requested_delivery_time",
    "outsource_company",
    "outsource_broker_name",
    "outsource_broker_number",
    "outsource_unit_details",
    "international_type",
    "international_country",
    "international_details",
    "deletion_request_status",
    "deletion_request",
)


def is_available():
    return frappe.db.exists("DocType", "Lead Interest")


def parse_names(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(item for item in value if item))


def derive_legacy_status(row):
    if row.get("unit_interest_status") == "Lost Interest" or row.get("proposal_status") == "Rejected":
        return "Rejected"
    record_type = row.get("interest_record_type") or ("Inventory Unit" if row.get("unit") else "Request")
    if record_type in ("Request", "International"):
        request_status = row.get("request_status")
        if request_status == "Fulfilled":
            return "Fulfilled"
        if request_status == "Cancelled":
            return "Cancelled"
        return "Requested"
    proposal_status = row.get("proposal_status")
    if proposal_status == "Offer Accepted":
        return "Offer Accepted"
    if proposal_status == "Viewed":
        return "Offer Viewed"
    if row.get("offer_sent") or proposal_status == "Sent":
        return "Offer Sent"
    return "Matched"


def _origin_status(lead_doc):
    for fieldname in ("workflow_origin_status", "previous_status"):
        value = lead_doc.get(fieldname)
        if value in (LEAD_STATUS_NEW, LEAD_STATUS_FRESH):
            return value
    return lead_doc.get("status") or LEAD_STATUS_NEW


def _legacy_interest_values(lead_doc, row):
    unit = row.get("unit")
    unit_inventory_type = (
        frappe.db.get_value("Real Estate Unit", unit, "inventory_type")
        if unit and frappe.db.exists("Real Estate Unit", unit)
        else None
    )
    binding = normalize_legacy_interest_binding(
        record_type=row.get("interest_record_type"),
        category=row.get("interest_category"),
        unit=unit,
        unit_inventory_type=unit_inventory_type,
    )
    values = {
        "doctype": "Lead Interest",
        "lead": lead_doc.name,
        "record_type": binding["record_type"],
        "category": binding["category"],
        "workflow_status": derive_legacy_status(row),
        "unit": unit,
        "origin_lead_status": _origin_status(lead_doc),
        "offer_sent_at": row.get("offer_sent_at"),
        "legacy_child_row": row.name,
        "is_active": 0 if derive_legacy_status(row) in TERMINAL_INTEREST_STATUSES else 1,
        "last_transition_at": row.get("modified") or lead_doc.get("modified") or now_datetime(),
        "last_transition_by": row.get("modified_by") or lead_doc.get("modified_by") or frappe.session.user,
    }
    for fieldname in LEGACY_COPY_FIELDS:
        values[fieldname] = row.get(fieldname)
    if values["record_type"] == "Request" and not values.get("request_status"):
        values["request_status"] = "Open"
    return values


def ensure_interest_for_legacy_row(lead_doc, row, update_existing=False):
    if not is_available() or not row.name:
        return None
    existing_name = frappe.db.get_value("Lead Interest", {"legacy_child_row": row.name}, "name")
    values = _legacy_interest_values(lead_doc, row)
    if existing_name:
        interest = frappe.get_doc("Lead Interest", existing_name)
        if update_existing:
            for fieldname, value in values.items():
                if fieldname in ("doctype", "workflow_status", "is_active", "last_transition_at", "last_transition_by"):
                    continue
                interest.set(fieldname, value)
            interest.save(ignore_permissions=True)
        return interest
    interest = frappe.get_doc(values)
    interest.insert(ignore_permissions=True)
    create_transition(
        interest,
        from_status=None,
        to_status=interest.workflow_status,
        reason="Migrated from CRM Lead child interest row",
        allow_same=True,
    )
    return interest


def create_interest(lead_doc, values, reason="Standalone Lead Interest created"):
    """Create the authoritative Interest first, then generate its legacy mirror."""
    payload = dict(values or {})
    payload.update({"doctype": "Lead Interest", "lead": lead_doc.name})
    payload.setdefault("origin_lead_status", _origin_status(lead_doc))
    payload.setdefault("workflow_status", "Matched")
    payload.setdefault("is_active", 1)
    payload.setdefault("last_transition_at", now_datetime())
    payload.setdefault("last_transition_by", frappe.session.user)
    interest = frappe.get_doc(payload)
    interest.insert(ignore_permissions=True)
    create_transition(
        interest,
        from_status=None,
        to_status=interest.workflow_status,
        reason=reason,
        allow_same=True,
    )
    mirror_interest_to_legacy(interest)
    return interest


def _record_migration_issue(stage, source_doctype, source_name, exc):
    issue = {
        "stage": stage,
        "source_doctype": source_doctype,
        "source_name": source_name,
        "error": str(exc),
    }
    message = (
        f"Stage: {stage}\nSource: {source_doctype} {source_name}\n"
        f"Error: {exc}\n\nThe source record was preserved and skipped."
    )
    try:
        frappe.log_error(
            title=f"Real-estate migration skipped {source_doctype}"[:140],
            message=message,
        )
    except Exception:
        print(message)
    return issue


def migrate_legacy_lead_interests():
    if not is_available() or not frappe.db.exists("DocType", "CRM Lead"):
        return {"created": 0, "existing": 0, "skipped": 0, "issues": []}
    created = 0
    existing = 0
    issues = []
    lead_names = frappe.get_all("CRM Lead", pluck="name", limit_page_length=0)
    for lead_name in lead_names:
        lead_doc = frappe.get_doc("CRM Lead", lead_name)
        if lead_doc.meta.has_field("workflow_origin_status") and not lead_doc.get("workflow_origin_status"):
            frappe.db.set_value(
                "CRM Lead",
                lead_doc.name,
                "workflow_origin_status",
                _origin_status(lead_doc),
                update_modified=False,
            )
            lead_doc.workflow_origin_status = _origin_status(lead_doc)
        rows = lead_doc.get("interested_in_units") or []
        if rows and lead_doc.get("party_type") != "Buyer":
            for row in rows:
                issues.append(
                    _record_migration_issue(
                        "interest promotion",
                        "Lead Interested Unit",
                        row.name,
                        LegacyInterestDataError(
                            f"Parent Lead {lead_doc.name} is not a canonical Buyer"
                        ),
                    )
                )
            continue
        for index, row in enumerate(rows):
            savepoint = f"legacy_interest_{index}"
            frappe.db.savepoint(savepoint)
            try:
                existed = frappe.db.exists("Lead Interest", {"legacy_child_row": row.name})
                ensure_interest_for_legacy_row(lead_doc, row, update_existing=False)
            except Exception as exc:
                frappe.db.rollback(save_point=savepoint)
                issues.append(
                    _record_migration_issue(
                        "interest promotion",
                        "Lead Interested Unit",
                        row.name,
                        exc,
                    )
                )
                continue
            finally:
                try:
                    frappe.db.release_savepoint(savepoint)
                except Exception:
                    pass
            if existed:
                existing += 1
            else:
                created += 1
    return {
        "created": created,
        "existing": existing,
        "skipped": len(issues),
        "issues": issues,
    }


def reconcile_existing_legacy_interest_bindings():
    """Repair target-only bindings from canonical Units; never rewrite legacy sources."""
    if not is_available():
        return {"updated": 0, "skipped": 0, "normalized": [], "issues": []}
    updated = 0
    normalized = []
    issues = []
    rows = frappe.get_all(
        "Lead Interest",
        fields=["name", "record_type", "category", "unit", "legacy_child_row"],
        limit_page_length=0,
    )
    for index, row in enumerate(rows):
        if not row.legacy_child_row:
            continue
        if not row.unit:
            continue
        savepoint = f"binding_reconcile_{index}"
        frappe.db.savepoint(savepoint)
        try:
            inventory_type = frappe.db.get_value("Real Estate Unit", row.unit, "inventory_type")
            binding = normalize_legacy_interest_binding(
                record_type=row.record_type,
                category=row.category,
                unit=row.unit,
                unit_inventory_type=inventory_type,
            )
            changes = {}
            if row.record_type != binding["record_type"]:
                changes["record_type"] = binding["record_type"]
            if row.category != binding["category"]:
                changes["category"] = binding["category"]
            if changes:
                frappe.db.set_value("Lead Interest", row.name, changes, update_modified=False)
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            issues.append(
                _record_migration_issue(
                    "interest binding reconciliation",
                    "Lead Interest",
                    row.name,
                    exc,
                )
            )
            continue
        finally:
            try:
                frappe.db.release_savepoint(savepoint)
            except Exception:
                pass
        if changes:
            normalized.append(
                {
                    "lead_interest": row.name,
                    "from_record_type": row.record_type,
                    "to_record_type": changes.get("record_type", row.record_type),
                    "from_category": row.category,
                    "to_category": changes.get("category", row.category),
                }
            )
            updated += 1
    return {
        "updated": updated,
        "skipped": len(issues),
        "normalized": normalized,
        "issues": issues,
    }


def _legacy_to_interest_name(lead, identifier):
    if not identifier:
        return None
    if frappe.db.exists("Lead Interest", identifier):
        if frappe.db.get_value("Lead Interest", identifier, "lead") != lead:
            frappe.throw(_("Lead Interest {0} does not belong to Lead {1}.").format(identifier, lead), frappe.PermissionError)
        return identifier
    return frappe.db.get_value("Lead Interest", {"lead": lead, "legacy_child_row": identifier}, "name")


def resolve_interest_names(lead, identifiers, required=False):
    resolved = []
    for identifier in parse_names(identifiers):
        name = _legacy_to_interest_name(lead, identifier)
        if not name:
            frappe.throw(_("Interest record {0} was not found for this Lead.").format(identifier), frappe.DoesNotExistError)
        if name not in resolved:
            resolved.append(name)
    if required and not resolved:
        frappe.throw(_("Select at least one Lead Interest."))
    return resolved


def get_interests(lead, names=None, include_closed=True):
    filters = {"lead": lead}
    resolved = resolve_interest_names(lead, names) if names else []
    if resolved:
        filters["name"] = ["in", resolved]
    if not include_closed:
        filters["is_active"] = 1
    rows = frappe.get_all(
        "Lead Interest",
        filters=filters,
        fields=["*"],
        order_by="is_active desc, last_transition_at desc, creation desc",
        limit_page_length=0,
    )
    return [frappe._dict(row) for row in rows]


def get_interest(lead, identifier):
    names = resolve_interest_names(lead, [identifier], required=True)
    return frappe.get_doc("Lead Interest", names[0])


def create_transition(interest, from_status, to_status, action=None, outcome=None, reason=None, allow_same=False):
    if not frappe.db.exists("DocType", "Lead Interest Transition"):
        return None
    if from_status == to_status and not allow_same:
        return None
    transition = frappe.get_doc({
        "doctype": "Lead Interest Transition",
        "lead_interest": interest.name,
        "lead": interest.lead,
        "from_status": from_status,
        "to_status": to_status,
        "action": action,
        "outcome": outcome,
        "reason": reason,
        "transitioned_on": now_datetime(),
        "actor": frappe.session.user,
    })
    transition.insert(ignore_permissions=True)
    return transition


def transition_interest(
    identifier,
    to_status,
    action=None,
    outcome=None,
    reason=None,
    force=False,
    mirror_legacy=True,
):
    if to_status not in INTEREST_STATUSES:
        frappe.throw(_("Invalid Lead Interest status: {0}").format(to_status))
    interest = frappe.get_doc("Lead Interest", identifier)
    frappe.db.sql("SELECT `name` FROM `tabLead Interest` WHERE `name` = %s FOR UPDATE", (interest.name,))
    interest.reload()
    from_status = interest.workflow_status
    if from_status == to_status:
        if action:
            interest.latest_action = action
            interest.save(ignore_permissions=True)
        return interest
    if not force and to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
        frappe.throw(
            _("Lead Interest cannot move from {0} to {1}.").format(from_status, to_status),
            frappe.ValidationError,
        )
    interest.workflow_status = to_status
    interest.is_active = 0 if to_status in TERMINAL_INTEREST_STATUSES else 1
    interest.latest_action = action
    interest.last_transition_at = now_datetime()
    interest.last_transition_by = frappe.session.user
    if to_status == "Offer Sent" and not interest.offer_sent_at:
        interest.offer_sent_at = now_datetime()
    if interest.record_type == "Request":
        if to_status == "Fulfilled":
            interest.request_status = "Fulfilled"
        elif to_status in {"Cancelled", "Superseded"}:
            interest.request_status = "Cancelled"
        elif to_status == "Requested":
            interest.request_status = "Open"
    interest.save(ignore_permissions=True)
    create_transition(interest, from_status, to_status, action=action, outcome=outcome, reason=reason)
    if mirror_legacy:
        mirror_interest_to_legacy(interest)
    return interest


def mirror_interest_to_legacy(interest):
    if not frappe.db.exists("CRM Lead", interest.lead):
        return
    lead_doc = frappe.get_doc("CRM Lead", interest.lead)
    row = next(
        (item for item in (lead_doc.get("interested_in_units") or []) if item.name == interest.legacy_child_row),
        None,
    )
    if not row:
        row = lead_doc.append(
            "interested_in_units",
            {
                "doctype": "Lead Interested Unit",
                "interest_record_type": interest.record_type,
                "interest_category": interest.category,
                "unit": interest.unit,
            },
        )
    row.interest_record_type = interest.record_type
    row.interest_category = interest.category
    row.unit = interest.unit
    for fieldname in LEGACY_COPY_FIELDS:
        if row.meta.has_field(fieldname):
            row.set(fieldname, interest.get(fieldname))
    status = interest.workflow_status
    row.unit_interest_status = "Lost Interest" if status in TERMINAL_INTEREST_STATUSES else "Active"
    row.offer_sent = int(status in {
        "Offer Sent",
        "Offer Viewed",
        "Offer Accepted",
        "Negotiating",
        "Showing Scheduled",
        "Shown - Interested",
        "Shown - Considering",
    })
    row.offer_sent_at = interest.offer_sent_at
    proposal_map = {
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
    }
    if status in proposal_map:
        row.proposal_status = proposal_map[status]
    lead_doc.flags.real_estate_status_transition = True
    lead_doc.save(ignore_permissions=True)
    if row.name and interest.get("legacy_child_row") != row.name:
        frappe.db.set_value(
            "Lead Interest",
            interest.name,
            "legacy_child_row",
            row.name,
            update_modified=False,
        )
        interest.legacy_child_row = row.name


def append_action_scopes(action, identifiers, primary=None):
    names = resolve_interest_names(action.lead, identifiers)
    existing = {row.lead_interest for row in (action.get("interest_scopes") or []) if row.lead_interest}
    for name in names:
        if name in existing:
            continue
        interest = frappe.get_doc("Lead Interest", name)
        action.append("interest_scopes", {
            "lead_interest": interest.name,
            "unit": interest.unit,
            "status_before": interest.workflow_status,
            "is_primary_scope": int(name == primary),
        })
    action.scope_type = "Batch" if len(names) > 1 else "Interest" if names else "Lead"
    return names


def action_interest_names(action):
    scope_rows = action.get("interest_scopes") or []
    scoped = [row.lead_interest for row in scope_rows if row.lead_interest]
    if scope_rows and len(scoped) != len(scope_rows):
        frappe.throw(
            _("Action {0} contains an incomplete Lead Interest scope.").format(action.name),
            frappe.ValidationError,
        )
    if scoped:
        return resolve_interest_names(action.lead, scoped, required=True)
    return resolve_interest_names(action.lead, action.get("interest_rows"))


def migrate_action_interest_scopes():
    if not is_available() or not frappe.db.exists("DocType", "Lead Action Execution"):
        return {"updated": 0, "skipped": 0, "issues": []}
    updated = 0
    skipped = 0
    issues = []
    action_names = frappe.get_all("Lead Action Execution", pluck="name", limit_page_length=0)
    for index, action_name in enumerate(action_names):
        action = frappe.get_doc("Lead Action Execution", action_name)
        if action.get("interest_scopes"):
            try:
                action_interest_names(action)
            except Exception as exc:
                issues.append(
                    _record_migration_issue(
                        "existing action scope validation",
                        "Lead Action Execution",
                        action_name,
                        exc,
                    )
                )
            skipped += 1
            continue
        savepoint = f"action_scope_{index}"
        frappe.db.savepoint(savepoint)
        try:
            identifiers = parse_legacy_scope_identifiers(action.get("interest_rows"))
            if not identifiers and action.get("unit"):
                candidates = frappe.get_all(
                    "Lead Interest",
                    filters={"lead": action.lead, "unit": action.unit, "is_active": 1},
                    pluck="name",
                    limit_page_length=2,
                )
                if len(candidates) != 1:
                    raise LegacyInterestDataError(
                        f"Unit {action.unit} has {len(candidates)} active Interests; scope requires exactly one"
                    )
                identifiers = candidates
            append_action_scopes(action, identifiers)
            action.save(ignore_permissions=True)
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            issues.append(
                _record_migration_issue(
                    "action scope promotion",
                    "Lead Action Execution",
                    action_name,
                    exc,
                )
            )
            skipped += 1
            continue
        finally:
            try:
                frappe.db.release_savepoint(savepoint)
            except Exception:
                pass
        updated += 1
    return {"updated": updated, "skipped": skipped, "issues": issues}


def open_action_for_interest(lead_interest, exclude_action=None):
    if not frappe.db.exists("DocType", "Lead Action Interest Scope"):
        return None
    rows = frappe.db.sql(
        """
        SELECT scope.parent AS action
        FROM `tabLead Action Interest Scope` scope
        INNER JOIN `tabLead Action Execution` action ON action.name = scope.parent
        WHERE scope.parenttype = 'Lead Action Execution'
          AND scope.lead_interest = %s
          AND action.is_required = 1
          AND action.workflow_status IN ('Planned', 'Due', 'In Progress')
          AND (%s IS NULL OR action.name != %s)
        ORDER BY action.scheduled_start ASC, action.creation ASC
        LIMIT 1
        """,
        (lead_interest, exclude_action, exclude_action),
        as_dict=True,
    )
    return rows[0].action if rows else None


def assert_interests_available(names, exclude_action=None):
    for name in names:
        existing = open_action_for_interest(name, exclude_action=exclude_action)
        if existing:
            frappe.throw(
                _("Lead Interest {0} already has required action {1}.").format(name, existing),
                frappe.ValidationError,
            )


def update_action_scope_results(action, outcome=None):
    for scope in action.get("interest_scopes") or []:
        if not scope.lead_interest or not frappe.db.exists("Lead Interest", scope.lead_interest):
            continue
        interest = frappe.get_doc("Lead Interest", scope.lead_interest)
        scope.status_after = interest.workflow_status
        scope.row_outcome = outcome
    action.save(ignore_permissions=True)


def _rollup_target(interests, origin_status):
    active = [row for row in interests if row.workflow_status not in TERMINAL_INTEREST_STATUSES]
    if any(row.workflow_status == "Shown - Interested" for row in active):
        return LEAD_STATUS_OFFER_SELECTED
    if any(row.workflow_status in {"Offer Accepted", "Negotiating", "Showing Scheduled", "Shown - Considering"} for row in active):
        return LEAD_STATUS_NEGOTIATING
    if any(row.workflow_status in {"Offer Sent", "Offer Viewed"} for row in active):
        return LEAD_STATUS_OFFER_SENT
    if any(row.workflow_status == "Requested" for row in active):
        return LEAD_STATUS_REQUESTED
    return origin_status


def _validate_rollup_interests(interests):
    valid_categories = {
        "Inventory Unit": {"Resale", "Primary"},
        "Request": {"Resale", "Primary", "Brokerage Request"},
        "Outsource": {"Outsource"},
        "International": {"International"},
    }
    for interest in interests:
        if interest.workflow_status not in INTEREST_STATUSES:
            raise LegacyInterestDataError(
                f"Lead Interest {interest.name} has invalid workflow status {interest.workflow_status!r}"
            )
        if interest.category not in valid_categories.get(interest.record_type, set()):
            raise LegacyInterestDataError(
                f"Lead Interest {interest.name} has invalid {interest.record_type}/{interest.category} binding"
            )
        if interest.record_type == "Inventory Unit":
            if not interest.unit:
                raise LegacyInterestDataError(
                    f"Lead Interest {interest.name} has no linked Unit"
                )
            unit = frappe.db.get_value(
                "Real Estate Unit",
                interest.unit,
                ["inventory_type", "owner_lead"],
                as_dict=True,
            )
            if not unit or unit.inventory_type != interest.category:
                raise LegacyInterestDataError(
                    f"Lead Interest {interest.name} does not match its Unit inventory type"
                )
            if interest.category == "Resale":
                owner_role = (
                    frappe.db.get_value("CRM Lead", unit.owner_lead, "party_type")
                    if unit.owner_lead
                    else None
                )
                if owner_role != "Seller":
                    raise LegacyInterestDataError(
                        f"Lead Interest {interest.name} uses Resale inventory without a Seller owner"
                    )
            if interest.category == "Primary" and unit.owner_lead:
                raise LegacyInterestDataError(
                    f"Lead Interest {interest.name} uses seller-owned Primary inventory"
                )
        elif interest.unit:
            raise LegacyInterestDataError(
                f"Lead Interest {interest.name} links a Unit from non-inventory record type"
            )
        if (
            interest.record_type == "Request"
            and interest.category in {"Resale", "Primary"}
            and not interest.requested_destination
        ):
            raise LegacyInterestDataError(
                f"Lead Interest {interest.name} has no requested Destination"
            )


def reconcile_lead_rollup(lead, action_label="Interest workflow rollup"):
    if not is_available() or not frappe.db.exists("CRM Lead", lead):
        return None
    lead_doc = frappe.get_doc("CRM Lead", lead)
    if lead_doc.get("party_type") == "Seller":
        return lead_doc.get("status")
    interests = get_interests(lead, include_closed=True)
    _validate_rollup_interests(interests)
    target = _rollup_target(interests, _origin_status(lead_doc))
    if lead_doc.get("status") != target:
        from real_estate_crm_customs.api import _save_workflow_doc, _set_lead_status

        _set_lead_status(lead_doc, target, action_label)
        _save_workflow_doc(lead_doc)
    return target


def migrate_open_showing_facts():
    if not is_available() or not frappe.db.exists("DocType", "Lead Action Execution"):
        return {"changed": 0, "skipped": 0, "issues": []}
    changed = 0
    skipped = 0
    issues = []
    actions = frappe.get_all(
        "Lead Action Execution",
        filters={
            "workflow_status": ["in", list(ACTION_OPEN_STATUSES)],
            "action_type": "Showing",
        },
        pluck="name",
        order_by="scheduled_start asc, creation asc",
        limit_page_length=0,
    )
    for action_index, action_name in enumerate(actions):
        action = frappe.get_doc("Lead Action Execution", action_name)
        try:
            names = action_interest_names(action)
        except Exception as exc:
            issues.append(
                _record_migration_issue(
                    "open Showing fact migration",
                    "Lead Action Execution",
                    action_name,
                    exc,
                )
            )
            skipped += 1
            continue
        for interest_index, name in enumerate(names):
            try:
                already_migrated = frappe.db.exists(
                    "Lead Interest Transition",
                    {
                        "lead_interest": name,
                        "action": action.name,
                        "reason": "Migrated open Showing",
                    },
                )
                if already_migrated:
                    continue
                interest = frappe.get_doc("Lead Interest", name)
            except Exception as exc:
                issues.append(
                    _record_migration_issue(
                        "open Showing fact migration",
                        "Lead Interest",
                        name,
                        exc,
                    )
                )
                skipped += 1
                continue
            if interest.workflow_status == "Showing Scheduled":
                continue
            if interest.workflow_status not in {
                "Offer Accepted",
                "Negotiating",
                "Shown - Considering",
                "Shown - Interested",
            }:
                continue
            savepoint = f"open_showing_{action_index}_{interest_index}"
            frappe.db.savepoint(savepoint)
            try:
                transition_interest(
                    name,
                    "Showing Scheduled",
                    action=action.name,
                    outcome="Scheduled",
                    reason="Migrated open Showing",
                    force=True,
                    mirror_legacy=False,
                )
            except Exception as exc:
                frappe.db.rollback(save_point=savepoint)
                issues.append(
                    _record_migration_issue(
                        "open Showing fact migration",
                        "Lead Interest",
                        name,
                        exc,
                    )
                )
                skipped += 1
                continue
            finally:
                try:
                    frappe.db.release_savepoint(savepoint)
                except Exception:
                    pass
            changed += 1
    return {"changed": changed, "skipped": skipped, "issues": issues}


def migrate_showing_and_negotiation_facts():
    if not is_available() or not frappe.db.exists("DocType", "Lead Action Execution"):
        return {"changed": 0, "skipped": 0, "issues": []}
    changed = 0
    skipped = 0
    issues = []
    actions = frappe.get_all(
        "Lead Action Execution",
        filters={
            "workflow_status": "Completed",
            "action_type": ["in", ["Showing", "Negotiation Follow-up"]],
        },
        fields=["name", "action_type", "outcome", "result_data"],
        order_by="completed_at asc, creation asc",
        limit_page_length=0,
    )
    for action_index, row in enumerate(actions):
        action = frappe.get_doc("Lead Action Execution", row.name)
        try:
            names = action_interest_names(action)
        except Exception as exc:
            issues.append(
                _record_migration_issue(
                    "action fact migration",
                    "Lead Action Execution",
                    row.name,
                    exc,
                )
            )
            skipped += 1
            continue
        if not names:
            continue
        result = {}
        if row.result_data:
            try:
                result = json.loads(row.result_data)
            except (TypeError, ValueError, json.JSONDecodeError):
                result = {}
        if row.action_type == "Showing" and row.outcome == "Completed":
            target = {
                "Interested": "Shown - Interested",
                "Considering": "Shown - Considering",
                "No Feedback": "Shown - Considering",
                "Rejected": "Rejected",
            }.get(result.get("unit_outcome"))
        elif row.action_type == "Negotiation Follow-up":
            target = "Rejected" if row.outcome == "Declined" else "Negotiating"
        else:
            target = None
        if not target:
            continue
        for interest_index, name in enumerate(names):
            try:
                already_migrated = frappe.db.exists(
                    "Lead Interest Transition",
                    {
                        "lead_interest": name,
                        "action": action.name,
                        "reason": "Migrated action result",
                    },
                )
                if already_migrated:
                    continue
                interest = frappe.get_doc("Lead Interest", name)
            except Exception as exc:
                issues.append(
                    _record_migration_issue(
                        "action fact migration",
                        "Lead Interest",
                        name,
                        exc,
                    )
                )
                skipped += 1
                continue
            if interest.workflow_status == target:
                continue
            savepoint = f"action_fact_{action_index}_{interest_index}"
            frappe.db.savepoint(savepoint)
            try:
                transition_interest(
                    name,
                    target,
                    action=action.name,
                    outcome=row.outcome,
                    reason="Migrated action result",
                    force=True,
                    mirror_legacy=False,
                )
            except Exception as exc:
                frappe.db.rollback(save_point=savepoint)
                issues.append(
                    _record_migration_issue(
                        "action fact migration",
                        "Lead Interest",
                        name,
                        exc,
                    )
                )
                skipped += 1
                continue
            finally:
                try:
                    frappe.db.release_savepoint(savepoint)
                except Exception:
                    pass
            changed += 1
    return {"changed": changed, "skipped": skipped, "issues": issues}


def run_full_migration():
    result = {
        "interests": migrate_legacy_lead_interests(),
        "binding_reconciliation": reconcile_existing_legacy_interest_bindings(),
    }
    result["action_scopes"] = migrate_action_interest_scopes()
    result["open_showings"] = migrate_open_showing_facts()
    result["action_facts"] = migrate_showing_and_negotiation_facts()
    reconciled = 0
    rollup_issues = []
    for index, lead in enumerate(
        frappe.get_all(
            "CRM Lead",
            filters={"party_type": "Buyer"},
            pluck="name",
            limit_page_length=0,
        )
    ):
        savepoint = f"lead_rollup_{index}"
        frappe.db.savepoint(savepoint)
        try:
            legacy_rows = frappe.get_all(
                "Lead Interested Unit",
                filters={"parent": lead, "parenttype": "CRM Lead"},
                pluck="name",
            )
            promoted_rows = set(
                frappe.get_all(
                    "Lead Interest",
                    filters={"lead": lead, "legacy_child_row": ["in", legacy_rows]},
                    pluck="legacy_child_row",
                )
            ) if legacy_rows else set()
            missing_rows = [name for name in legacy_rows if name not in promoted_rows]
            if missing_rows:
                raise LegacyInterestDataError(
                    f"Rollup skipped because {len(missing_rows)} legacy Interests remain unresolved"
                )
            reconcile_lead_rollup(lead, "Standalone Lead Interest migration")
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            rollup_issues.append(
                _record_migration_issue(
                    "Lead rollup",
                    "CRM Lead",
                    lead,
                    exc,
                )
            )
            continue
        finally:
            try:
                frappe.db.release_savepoint(savepoint)
            except Exception:
                pass
        reconciled += 1
    result["leads_reconciled"] = reconciled
    result["rollup_issues"] = rollup_issues
    issue_count = sum(
        len(value.get("issues", []))
        for value in result.values()
        if isinstance(value, dict)
    ) + len(rollup_issues)
    print(f"Standalone Interest migration completed with {issue_count} skipped issue(s).")
    return result
