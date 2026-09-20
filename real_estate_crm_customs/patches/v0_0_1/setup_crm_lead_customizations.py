def execute():
    """Compatibility marker only.

    The original patch called the complete installer before custom DocTypes were
    synchronized. Frappe v16 executes this patch in pre-model-sync, so all
    schema-dependent setup is intentionally owned by post-schema lifecycle hooks.
    """
    return None
