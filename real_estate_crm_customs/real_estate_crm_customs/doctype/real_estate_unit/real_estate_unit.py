import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

from real_estate_crm_customs.party_roles import OWNER_ROLE, is_owner_role
from real_estate_crm_customs.requirement_policy import listing_purposes_for_deal  # noqa: F401 (re-export)
from real_estate_crm_customs.unit_financials import UnitFinancialError, calculate_unit_financials
from real_estate_crm_customs.workflow_context import unit_mutation_active


LEGACY_UNIT_TYPE_ABBREVIATIONS = {
    "Villa": "VIL",
    "Chalet": "CHL",
    "Apartment": "APT",
    "Duplex": "DUP",
    "Penthouse": "PEN",
    "Studio": "STD",
    "Townhouse": "TWN",
}
INVENTORY_TYPES = {"Resale", "Primary", "International"}
LISTING_FOR_SALE = "For Sale"
LISTING_FOR_RENT = "For Rent"
LISTING_FOR_SALE_OR_RENT = "For Sale or Rent"
LISTING_PURPOSES = (LISTING_FOR_SALE, LISTING_FOR_RENT, LISTING_FOR_SALE_OR_RENT)
# Owner Intent on the Owner lead -> default Listing Purpose of that Owner's new Units.
OWNER_INTENT_LISTING_PURPOSE = {
    "Sell": LISTING_FOR_SALE,
    "Rent": LISTING_FOR_RENT,
    "Sell or Rent": LISTING_FOR_SALE_OR_RENT,
}


def is_listed_for_sale(listing_purpose):
    return (listing_purpose or LISTING_FOR_SALE) in (LISTING_FOR_SALE, LISTING_FOR_SALE_OR_RENT)


def is_listed_for_rent(listing_purpose):
    return listing_purpose in (LISTING_FOR_RENT, LISTING_FOR_SALE_OR_RENT)


AREA_FIELDS = ("bua", "land_area", "garden_area", "roof_area", "terrace_area")
COUNT_FIELDS = ("bedrooms", "bathrooms", "number_of_floors")
FINANCIAL_INPUT_FIELDS = ("paid", "over_price", "remaining", "over_is_gross")
OWNER_PRE_PRICE_LISTING_STATUSES = {"Pricing Requested", "Price Proposed"}
OWNER_WORKFLOW_CONTROLLED_FIELDS = {
    "pricing_method",
    "listing_status",
    "listing_status_reason",
    "proposed_over_price",
    "proposed_monthly_rent",
    "proposed_daily_rent",
    "pricing_basis",
    "price_proposed_on",
    "listed_on",
    "last_verified_on",
    "next_verification_due",
    "verification_alert_sent_on",
    "owner_documents",
    "listing_agreement_type",
    "listing_agreement_reference",
    "listing_agreement_signed_on",
    "listing_agreement_expires_on",
    "listing_commission_percent",
    "listing_agreement_file",
    "active_buyer_interest_count",
    "total_buyer_interest_count",
}


class RealEstateUnit(Document):
    def before_validate(self):
        set_dynamic_defaults(self)
        if self.is_new() or self.has_value_changed("project"):
            set_parent_projections(self)
        if self.is_new() or self.has_value_changed("physical_unit_type"):
            mirror_legacy_unit_type(self)
        sync_unit_category(self)
        if self.is_new() or self.has_value_changed("delivery_date"):
            set_delivery_status(self)
        if self.is_new() or any(self.has_value_changed(fieldname) for fieldname in FINANCIAL_INPUT_FIELDS + ("listing_purpose",)):
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
        validate_closing_state_mutation(self)
        validate_owner_listing_mutation(self)
        validate_unit(self)


def set_dynamic_defaults(doc):
    if not doc.status:
        doc.status = "Available"
    if doc.meta.has_field("listing_purpose") and not doc.get("listing_purpose"):
        doc.listing_purpose = default_listing_purpose(doc.get("owner_lead"))
    if not doc.area_uom:
        doc.area_uom = "Sq M"
    # Preserve the legacy audit field without treating it as authoritative.
    if doc.meta.has_field("created_by") and not doc.created_by:
        doc.created_by = frappe.session.user


def default_listing_purpose(owner_lead=None):
    if owner_lead:
        intent = frappe.db.get_value("CRM Lead", owner_lead, "owner_intent") if _lead_has_owner_intent() else None
        return OWNER_INTENT_LISTING_PURPOSE.get(intent, LISTING_FOR_SALE)
    return LISTING_FOR_SALE


def _lead_has_owner_intent():
    try:
        return bool(frappe.get_meta("CRM Lead").has_field("owner_intent"))
    except Exception:
        return False


def set_parent_projections(doc):
    if not doc.project:
        return
    compound = frappe.db.get_value(
        "Real Estate Project",
        doc.project,
        ["name", "developer", "destination", "record_type", "parent_compound"],
        as_dict=True,
    )
    if not compound:
        frappe.throw(_("Compound {0} was not found.").format(doc.project), frappe.DoesNotExistError)
    doc.developer = compound.get("developer")
    doc.destination = compound.get("destination")
    if doc.meta.has_field("master_compound"):
        is_phase = compound.get("record_type") == "Phase" and compound.get("parent_compound")
        doc.master_compound = compound.get("parent_compound") if is_phase else compound.get("name")


def sync_unit_category(doc):
    """Unit Category only narrows the Unit Type choice; the saved value follows the Unit Type."""
    if not doc.meta.has_field("unit_category") or not doc.get("physical_unit_type"):
        return
    category = frappe.db.get_value("Real Estate Unit Type", doc.physical_unit_type, "unit_category")
    if category:
        doc.unit_category = category


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
    if not is_listed_for_sale(doc.get("listing_purpose")):
        # A rent-only Unit has no sale price; keep the inputs and clear derived totals at zero.
        for fieldname in ("paid", "over_price", "remaining"):
            if doc.get(fieldname) in (None, ""):
                doc.set(fieldname, 0)
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
    if doc.is_new() or _changed(doc, "inventory_type", "paid"):
        validate_inventory_type(doc)
    if doc.is_new() or _changed(doc, "inventory_type", "owner_lead"):
        validate_owner_lead(doc)
    if doc.is_new() or _changed(
        doc,
        "inventory_type",
        "listing_purpose",
        "paid",
        "over_price",
        "remaining",
        "rental_monthly_rate",
        "rental_daily_rate",
    ):
        validate_listing_purpose(doc)
    if doc.is_new() or _changed(doc, "project", "unit_number"):
        validate_unit_number(doc)
    if doc.is_new() or _changed(doc, "project", "physical_unit_type"):
        validate_physical_unit_type(doc)
    if doc.is_new() or _changed(doc, *(AREA_FIELDS + COUNT_FIELDS + ("maintenance",))):
        validate_non_negative_details(doc)
    if doc.is_new() or _changed(
        doc,
        "inventory_type",
        "listing_purpose",
        "rental_monthly_rate",
        "rental_daily_rate",
    ):
        validate_rental_rates(doc)


def validate_closing_state_mutation(doc):
    """Closing reservation and sale links are controlled by the workflow service.

    Unit creation remains unaffected. Existing ordinary edits cannot turn a
    Unit Reserved/Sold or rewrite the Interest which owns that reservation.
    """
    if doc.is_new() or unit_mutation_active():
        return
    changed = _changed(doc, "status", "closing_interest", "sold_interest")
    if changed:
        frappe.throw(
            _("Unit availability and closing links are managed only by the contract-closing workflow."),
            frappe.PermissionError,
        )


def validate_owner_listing_mutation(doc):
    """Owner listing state is changed only by the Owner workflow service.

    DocType read-only properties protect the portal, but Data Import and direct
    API saves are server requests.  This guard keeps those paths from marking
    an incomplete Unit as Listed or overwriting evidence/audit projections.
    """
    if doc.is_new() or unit_mutation_active():
        return
    changed = {fieldname for fieldname in OWNER_WORKFLOW_CONTROLLED_FIELDS if doc.has_value_changed(fieldname)}
    if changed:
        labels = ", ".join(doc.meta.get_label(fieldname) for fieldname in sorted(changed))
        frappe.throw(
            _("Owner listing workflow fields can only be changed through the guided Owner workflow: {0}.").format(labels),
            frappe.PermissionError,
        )


def _changed(doc, *fieldnames):
    return any(doc.has_value_changed(fieldname) for fieldname in fieldnames)


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
        frappe.throw(_("Inventory Type must be Resale, Primary, or International. For rentals, use Resale with Listing Purpose For Rent."))
    if doc.inventory_type != "Resale" and (doc.get("paid") or 0) > 0:
        frappe.throw(_("Paid is allowed only for Resale inventory."), frappe.ValidationError)


def validate_owner_lead(doc):
    if doc.inventory_type == "Resale" and not doc.owner_lead:
        frappe.throw(_("A Resale unit requires an Owner Lead."), frappe.ValidationError)
    if doc.inventory_type == "Primary" and doc.owner_lead:
        frappe.throw(_("A Primary unit cannot have an Owner Lead."), frappe.ValidationError)
    if not doc.owner_lead:
        return
    party_type = frappe.db.get_value("CRM Lead", doc.owner_lead, "party_type")
    if not party_type:
        frappe.throw(_("Owner Lead {0} was not found.").format(doc.owner_lead), frappe.DoesNotExistError)
    if not is_owner_role(party_type):
        frappe.throw(
            _("Unit owners must be Owner leads. {0} is marked as {1}.").format(
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
    allowed = _allowed_unit_types(doc.project)
    master = doc.get("master_compound")
    if not allowed and master and master != doc.project:
        # A Phase without its own list follows its Parent Compound's Unit Types.
        allowed = _allowed_unit_types(master)
    if allowed and doc.physical_unit_type not in allowed:
        frappe.throw(
            _("Unit Type {0} is not enabled for Compound {1}.").format(
                doc.physical_unit_type,
                doc.project,
            ),
            frappe.ValidationError,
        )


def _allowed_unit_types(project):
    return frappe.get_all(
        "Real Estate Compound Unit Type",
        filters={"parent": project, "parenttype": "Real Estate Project", "is_available": 1},
        pluck="unit_type",
    )


def validate_non_negative_details(doc):
    for fieldname in AREA_FIELDS + COUNT_FIELDS + ("maintenance",):
        value = doc.get(fieldname)
        if value not in (None, "") and float(value) < 0:
            frappe.throw(_("{0} cannot be negative.").format(doc.meta.get_label(fieldname)))


def validate_listing_purpose(doc):
    purpose = doc.get("listing_purpose") or LISTING_FOR_SALE
    if purpose not in LISTING_PURPOSES:
        frappe.throw(
            _("Listing Purpose must be For Sale, For Rent, or For Sale or Rent."),
            frappe.ValidationError,
        )
    if is_listed_for_rent(purpose) and doc.inventory_type != "Resale":
        frappe.throw(
            _("Only Resale units owned by an Owner lead can be listed for rent."),
            frappe.ValidationError,
        )
    awaiting_agent_price = doc.get("listing_status") in OWNER_PRE_PRICE_LISTING_STATUSES
    if (
        is_listed_for_sale(purpose)
        and not awaiting_agent_price
        and float(doc.get("total_price_net") or 0) <= 0
        and float(doc.get("price") or 0) <= 0
    ):
        frappe.throw(
            _("A Unit listed for sale needs a sale price. Enter Paid, Over Price, and Remaining."),
            frappe.ValidationError,
        )


def validate_rental_rates(doc):
    monthly = float(doc.get("rental_monthly_rate") or 0)
    daily = float(doc.get("rental_daily_rate") or 0)
    if monthly < 0 or daily < 0:
        frappe.throw(_("Rent amounts cannot be negative."), frappe.ValidationError)
    purpose = doc.get("listing_purpose") or LISTING_FOR_SALE
    awaiting_agent_price = doc.get("listing_status") in OWNER_PRE_PRICE_LISTING_STATUSES
    if is_listed_for_rent(purpose) and not awaiting_agent_price and monthly <= 0 and daily <= 0:
        frappe.throw(
            _("A Unit listed for rent needs a monthly rent or a daily rent."),
            frappe.ValidationError,
        )
    if not is_listed_for_rent(purpose) and (monthly > 0 or daily > 0):
        frappe.throw(
            _("Rent amounts are allowed only when Listing Purpose is For Rent or For Sale or Rent."),
            frappe.ValidationError,
        )


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
