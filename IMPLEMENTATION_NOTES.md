# Implementation Notes

The backend customization is isolated in this app. It creates three real-estate DocTypes and adds real-estate fields to `CRM Lead` and `User` through install hooks and fixtures.

The CRM Lead actions that are visible to agents are implemented in the CRM fork at `frontend/src/pages/Lead.vue`. The action API remains in this custom app so the same business rules can be reused by the frontend, migration scripts, and any future integration layer.

| Requirement | Implementation location |
|---|---|
| Real Estate Project DocType | `real_estate_crm_customs/real_estate_crm_customs/doctype/real_estate_project` |
| Real Estate Unit DocType and resale validation | `real_estate_crm_customs/real_estate_crm_customs/doctype/real_estate_unit` |
| Lead Interested Unit child table | `real_estate_crm_customs/real_estate_crm_customs/doctype/lead_interested_unit` |
| Buyer request without inventory match | `Lead Interested Unit` rows can be marked as `Request` with request notes/status and no linked `Real Estate Unit`; these rows remain in Buyer Interest until matching inventory is available |
| Call outcome action model | `real_estate_crm_customs.api.record_lead_call_outcome` records `Answered` and `No Answer` actions, creates a CRM Call Log where available, writes a Lead comment, and synchronizes the Lead status to `Contacted` or `No Answer` |
| Consecutive no-answer logic | `no_answer_consecutive_count`, `no_answer_first_call`, and `no_answer_second_call` track the current unanswered streak; an answered call resets these active counters to zero |
| Historical no-answer logic | `no_answer_total_count` preserves the lifetime no-answer count even when a later answered call resets the current streak |
| Last call metadata | `last_call_outcome` and `last_call_at` store the latest call result and timestamp for side-panel visibility and reporting |
| Assigned-agent outreach identity | `User` custom fields `real_estate_agent_whatsapp_number` and `real_estate_agent_outreach_email` let the assigned Lead owner define the sender identity used by system WhatsApp/email actions |
| WhatsApp and email outreach actions | `real_estate_crm_customs.api.record_lead_outreach_action` resolves the assigned agent, records the action, and can create the platform WhatsApp Message or outgoing Communication record when sending is requested |
| CRM Lead and User fields | `install.py`, migration hooks, and `fixtures/custom_field.json` |
| Portal-visible Lead actions | CRM fork: `frontend/src/pages/Lead.vue` |

The enhanced no-answer behavior intentionally separates **current operational state** from **historical reporting**. If an agent calls a lead and the lead does not answer, the current streak and total count both increase. If the lead answers on a later call, the current streak and first/second-call indicators return to zero, while the total no-answer history remains available for reporting and coaching.
