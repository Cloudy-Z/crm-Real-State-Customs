"""Rewrite legacy CRM Lead saved-view filters, columns, and rows."""

from real_estate_crm_customs.lead_role_migration import (
    enforce_canonical_party_role_schema,
)


def execute():
    enforce_canonical_party_role_schema()
