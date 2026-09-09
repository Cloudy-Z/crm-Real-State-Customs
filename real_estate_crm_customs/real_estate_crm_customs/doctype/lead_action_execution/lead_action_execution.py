import frappe
from frappe.model.document import Document


class LeadActionExecution(Document):
    """Immutable-by-agent workflow action record.

    Agent actions are created and completed through the workflow API. The DocType
    remains readable to sales roles to provide a durable action timeline.
    """

    def validate(self):
        if self.workflow_status in {"Completed", "Cancelled", "Rescheduled", "Missed"} and not self.completed_at:
            self.completed_at = frappe.utils.now_datetime()
