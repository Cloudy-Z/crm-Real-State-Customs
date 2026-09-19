import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

from real_estate_crm_customs.unit_financials import UnitFinancialError, calculate_unit_financials


LEGACY_UNIT_TYPE_ABBREVIATIONS = {
    "Villa": "VIL",
    "Chalet": "CHL",
    "Apartment": "APT",
    "Duplex": "DUP",
    "Penthouse": "PEN",
    "Studio": "STD",
    "Townhouse": "TWN",
}
INVENTORY_TYPES = {"Rental", "Resale", "Primary", "International"}
AREA_FIELDS = ("bua", "land_area", "garden_area", "roof_area", "terrace_area")
COUNT_FIELDS = ("bedrooms", "bathrooms", "number_of_floors")


class RealEstateUnit(Document):
    def before_validate(self):
        set_dynamic_defaults(self)
        set_parent_projections(self)
        mirror_legacy_unit_type(self)
        set_delivery_status(self)
        set_financial_values(self)

    def before_insert(self):
        set_dynamic_defaults(self)
        set_parent_projections(self)
        mirror_legacy_unit_type(self)
        set_delivery_status(self)
        set_financial_values(self)
        if not self.sku:
            self.sku = generate_unit_sku(self)

    def validate(self):
        validate_unit(self)


def set_dynamic_defaults(doc):
    if not doc.status:
        doc.status = "Available"
    if not doc.area_uom:
        doc.area_uom = "Sq M"
    # Preserve the legacy audit field without treating it as authoritative.
    if doc.meta.has_field("created_by") and not doc.created_by:
        doc.created_by = frappe.session.user


def set_parent_projections(doc):
    if not doc.project:
        return
    compound = frappe.db.get_value(
        "Real Estate Project",
        doc.project,
        ["developer", "destination"],
        as_dict=True,
    )
    if not compound:
        frappe.throw(_("Compound {0} was not found.").format(doc.project), frappe.DoesNotExistError)
    doc.developer = compound.get("developer")
    doc.destination = compound.get("destination")


def mirror_legacy_unit_type(doc):
    if doc.get("physical_unit_type"):
        doc.unit_type = doc.physical_unit_type
    elif doc.get("unit_type") and frappe.db.exists("Real Estate Unit Type", doc.unit_type):
        doc.physical_unit_type = doc.unit_type


def set_delivery_status(doc):
    if not doc.get("delivery_date"):
        doc.delivery_status = None
        return
    doc.delivery_status = "Ready to Move" if getdate(doc.delivery_date) <= getdate(nowdate()) else "Off Plan"


def set_financial_values(doc):
    try:
        values = calculate_unit_financials(
            paid=doc.get("paid"),
            over_price=doc.get("over_price"),
            remaining=doc.get("remaining"),
            over_is_gross=bool(doc.get("over_is_gross")),
            precision=2,
        )
    except UnitFinancialError as exc:
        frappe.throw(_(str(exc)), frappe.ValidationError)
    for fieldname, value in values.items():
        doc.set(fieldname, float(value))


def validate_unit(doc):
    validate_new_unit_completeness(doc)
    validate_inventory_type(doc)
    validate_owner_lead(doc)
    validate_unit_number(doc)
    validate_physical_unit_type(doc)
    validate_non_negative_details(doc)
    validate_rental_rates(doc)


def validate_new_unit_completeness(doc):
    if not doc.is_new():
        return
    missing = [
        label
        for fieldname, label in (
            ("physical_unit_type", _("Unit Type")),
            ("finishing_type", _("Finishing Type")),
            ("delivery_date", _("Delivery Date")),
        )
        if not doc.get(fieldname)
    ]
    if missing:
        frappe.throw(_("Complete the mandatory Unit fields: {0}.").format(", ".join(missing)))


def validate_inventory_type(doc):
    if doc.inventory_type not in INVENTORY_TYPES:
        frappe.throw(_("Inventory Type must be Rental, Resale, Primary, or International."))
    if doc.inventory_type != "Resale" and (doc.get("paid") or 0) > 0:
        frappe.throw(_("Paid is allowed only for Resale inventory."), frappe.ValidationError)


def validate_owner_lead(doc):
    if doc.inventory_type == "Resale" and not doc.owner_lead:
        frappe.throw(_("A Resale unit requires a Seller Owner."), frappe.ValidationError)
    if not doc.owner_lead:
        return
    party_type = frappe.db.get_value("CRM Lead", doc.owner_lead, "party_type")
    if not party_type:
        frappe.throw(_("Owner Lead {0} was not found.").format(doc.owner_lead), frappe.DoesNotExistError)
    if party_type != "Seller":
        frappe.throw(
            _("Unit owners must be Seller leads. {0} is marked as {1}.").format(
                doc.owner_lead,
                party_type,
            ),
            frappe.ValidationError,
        )


def validate_unit_number(doc):
    if not str(doc.get("unit_number") or "").strip():
        frappe.throw(_("Unit Number is mandatory."), frappe.ValidationError)
    filters = {"project": doc.project, "unit_number": doc.unit_number}
    duplicate = frappe.db.get_value("Real Estate Unit", filters, "name")
    if duplicate and duplicate != doc.name:
        frappe.throw(
            _("Unit Number {0} already exists in Compound {1}.").format(doc.unit_number, doc.project),
            frappe.ValidationError,
        )


def validate_physical_unit_type(doc):
    if not doc.physical_unit_type:
        if doc.is_new():
            frappe.throw(_("Unit Type is mandatory."), frappe.ValidationError)
        return
    if not frappe.db.exists("Real Estate Unit Type", doc.physical_unit_type):
        frappe.throw(_("Unit Type {0} was not found.").format(doc.physical_unit_type), frappe.DoesNotExistError)
    is_active = frappe.db.get_value("Real Estate Unit Type", doc.physical_unit_type, "is_active")
    if not is_active and (doc.is_new() or doc.has_value_changed("physical_unit_type")):
        frappe.throw(_("Select an active Unit Type."), frappe.ValidationError)
    allowed = frappe.get_all(
        "Real Estate Compound Unit Type",
        filters={"parent": doc.project, "parenttype": "Real Estate Project", "is_available": 1},
        pluck="unit_type",
    )
    if allowed and doc.physical_unit_type not in allowed:
        frappe.throw(
            _("Unit Type {0} is not enabled for Compound {1}.").format(
                doc.physical_unit_type,
                doc.project,
            ),
            frappe.ValidationError,
        )


def validate_non_negative_details(doc):
    for fieldname in AREA_FIELDS + COUNT_FIELDS + ("maintenance",):
        value = doc.get(fieldname)
        if value not in (None, "") and float(value) < 0:
            frappe.throw(_("{0} cannot be negative.").format(doc.meta.get_label(fieldname)))


def validate_rental_rates(doc):
    monthly = float(doc.get("rental_monthly_rate") or 0)
    daily = float(doc.get("rental_daily_rate") or 0)
    if monthly < 0 or daily < 0:
        frappe.throw(_("Rental rates cannot be negative."), frappe.ValidationError)
    if doc.inventory_type == "Rental" and monthly <= 0 and daily <= 0:
        frappe.throw(_("Rental inventory requires a monthly or daily rent."), frappe.ValidationError)
    if doc.inventory_type != "Rental" and (monthly > 0 or daily > 0):
        frappe.throw(_("Rental rates are allowed only for Rental inventory."), frappe.ValidationError)


def generate_unit_sku(doc):
    if not doc.project:
        frappe.throw(_("Compound is required before generating the unit SKU."))
    if not doc.physical_unit_type:
        frappe.throw(_("Unit Type is required before generating the unit SKU."))

    project_name = frappe.db.get_value("Real Estate Project", doc.project, "project_name") or doc.project
    project_code = abbreviate_project_name(project_name)
    abbreviation = frappe.db.get_value("Real Estate Unit Type", doc.physical_unit_type, "abbreviation")
    unit_code = (abbreviation or LEGACY_UNIT_TYPE_ABBREVIATIONS.get(doc.physical_unit_type) or abbreviate_project_name(doc.physical_unit_type, max_length=3)).upper()[:3]
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


def refresh_delivery_statuses():
    """Keep the stored delivery projection current without touching modified timestamps."""
    if not frappe.db.exists("DocType", "Real Estate Unit"):
        return
    frappe.db.sql(
        """
        update `tabReal Estate Unit`
        set delivery_status = case
            when delivery_date is null then null
            when delivery_date <= current_date then 'Ready to Move'
            else 'Off Plan'
        end
        where coalesce(delivery_status, '') != coalesce(
            case
                when delivery_date is null then null
                when delivery_date <= current_date then 'Ready to Move'
                else 'Off Plan'
            end,
            ''
        )
        """
    )


def before_insert_generate_sku(doc, method=None):
    """Compatibility doc-event wrapper; controller validation remains authoritative."""
    set_dynamic_defaults(doc)
    set_parent_projections(doc)
    mirror_legacy_unit_type(doc)
    set_delivery_status(doc)
    set_financial_values(doc)
    if not doc.sku:
        doc.sku = generate_unit_sku(doc)


def validate_resale_owner(doc, method=None):
    """Compatibility doc-event wrapper for sites that still have hooks metadata cached."""
    validate_unit(doc)
