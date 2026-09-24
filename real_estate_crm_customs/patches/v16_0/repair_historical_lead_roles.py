"""Repair historical CRM Lead Party Roles from retained aliases and relationships."""

from real_estate_crm_customs.lead_role_migration import (
    enforce_canonical_party_role_schema,
)


def execute():
    enforce_canonical_party_role_schema()
