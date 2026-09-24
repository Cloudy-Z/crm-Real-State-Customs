"""Canonical real-estate party roles shared by runtime validation and migrations."""

import json


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


def resolve_party_role(canonical, aliases=(), *, owns_unit=False, has_interest=False):
    """Resolve historical evidence without silently preserving a stale default."""
    canonical_role = normalize_party_role(canonical)
    alias_roles = {
        role for role in (normalize_party_role(value) for value in aliases) if role
    }

    if owns_unit:
        reason = "relationship_conflict" if has_interest else "unit_owner"
        return "Seller", reason
    if has_interest:
        return "Buyer", "lead_interest"
    if len(alias_roles) == 1:
        alias_role = next(iter(alias_roles))
        if canonical_role and canonical_role != alias_role:
            return alias_role, "legacy_alias_conflict"
        return alias_role, "legacy_alias"
    if canonical_role:
        return canonical_role, "canonical"
    if len(alias_roles) > 1:
        return None, "legacy_alias_conflict"
    return None, "missing"


def party_role_from_view_filters(filters):
    """Read a canonical Party Role from CRM View Settings filter JSON."""
    if isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except (TypeError, ValueError):
            return None
    if not isinstance(filters, dict):
        return None
    value = filters.get("party_type")
    if isinstance(value, list):
        value = value[-1] if value else None
    role = normalize_party_role(value)
    return role if value in {"Buyer", "Seller"} else None


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
