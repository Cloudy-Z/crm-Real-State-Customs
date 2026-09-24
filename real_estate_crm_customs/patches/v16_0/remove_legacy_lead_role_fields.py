"""Consolidate historical Lead role aliases into party_type and remove alias metadata."""

from real_estate_crm_customs.lead_role_migration import (
    enforce_canonical_party_role_schema,
)


def execute():
    enforce_canonical_party_role_schema()
