"""Canonical real-estate party roles shared by runtime validation and migrations."""


PARTY_ROLE_ALIASES = {
    "buyer": "Buyer",
    "buyer lead": "Buyer",
    "buy": "Buyer",
    "purchaser": "Buyer",
    "seller": "Seller",
    "seller lead": "Seller",
    "sell": "Seller",
    "owner": "Seller",
}


def normalize_party_role(value):
    normalized = " ".join(str(value or "").strip().lower().split())
    return PARTY_ROLE_ALIASES.get(normalized)


def remove_fields_from_layout(layout, fieldnames):
    """Remove field references from flat-section or tabbed CRM layout JSON."""
    if not isinstance(layout, list):
        return False
    blocked = set(fieldnames)
    changed = False
    for top_level in layout:
        if not isinstance(top_level, dict):
            continue
        nested_sections = top_level.get("sections")
        sections = nested_sections if isinstance(nested_sections, list) else [top_level]
        for section in sections:
            if not isinstance(section, dict):
                continue
            columns = section.get("columns")
            if not isinstance(columns, list):
                continue
            for column in columns:
                if not isinstance(column, dict):
                    continue
                fields = column.get("fields")
                if not isinstance(fields, list):
                    continue
                filtered = [
                    field
                    for field in fields
                    if (
                        field.get("fieldname")
                        if isinstance(field, dict)
                        else field
                    )
                    not in blocked
                ]
                if filtered != fields:
                    column["fields"] = filtered
                    changed = True
    return changed


def remove_fields_from_quick_filters(values, fieldnames):
    """Return a cleaned field-name list, or None for malformed settings data."""
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        return None
    blocked = set(fieldnames)
    return [value for value in values if value not in blocked]
