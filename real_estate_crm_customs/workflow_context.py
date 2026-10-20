"""Server-side workflow contexts for authoritative real-estate mutations.

Contexts are held only in ``frappe.flags``.  They are never inferred from a
submitted Document's ``flags`` object or from request payload data.
"""

from contextlib import contextmanager

import frappe
from frappe import _


_CREATION_MODES = {"workflow", "migration"}
_CREATION_STACK_FLAG = "_real_estate_interest_creation_context_stack"
_MUTATION_STACK_FLAG = "_real_estate_interest_mutation_context_stack"
_UNIT_MUTATION_STACK_FLAG = "_real_estate_unit_mutation_context_stack"


def _flags():
    """Return Frappe's request-local flags namespace, creating it only if needed."""
    flags = getattr(frappe, "flags", None)
    if flags is None:
        # Frappe normally always exposes flags.  This small fallback also keeps
        # isolated controller tests honest without consulting document flags.
        flags = frappe._dict()
        frappe.flags = flags
    return flags


def _stack(flag_name):
    flags = _flags()
    stack = getattr(flags, flag_name, None)
    if stack is None:
        stack = []
        setattr(flags, flag_name, stack)
    return stack


@contextmanager
def interest_creation_context(mode="workflow"):
    """Authorize one server-side Interest creation/mirror operation.

    ``workflow`` is for a validated action submission. ``migration`` is only
    for the compatibility/backfill wrappers.  Nested contexts are supported so
    a mirror can run inside the action cycle without widening authorization.
    """
    if mode not in _CREATION_MODES:
        raise ValueError("Interest creation mode must be 'workflow' or 'migration'.")
    stack = _stack(_CREATION_STACK_FLAG)
    stack.append(mode)
    try:
        yield
    finally:
        stack.pop()


def creation_context_active():
    """Whether this request currently has an authorized creation context."""
    return bool(_stack(_CREATION_STACK_FLAG))


def creation_context_mode():
    """Return the innermost creation mode, or ``None`` outside a context."""
    stack = _stack(_CREATION_STACK_FLAG)
    return stack[-1] if stack else None


def require_interest_creation_context():
    """Reject generic/import inserts that did not come from the workflow."""
    if not creation_context_active():
        frappe.throw(
            _("Lead Interests may only be created by the approved workflow or migration."),
            frappe.PermissionError,
        )


@contextmanager
def interest_mutation_context(operation="workflow"):
    """Authorize an internal lifecycle/evidence mutation for this request."""
    stack = _stack(_MUTATION_STACK_FLAG)
    stack.append(operation)
    try:
        yield
    finally:
        stack.pop()


def interest_mutation_active():
    return bool(_stack(_MUTATION_STACK_FLAG))


@contextmanager
def unit_mutation_context(operation="closing"):
    """Authorize internal Closing/Sold reservation link updates on a Unit."""
    stack = _stack(_UNIT_MUTATION_STACK_FLAG)
    stack.append(operation)
    try:
        yield
    finally:
        stack.pop()


def unit_mutation_active():
    return bool(_stack(_UNIT_MUTATION_STACK_FLAG))
