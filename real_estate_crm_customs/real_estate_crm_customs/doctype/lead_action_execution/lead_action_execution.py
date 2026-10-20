import frappe
from frappe import _
from frappe.model.document import Document


RETIRED_MEETING_PURPOSES = {
    "General Meeting",
    "Meeting with Owner",
    "Discovery Meeting",
    "Offer Review",
    "Negotiation Meeting",
}
CANONICAL_MEETING_PURPOSES = {"Explore Meeting", "Owner Meeting", "Developer Meeting"}


class LeadActionExecution(Document):
    """Immutable-by-agent workflow action record.

    Completed historical meetings retain their original purpose.  Retired
    purpose values are kept in the Select options only so historical documents
    remain valid; new Meeting actions must use one of the three canonical
    meeting types.
    """

    def validate(self):
        if self.is_new() and self.action_type == "Meeting" and self.purpose not in CANONICAL_MEETING_PURPOSES:
            frappe.throw(
                _("New Meeting actions must be Explore Meeting, Owner Meeting, or Developer Meeting."),
                frappe.ValidationError,
            )
        if self.workflow_status in {"Completed", "Cancelled", "Rescheduled", "Missed"} and not self.completed_at:
            self.completed_at = frappe.utils.now_datetime()
