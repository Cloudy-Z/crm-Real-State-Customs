import frappe
from frappe import _
from frappe.model.document import Document


class LeadInterestTransition(Document):
    def before_insert(self):
        if not self.transitioned_on:
            self.transitioned_on = frappe.utils.now_datetime()
        if not self.actor:
            self.actor = frappe.session.user

    def validate(self):
        if not self.is_new():
            frappe.throw(_("Lead Interest Transition records are immutable."), frappe.PermissionError)

    def on_trash(self):
        frappe.throw(_("Lead Interest Transition records cannot be deleted."), frappe.PermissionError)
