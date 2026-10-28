"""Safe additive migration for contract closing and requirement snapshots.

This patch deliberately never fabricates a successful transaction, drops a
legacy column, commits, or reinterprets historical completed meetings.
"""

from datetime import date

import frappe

from real_estate_crm_customs.party_roles import is_owner_role
from real_estate_crm_customs.requirement_policy import RequirementError, delivery_date


RETIRED_MEETING_PURPOSES = {
    "General Meeting",
    "Meeting with Owner",
    "Discovery Meeting",
    "Offer Review",
    "Negotiation Meeting",
}
LEGACY_OWNER_PURPOSES = {"Meeting with Owner"}
OPEN_ACTION_STATUSES = {"Planned", "Due", "In Progress"}


def execute():
    """Run idempotent field/data normalization only after models are synced."""
    migrate_budget_presence()
    migrate_canonical_delivery_dates()
    migrate_interest_snapshot_defaults()
    normalize_legacy_meetings()
    reconcile_conservative_rollups()
    frappe.clear_cache()


def _has_columns(doctype, *fieldnames):
    return frappe.db.exists("DocType", doctype) and all(
        frappe.db.has_column(doctype, fieldname) for fieldname in fieldnames
    )


def _log_issue(title, message):
    try:
        frappe.log_error(message=message, title=title[:140])
    except Exception:
        print(f"{title}: {message}")


def _unambiguous_iso_date(value):
    """Return strict ISO date text or None; never turn fuzzy data into a date."""
    # Legacy fields are free-text values.  Do not accept a driver-converted
    # ``date`` object here: that would make a non-ISO source indistinguishable
    # from an actual YYYY-MM-DD value and silently invent canonical certainty.
    if not isinstance(value, str):
        return None
    try:
        return delivery_date(value)
    except RequirementError:
        return None


def migrate_canonical_delivery_dates():
    """Copy only strict legacy YYYY-MM-DD values into the new Date fields."""
    mappings = (
        ("CRM Lead", "preferred_delivery_time", "preferred_delivery_date"),
        ("Lead Interest", "requested_delivery_time", "requested_delivery_date"),
        ("Lead Interested Unit", "requested_delivery_time", "requested_delivery_date"),
    )
    for doctype, legacy_field, canonical_field in mappings:
        if not _has_columns(doctype, legacy_field, canonical_field):
            continue
        rows = frappe.get_all(
            doctype,
            fields=["name", legacy_field, canonical_field],
            filters={canonical_field: ["is", "not set"]},
            limit_page_length=0,
        )
        for index, row in enumerate(rows):
            raw = row.get(legacy_field)
            if raw in (None, ""):
                continue
            parsed = _unambiguous_iso_date(raw)
            if not parsed:
                _log_issue(
                    "Unresolved legacy delivery date",
                    f"{doctype} {row.name}: retained legacy {legacy_field}={raw!r}; no canonical date was invented.",
                )
                continue
            savepoint = f"delivery_date_{doctype}_{index}".replace(" ", "_")[:60]
            frappe.db.savepoint(savepoint)
            try:
                frappe.db.set_value(doctype, row.name, canonical_field, parsed, update_modified=False)
            except Exception as exc:
                frappe.db.rollback(save_point=savepoint)
                _log_issue(
                    "Canonical delivery date migration skipped",
                    f"{doctype} {row.name}: {exc}; legacy {legacy_field}={raw!r} was retained.",
                )
            finally:
                try:
                    frappe.db.release_savepoint(savepoint)
                except Exception:
                    pass


def _site_currency():
    """Use CRM's native setting first and tolerate Frappe-only installations."""
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


def migrate_budget_presence():
    """One-time, non-destructive interpretation of old Currency defaults.

    Positive historical values are known; old zero defaults are not proof that
    an agent explicitly supplied zero. New workflow snapshots set their flags.
    """
    amounts = ("requested_budget", "requested_down_payment_budget", "requested_monthly_installment_budget")
    fields = [field for amount in amounts for field in (amount, amount + "_set")]
    for doctype in ("Lead Interest", "Lead Interested Unit"):
        if not _has_columns(doctype, "requirement_budget_presence_version", *fields):
            continue
        rows = frappe.get_all(doctype, filters={"requirement_budget_presence_version": 0},
            fields=["name", *fields], limit_page_length=0)
        for row in rows:
            values = {"requirement_budget_presence_version": 1}
            for amount in amounts:
                raw = row.get(amount)
                values[amount + "_set"] = int(bool(row.get(amount + "_set")) or (raw is not None and float(raw) > 0))
            frappe.db.set_value(doctype, row.name, values, update_modified=False)


def migrate_interest_snapshot_defaults():
    """Set only missing currency/scope defaults; preserve null optional budgets."""
    default_currency = _site_currency()
    for doctype in ("Lead Interest", "Lead Interested Unit"):
        if not _has_columns(doctype, "inventory_search_scope", "budget_currency"):
            continue
        fields = ["name", "interest_category" if doctype == "Lead Interested Unit" else "category", "inventory_search_scope", "budget_currency"]
        category_field = fields[1]
        for row in frappe.get_all(doctype, fields=fields, limit_page_length=0):
            values = {}
            category = row.get(category_field)
            if not row.get("inventory_search_scope"):
                values["inventory_search_scope"] = category if category in {"Resale", "Primary"} else "Both"
            if not row.get("budget_currency") and default_currency:
                values["budget_currency"] = default_currency
            if values:
                frappe.db.set_value(doctype, row.name, values, update_modified=False)


def _pending_resale_owner_scope_is_valid(action):
    """Whether a historical owner alias still has one canonical pending scope."""
    if action.workflow_status not in OPEN_ACTION_STATUSES or not action.get("unit"):
        return False
    if not frappe.db.exists("DocType", "Lead Action Interest Scope"):
        return False
    scopes = frappe.get_all(
        "Lead Action Interest Scope",
        filters={
            "parent": action.name,
            "parenttype": "Lead Action Execution",
        },
        fields=["lead_interest", "unit"],
        limit_page_length=2,
    )
    if len(scopes) != 1 or scopes[0].get("unit") != action.unit or not scopes[0].get("lead_interest"):
        return False
    interest = frappe.db.get_value(
        "Lead Interest",
        scopes[0].lead_interest,
        ["record_type", "category", "unit"],
        as_dict=True,
    )
    if not interest or interest.record_type != "Inventory Unit" or interest.category != "Resale" or interest.unit != action.unit:
        return False
    unit = frappe.db.get_value(
        "Real Estate Unit", action.unit, ["inventory_type", "owner_lead"], as_dict=True
    )
    if not unit or unit.inventory_type != "Resale" or not unit.owner_lead:
        return False
    return is_owner_role(frappe.db.get_value("CRM Lead", unit.owner_lead, "party_type"))


def _mark_meeting_for_review(action):
    if action.meeting_review_status == "Needs Review/Replan":
        return
    frappe.db.set_value(
        "Lead Action Execution",
        action.name,
        "meeting_review_status",
        "Needs Review/Replan",
        update_modified=False,
    )


def normalize_legacy_meetings():
    """Preserve historical purposes; repair only an unambiguous pending owner alias."""
    if not _has_columns("Lead Action Execution", "action_type", "purpose", "workflow_status", "meeting_review_status"):
        return
    fields = ["name", "action_type", "purpose", "workflow_status", "meeting_review_status", "unit"]
    for index, action in enumerate(frappe.get_all("Lead Action Execution", fields=fields, filters={"action_type": "Meeting"}, limit_page_length=0)):
        if action.purpose not in RETIRED_MEETING_PURPOSES:
            continue
        # Completed/cancelled/rescheduled historical records are audit facts.
        # Never rename or flag them merely because their old select value has
        # been retired from new meeting creation.
        if action.workflow_status not in OPEN_ACTION_STATUSES:
            continue
        savepoint = f"legacy_meeting_{index}"
        frappe.db.savepoint(savepoint)
        try:
            if action.purpose in LEGACY_OWNER_PURPOSES and _pending_resale_owner_scope_is_valid(action):
                frappe.db.set_value(
                    "Lead Action Execution", action.name, "purpose", "Owner Meeting", update_modified=False
                )
            else:
                # Do not cancel or repurpose an ambiguous pending task.  Its
                # existing schema option remains valid so agents can cancel it
                # without an OptionError while they review/replan it.
                _mark_meeting_for_review(action)
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            _log_issue("Legacy meeting migration skipped", f"Lead Action Execution {action.name}: {exc}")
        finally:
            try:
                frappe.db.release_savepoint(savepoint)
            except Exception:
                pass


def reconcile_conservative_rollups():
    """Replace obsolete Offer Selected summaries by facts without inventing wins."""
    if not _has_columns("CRM Lead", "party_type", "status") or not frappe.db.exists("DocType", "Lead Interest"):
        return
    from real_estate_crm_customs.interest_workflow import reconcile_lead_rollup

    for lead in frappe.get_all(
        "CRM Lead",
        filters={"party_type": "Buyer"},
        fields=["name"],
        limit_page_length=0,
    ):
        savepoint = f"closing_rollup_{lead.name}".replace("-", "_")[:60]
        frappe.db.savepoint(savepoint)
        try:
            # Reconciliation uses evidence-backed Closed Won first, then
            # Closing/negotiating/requested facts. It never changes an Interest.
            reconcile_lead_rollup(lead.name, "Closing workflow migration")
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            _log_issue("Closing rollup migration skipped", f"CRM Lead {lead.name}: {exc}")
        finally:
            try:
                frappe.db.release_savepoint(savepoint)
            except Exception:
                pass
