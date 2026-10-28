# Research and audit notes (Seller/Unit workflow) — 27 Oct 2026

## External sources (already read)
- RESO DD 2.0 StandardStatus (https://dd.reso.org/DD2.0/Property/StandardStatus/): status reflects listing agent–seller contract; values Active, Active Under Contract, Canceled, Closed, Coming Soon, Delete, Expired, Hold, Incomplete, Pending, Withdrawn; adoption 96% (404/420 orgs).
- NAR consumer guide (https://www.nar.realtor/the-facts/consumer-guide-what-goes-into-pricing-your-home): agent recommends list price using CMA/comps; "Do I have the final say on my asking price? Yes."
- NAR Determining Asking Price (https://www.nar.realtor/determining-asking-price): agents use market analysis, comps, condition, trends, seller goals/timeline.
- Bayut TruCheck (https://www.bayut.sa/blog/en/invest-in-saudi/trucheck/): badge = property available and up to date; visible for a set period.
- Frappe Customize Form (https://docs.frappe.io/erpnext/user/manual/en/customize-form): Track Changes, Track Views, Allow Import definitions.
- Frappe Data Import (https://docs.frappe.io/erpnext/user/manual/en/data-import): child tables imported within parent template; insert/update modes.

## Frappe v16.34 source findings
- View Log only written by frappe/desk/form/load.py getdoc -> doc.add_viewed(); CRM portal uses frappe.client.get (no view log).
- Data Import requires meta.allow_import AND role "import" permission; System Manager boot can_import lists allow_import doctypes.
- import_file_by_path re-syncs DocType JSON when migration_hash differs or file modified newer.

## Code audit findings
- Done: backend a5072b2 (allow_import on 6 masters, import perm SM+Sales Manager, track_changes Unit+Compound, track_views Unit). 76 backend tests OK (run: PYTHONPATH=tests python3 -m unittest discover -s tests -p 'test_*.py').
- Used Lead statuses (code): New, Fresh Lead, Requested, Offer Sent, Negotiating, Closing, Closed Won. Upstream CRM after_install creates Contacted, Nurture, Qualified, Converted, Unqualified, Junk (only on install). Offer Selected obsolete (patch recalculates). Upstream convert_to_deal sets Qualified only if it exists. Earlier cleanup 7d3da55 used raw SQL reassign + force delete; removed in a0329b5.
- reconcile_lead_rollup returns early for Seller (no seller rollup today). _allowed_action_definitions returns [] for Seller. record_call_outcome/record_whatsapp_subject/complete_lead_action call _validate_buyer_lead (Buyer-only).
- Lead fields Buyer-only via depends_on: interest_status, no_answer_* counters, last_call_outcome, last_call_at, is_primary_buyer.
- Unit.status Available/Reserved/Sold; direct edits blocked by validate_closing_state_mutation; Smart Match filters status == Available (api.py ~1085, ~1226).
- Buyer Owner Meeting/Showing already project Events to seller lead via Lead Action Execution.seller_lead.
- Interest statuses (15) all referenced; action purposes legacy (General Meeting, Meeting with Owner, Discovery Meeting, Offer Review, Negotiation Meeting) only in patch history; Meeting Confirmation not offered by UI.
- Frontend LeadProgressGraph.vue maps 'Offer Selected' -> Negotiating (legacy reference).
