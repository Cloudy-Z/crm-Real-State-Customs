import frappe
from frappe import _
from frappe.model.document import Document

RECORD_TYPE_COMPOUND = "Compound"
RECORD_TYPE_PHASE = "Phase"
RECORD_TYPES = (RECORD_TYPE_COMPOUND, RECORD_TYPE_PHASE)


class RealEstateProject(Document):
    """Compound master. A Phase is a Compound record that belongs to a Parent Compound.

    Phases inherit Developer and Destination from their Parent Compound, so the
    hierarchy can never disagree with the Units and Smart Match projections.
    """

    def validate(self):
        self._normalize_record_type()
        self._validate_phase_hierarchy()
        self._validate_destination()
        self._validate_area()
        self._validate_unique_children("phases", "phase_name", _("Legacy phase note"))
        self._validate_unique_children("available_unit_types", "unit_type", _("Unit type"))
        self._validate_unique_children("amenities", "amenity", _("Amenity"))
        self._validate_required_geography()

    def on_update(self):
        self._sync_child_phase_projections()
        self._sync_unit_projections()

    def on_trash(self):
        if self.record_type != RECORD_TYPE_PHASE:
            phases = frappe.get_all(
                "Real Estate Project",
                filters={"parent_compound": self.name},
                pluck="name",
                limit_page_length=5,
            )
            if phases:
                frappe.throw(
                    _("Delete or move the Phases of this Compound first: {0}").format(", ".join(phases)),
                    frappe.LinkExistsError,
                )

    def _normalize_record_type(self):
        self.record_type = (self.record_type or RECORD_TYPE_COMPOUND).strip().title()
        if self.record_type not in RECORD_TYPES:
            frappe.throw(_("Record Type must be Compound or Phase."))
        if self.record_type == RECORD_TYPE_COMPOUND:
            self.parent_compound = None

    def _validate_phase_hierarchy(self):
        if self.record_type != RECORD_TYPE_PHASE:
            return
        if not self.parent_compound:
            frappe.throw(_("Parent Compound is mandatory for a Phase."))
        if self.parent_compound == self.name:
            frappe.throw(_("A Phase cannot be its own Parent Compound."))
        parent = frappe.db.get_value(
            "Real Estate Project",
            self.parent_compound,
            ["name", "record_type", "developer", "destination"],
            as_dict=True,
        )
        if not parent:
            frappe.throw(
                _("Parent Compound {0} was not found. Import or create the Compound before its Phases.").format(
                    self.parent_compound
                ),
                frappe.DoesNotExistError,
            )
        if (parent.record_type or RECORD_TYPE_COMPOUND) != RECORD_TYPE_COMPOUND:
            frappe.throw(_("Parent Compound must be a Compound, not another Phase."))
        if not self.is_new() and frappe.db.exists("Real Estate Project", {"parent_compound": self.name}):
            frappe.throw(_("This Compound has Phases, so it cannot itself become a Phase."))
        # A Phase always belongs to the same Developer and Destination as its Compound.
        self.developer = parent.developer or self.developer
        self.destination = parent.destination or self.destination

    def _validate_required_geography(self):
        if not self.developer:
            frappe.throw(_("Developer is mandatory."))
        if not self.destination:
            frappe.throw(_("Destination is mandatory."))

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

    def _sync_child_phase_projections(self):
        """Keep each Phase's inherited Developer/Destination equal to this Compound's."""
        if self.record_type != RECORD_TYPE_COMPOUND:
            return
        phases = frappe.get_all(
            "Real Estate Project",
            filters={"parent_compound": self.name},
            fields=["name", "developer", "destination"],
            limit_page_length=0,
        )
        for phase in phases:
            if phase.developer == self.developer and phase.destination == self.destination:
                continue
            frappe.db.set_value(
                "Real Estate Project",
                phase.name,
                {"developer": self.developer, "destination": self.destination},
                update_modified=False,
            )
            _sync_units_for_project(phase.name, self.developer, self.destination, self.name)

    def _sync_unit_projections(self):
        master = self.parent_compound if self.record_type == RECORD_TYPE_PHASE else self.name
        _sync_units_for_project(self.name, self.developer, self.destination, master or self.name)


def _sync_units_for_project(project, developer, destination, master_compound):
    if not frappe.db.exists("DocType", "Real Estate Unit"):
        return
    has_master = frappe.get_meta("Real Estate Unit").has_field("master_compound")
    values = {"developer": developer, "destination": destination}
    if has_master:
        values["master_compound"] = master_compound
    for unit_name in frappe.get_all("Real Estate Unit", filters={"project": project}, pluck="name"):
        frappe.db.set_value("Real Estate Unit", unit_name, values, update_modified=False)


def get_master_compound(project):
    """Return the top-level Compound for a Compound or Phase name."""
    if not project:
        return None
    row = frappe.db.get_value(
        "Real Estate Project", project, ["name", "record_type", "parent_compound"], as_dict=True
    )
    if not row:
        return None
    if row.record_type == RECORD_TYPE_PHASE and row.parent_compound:
        return row.parent_compound
    return row.name


def get_compound_family(project):
    """Return the Compound and all of its Phases, so a Compound filter also matches Phase Units."""
    master = get_master_compound(project)
    if not master:
        return [project] if project else []
    phases = frappe.get_all(
        "Real Estate Project", filters={"parent_compound": master}, pluck="name", limit_page_length=0
    )
    if project != master:
        # A specific Phase was selected: match only that Phase.
        return [project]
    return [master, *phases]
