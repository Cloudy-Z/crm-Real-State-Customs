import frappe
from frappe import _
from frappe.model.document import Document


class RealEstateProject(Document):
    def validate(self):
        self._validate_destination()
        self._validate_area()
        self._validate_unique_children("phases", "phase_name", _("Compound phase"))
        self._validate_unique_children("available_unit_types", "unit_type", _("Unit type"))
        self._validate_unique_children("amenities", "amenity", _("Amenity"))
        if self.is_new() and not self.phases:
            frappe.throw(_("A Compound requires at least one phase."))
        if self.is_new() and not any(row.is_available for row in (self.available_unit_types or [])):
            frappe.throw(_("A Compound requires at least one available Unit Type."))

    def on_update(self):
        self._sync_unit_projections()

    def _validate_area(self):
        if self.compound_area not in (None, "") and float(self.compound_area) < 0:
            frappe.throw(_("Compound Area cannot be negative."))

    def _validate_destination(self):
        if not self.destination:
            return
        is_active = frappe.db.get_value("Real Estate Destination", self.destination, "is_active")
        if not is_active and (self.is_new() or self.has_value_changed("destination")):
            frappe.throw(_("Select an active Destination."), frappe.ValidationError)

    def _validate_unique_children(self, table_field, value_field, label):
        values = [row.get(value_field) for row in (self.get(table_field) or []) if row.get(value_field)]
        if len(values) != len(set(values)):
            frappe.throw(_("{0} rows cannot contain duplicates.").format(label))

    def _sync_unit_projections(self):
        if not frappe.db.exists("DocType", "Real Estate Unit"):
            return
        for unit_name in frappe.get_all("Real Estate Unit", filters={"project": self.name}, pluck="name"):
            frappe.db.set_value(
                "Real Estate Unit",
                unit_name,
                {"developer": self.developer, "destination": self.destination},
                update_modified=False,
            )
