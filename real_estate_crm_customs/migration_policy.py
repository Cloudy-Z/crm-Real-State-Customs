"""Pure compatibility rules for promoting historical real-estate Interest rows.

These helpers never mutate source data. They derive a valid standalone binding or
raise an explicit error so the migration can quarantine that source row and
continue with independent records.
"""

import json


VALID_RECORD_TYPES = {"Inventory Unit", "Request", "Outsource", "International"}
INVENTORY_CATEGORIES = {"Resale", "Primary"}
REQUEST_CATEGORIES = {"Resale", "Primary", "Brokerage Request"}


class LegacyInterestDataError(ValueError):
    pass


def parse_legacy_scope_identifiers(value):
    """Parse a blank value, one legacy identifier, or a JSON list without broadening scope."""
    if value in (None, "", [], (), set()):
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            return [stripped]
    else:
        parsed = value
    if not isinstance(parsed, (list, tuple, set)):
        raise LegacyInterestDataError(
            "Historical action Interest scope must be a list or one direct identifier"
        )
    return list(dict.fromkeys(item for item in parsed if item))


def normalize_legacy_interest_binding(
    *,
    record_type=None,
    category=None,
    unit=None,
    unit_inventory_type=None,
):
    """Return a canonical record type/category without changing the source row.

    Inventory-backed Interests follow the linked Unit because the Unit is the
    canonical inventory classifier. Historical generic Requests with no
    category become Brokerage Request rather than the old unsafe Resale default.
    """

    normalized_record_type = (record_type or "").strip() or (
        "Inventory Unit" if unit else "Request"
    )
    if normalized_record_type not in VALID_RECORD_TYPES:
        raise LegacyInterestDataError(
            f"Unsupported legacy Interest record type: {normalized_record_type}"
        )

    source_category = (category or "").strip() or None
    notes = []

    if normalized_record_type == "Inventory Unit":
        if not unit:
            raise LegacyInterestDataError("Inventory Unit Interest has no linked Unit")
        if unit_inventory_type not in INVENTORY_CATEGORIES:
            raise LegacyInterestDataError(
                f"Linked Unit has unsupported inventory type: {unit_inventory_type or 'blank'}"
            )
        normalized_category = unit_inventory_type
        if source_category and source_category != normalized_category:
            notes.append(
                f"legacy category {source_category} normalized to Unit inventory type {normalized_category}"
            )
    elif normalized_record_type == "Request":
        if unit:
            raise LegacyInterestDataError("Request Interest unexpectedly links a Unit")
        normalized_category = source_category or "Brokerage Request"
        if normalized_category not in REQUEST_CATEGORIES:
            raise LegacyInterestDataError(
                f"Unsupported Request category: {normalized_category}"
            )
        if not source_category:
            notes.append("missing legacy Request category normalized to Brokerage Request")
    elif normalized_record_type == "Outsource":
        if unit:
            raise LegacyInterestDataError("Outsource Interest unexpectedly links a Unit")
        normalized_category = "Outsource"
        if source_category and source_category != normalized_category:
            notes.append(
                f"legacy category {source_category} normalized to Outsource record type"
            )
    else:
        if unit:
            raise LegacyInterestDataError("International Interest unexpectedly links a Unit")
        normalized_category = "International"
        if source_category and source_category != normalized_category:
            notes.append(
                f"legacy category {source_category} normalized to International record type"
            )

    return {
        "record_type": normalized_record_type,
        "category": normalized_category,
        "notes": notes,
    }
