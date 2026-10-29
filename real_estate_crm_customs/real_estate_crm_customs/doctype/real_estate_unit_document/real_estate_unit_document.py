# Copyright (c) 2026, Real Estate CRM Customs
# For license information, please see license.txt

from frappe.model.document import Document


class RealEstateUnitDocument(Document):
	"""One owner-side paper collected for a Unit listing (ownership proof, ID, POA, ...).

	The listing gate is validated in ``owner_workflow`` during the guided Owner
	action, so the rules run once and never block a plain child-row save.
	"""
