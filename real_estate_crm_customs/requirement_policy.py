"""Pure requirement normalization shared by workflow submission and matching."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


class RequirementError(ValueError):
    pass


def money(value, label="Budget", required=False):
    if value in (None, ""):
        if required:
            raise RequirementError(f"{label} is required.")
        return None
    if isinstance(value, bool):
        raise RequirementError(f"{label} must be a valid non-negative amount.")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RequirementError(f"{label} must be a valid non-negative amount.")
    if not amount.is_finite() or amount < 0:
        raise RequirementError(f"{label} must be a valid non-negative amount.")
    try:
        return float(amount.quantize(Decimal("0.01")))
    except (InvalidOperation, OverflowError):
        raise RequirementError(f"{label} exceeds the supported monetary range.")


def delivery_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        raise RequirementError("Delivery date must be YYYY-MM-DD, not a timestamp.")
    if isinstance(value, date):
        return value.isoformat()
    try:
        parsed = date.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise RequirementError("Delivery date must be a valid YYYY-MM-DD date.")
    if parsed.isoformat() != str(value):
        raise RequirementError("Delivery date must be a valid YYYY-MM-DD date.")
    return parsed.isoformat()


def inventory_categories(scope=None, category="Resale"):
    resolved = scope or (category if category in ("Resale", "Primary") else "Both")
    if resolved not in ("Resale", "Primary", "Both"):
        raise RequirementError("Inventory search scope must be Resale, Primary, or Both.")
    return ["Resale", "Primary"] if resolved == "Both" else [resolved]


def is_property_requirement(category, international_type=None):
    return category in ("Resale", "Primary", "Brokerage Request") or (
        category == "International" and international_type == "Real Estate"
    )


def requirement_snapshot(payload, currency=None, required_total=False):
    categories = inventory_categories(payload.get("inventory_search_scope"), payload.get("interest_category"))
    snapshot = {
        "requested_destination": payload.get("preferred_destination") or None,
        "requested_unit_type": payload.get("preferred_unit_type") or None,
        "requested_budget": money(payload.get("buyer_budget"), "Maximum Total Budget", required_total),
        "requested_down_payment_budget": money(payload.get("down_payment_budget"), "Down Payment Budget"),
        "requested_monthly_installment_budget": money(payload.get("monthly_installment_budget"), "Monthly Installment Budget"),
        "requested_delivery_date": delivery_date(payload.get("preferred_delivery_date")),
        "requested_project": payload.get("preferred_compound") or None,
        "requested_developer": payload.get("preferred_developer") or None,
        "requested_finishing_type": payload.get("preferred_finishing_type") or None,
        "inventory_search_scope": "Both" if len(categories) == 2 else categories[0],
        "budget_currency": payload.get("budget_currency") or currency,
    }

    for field in BUDGET_AMOUNT_FIELDS:
        snapshot[field + "_set"] = int(snapshot[field] is not None)
    snapshot["requirement_budget_presence_version"] = 1
    return snapshot


BUDGET_AMOUNT_FIELDS = ("requested_budget", "requested_down_payment_budget", "requested_monthly_installment_budget")


def requirement_amount(source, field):
    """Read an optional Currency using explicit presence, never its SQL zero default."""
    present = source.get(field + "_set")
    return source.get(field) if present is None or bool(int(present)) else None
