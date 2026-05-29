import re

import frappe
from frappe import _
from frappe.model.document import Document


UNIT_TYPE_ABBREVIATIONS = {
    "Villa": "VIL",
    "Chalet": "CHL",
    "Apartment": "APT",
    "Duplex": "DUP",
    "Penthouse": "PEN",
}


class RealEstateUnit(Document):
    def before_validate(self):
        set_dynamic_defaults(self)
        set_developer_from_project(self)

    def before_insert(self):
        set_dynamic_defaults(self)
        set_developer_from_project(self)
        if not self.sku:
            self.sku = generate_unit_sku(self)

    def validate(self):
        validate_resale_owner(self, None)


def set_dynamic_defaults(doc):
    if not doc.status:
        doc.status = "Available"
    if not doc.created_by:
        doc.created_by = frappe.session.user


def set_developer_from_project(doc):
    if doc.project:
        doc.developer = frappe.db.get_value("Real Estate Project", doc.project, "developer")


def generate_unit_sku(doc):
    if not doc.project:
        frappe.throw(_("Project is required before generating the unit SKU."))
    if not doc.unit_type:
        frappe.throw(_("Unit Type is required before generating the unit SKU."))

    project_name = frappe.db.get_value("Real Estate Project", doc.project, "project_name") or doc.project
    project_code = abbreviate_project_name(project_name)
    unit_code = UNIT_TYPE_ABBREVIATIONS.get(doc.unit_type, abbreviate_project_name(doc.unit_type, max_length=3))
    floor_code = format_floor(doc.floor)
    prefix = f"{project_code}-{unit_code}-{floor_code}"
    serial = get_next_sku_serial(prefix)
    return f"{prefix}-{serial:04d}"


def abbreviate_project_name(value, max_length=4):
    cleaned_words = re.findall(r"[A-Za-z0-9]+", value or "")
    if not cleaned_words:
        return "UNIT"[:max_length]
    if len(cleaned_words) == 1:
        return cleaned_words[0][:max_length].upper().ljust(min(max_length, 4), "X")[:max_length]
    return "".join(word[0] for word in cleaned_words)[:max_length].upper()


def format_floor(floor):
    if floor is None or floor == "":
        return "FL00"
    try:
        return f"FL{int(floor):02d}"
    except (TypeError, ValueError):
        return "FL00"


def get_next_sku_serial(prefix):
    existing = frappe.db.sql(
        """
        select sku
        from `tabReal Estate Unit`
        where sku like %s
        order by sku desc
        limit 1
        """,
        (f"{prefix}-%",),
    )
    if not existing:
        return 1
    last_sku = existing[0][0] or ""
    match = re.search(r"-(\d{4})$", last_sku)
    return (int(match.group(1)) + 1) if match else 1


def before_insert_generate_sku(doc, method=None):
    """Doc event wrapper used by hooks.py for explicit SKU generation."""
    set_dynamic_defaults(doc)
    set_developer_from_project(doc)
    if not doc.sku:
        doc.sku = generate_unit_sku(doc)


def validate_resale_owner(doc, method=None):
    # Backward compatibility for legacy records that were created before unit_type became the property category.
    if doc.unit_type == "Resale" and not doc.owner_lead:
        frappe.throw(
            _("Owner Lead is mandatory when Unit Type is Resale."),
            frappe.ValidationError,
        )
