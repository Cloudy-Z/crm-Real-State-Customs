# Implementation Notes

The backend customization is isolated in this app. It creates three real-estate DocTypes and adds real-estate fields to `CRM Lead` through install hooks and fixtures.

The CRM portal action buttons are intentionally implemented in the CRM fork at `frontend/src/doctypes/crm_lead/form.js`, because the current CRM portal already supports file-based controllers loaded from that location.

| Requirement | Implementation location |
|---|---|
| Real Estate Project DocType | `real_estate_crm_customs/real_estate_crm_customs/doctype/real_estate_project` |
| Real Estate Unit DocType and resale validation | `real_estate_crm_customs/real_estate_crm_customs/doctype/real_estate_unit` |
| Lead Interested Unit child table | `real_estate_crm_customs/real_estate_crm_customs/doctype/lead_interested_unit` |
| CRM Lead fields | `install.py`, patch, and `fixtures/custom_field.json` |
| Portal-visible Lead actions | CRM fork: `frontend/src/doctypes/crm_lead/form.js` |
