"""Rewrite legacy CRM Lead saved-view role filters to canonical ``party_type``."""

from real_estate_crm_customs.lead_role_migration import (
    enforce_canonical_party_role_schema,
)


def execute():
    enforce_canonical_party_role_schema()
