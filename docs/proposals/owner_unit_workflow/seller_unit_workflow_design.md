# Owner Lead and Unit Listing Workflow — Implementation Record

> **Status: implemented on 29 October 2026.** This document preserves the approved editable design and records the production contract. “Seller” in older history means **Owner**; the only stored role authority is `CRM Lead.party_type = Owner`.

## Purpose and boundary

The Owner workflow guides an agent from first contact to a safely matchable resale listing. It is deliberately separate from the Buyer workflow:

- **Buyer authority:** one standalone `Lead Interest` per requirement or offered Unit. Buyer status is derived from Interest states.
- **Owner authority:** one Unit listing lifecycle per owned Unit. Owner status is derived from owned Unit listing/commercial facts.
- **Shared evidence:** both write `Lead Action Execution`, Events, comments and CRM activity. Owner Unit actions use `scope_type = Unit`; they never receive a Buyer Interest scope.
- **Ownership rule:** only an Owner Lead may be `Real Estate Unit.owner_lead`. A Buyer links to a Unit only through `Lead Interest`.

This separation prevents an Owner pricing/document action from changing a Buyer Interest, and prevents a Buyer offer from bypassing an Owner’s listing evidence/availability control.

## Editable diagrams

- [Owner Lead action cycle](seller_lead_workflow.mmd)
- [Unit listing state machine](unit_listing_state_machine.mmd)
- [Buyer/Owner Lead status map](lead_status_map.mmd)
- [Quarterly verification sequence](quarterly_verification_sequence.mmd)
- [Owner/Unit data model](seller_unit_erd.mmd)

## Owner Lead guided cycle

1. A manually/imported Owner Lead begins **New**; an API/campaign Owner Lead begins **Fresh Lead**.
2. The first guided action is **Call — Initial Qualification**. An answered call must be marked **Interested** or **Not Interested**. No Answer increments both counters; Answered resets only the current streak.
3. An Interested Owner can use **Add Property**, a General Follow-up Call, or an Explore Meeting. The Owner workflow panel is separate from the Buyer Interest Workboard.
4. **Add Property** opens the full Unit tab/section layout, with the Owner prefilled and protected. It creates a Resale Unit, a completed intake audit action and exactly one successor action.
5. The Owner chooses one pricing path:
   - **Owner Price:** the Unit proceeds to **Documents Pending**.
   - **Agent Pricing:** the Unit proceeds to **Pricing Requested**. The agent records the comparable/recommendation basis and at least one proposal; then the Owner accepts, sets another price, requests re-pricing, or declines.
6. The **Listing Documents** action captures/updates ownership evidence, Owner ID/passport evidence and the listing agreement. Files must be private. Marking Listed runs the server gate.
7. A Listed Unit has a planned **Availability Verification** call three months later. The daily scheduler makes it Due and reconciles one Event/action; it does not automatically claim that the Unit is available.

## Listing state and allowed operations

| Listing state | Unit actions | Result |
|---|---|---|
| Pricing Requested | Agent Pricing, Pricing Follow-up Call | Price Proposed or remains pending |
| Price Proposed | Price Decision, Pricing Follow-up Call | Documents Pending, Pricing Requested, Withdrawn |
| Documents Pending | Listing Documents, General Follow-up Call | Listed when all evidence passes; otherwise remains pending |
| Listed | Availability Verification | Verification Due on the planned date; still owner-controlled |
| Verification Due | Availability Verification | Listed, Off Market, Pricing Requested or Withdrawn |
| Off Market | Availability Verification | Listed after explicit availability confirmation, or Withdrawn |
| Withdrawn | Manager reactivation only | Documents Pending or Pricing Requested, then a successor action |

`Reserved`, `Sold` and `Rented` are **availability** states driven only by the Buyer signed-contract/lease process. A reserved, sold or rented Unit cannot be reactivated as an Owner listing. Returning a Rented Unit to market requires a new Owner confirmation before matching resumes.

## Evidence and pricing gate

The system allows a pre-price Agent Pricing intake but will not list a Unit until all relevant conditions are true:

1. the applicable sale price and/or monthly/daily rent exists for `Listing Purpose`;
2. a **privately uploaded** Owner document row is Received or Verified for **Ownership Contract** or **Title Deed / Registration**;
3. a **privately uploaded** Owner document row is Received or Verified for **Owner National ID / Passport**;
4. a Listing Agreement type, signed date and **privately uploaded** signed agreement are recorded; and
5. uploaded Owner evidence and agreement files are private.

The agent proposes a price; the Owner makes the final price decision. The confirmed financial inputs continue to use the canonical Unit calculation: Total Net = Paid + Over Net + Remaining, with the defined taxes and commission calculation. A proposal does not alter the live asking price until the Owner accepts/sets it.

## Buyer visibility, meetings and insights

- An Owner-owned Unit is included in new Buyer Smart Match/manual selection/direct Interest binding only when it is Available and `Listed`, or in the limited **14-day Verification Due grace**. The final `LeadInterest.validate` check prevents any UI/API bypass.
- Primary Units and historical Units without a listing lifecycle remain compatible with existing inventory behavior.
- Buyer **Owner Meetings** and **Showings** are projected to the Unit and Owner Lead. Buyer Closing, signed contract and abandoned closing refresh the Owner Lead rollup.
- `active_buyer_interest_count` is the number of distinct Buyers with active Interests on the Unit. `total_buyer_interest_count` is the distinct Buyer count across all linked Interests. Interest hooks and the migration reconcile them.
- Unit Track Changes and Track Views are enabled. The portal records one unique user view through `log_unit_view()` and displays views, active Buyers and all linked Buyers on the Unit form.

## Owner Lead status rollup

`reconcile_owner_lead_rollup()` selects the first applicable value:

1. **Closed Won** — an owned Unit is Sold/Rented by a signed CRM Interest.
2. **Closing** — an owned Unit is Reserved.
3. **Negotiating** — an active Buyer Interest on an owned Unit is in an accepted/negotiating/showing state.
4. **Listed** — an owned Unit is Listed, Verification Due or Off Market.
5. **Documents Pending** — an owned Unit requires listing documents/agreement.
6. **Pricing** — an owned Unit is Pricing Requested or Price Proposed.
7. **New** or **Fresh Lead** — the Owner’s stored origin when none of the above applies.

The Lead Status remains read-only. Buyer statuses (Requested and Offer Sent) remain Buyer-only because they come from Buyer Interests.

## Scheduler and overdue safety

`run_daily_listing_verification()` is configured as a daily scheduler job. For each eligible owned Unit it locks the Unit, transitions Listed to Verification Due on/after `next_verification_due`, ensures only one open Unit action/Event, and adds one manager-attention comment after seven overdue days. At more than fourteen overdue days it is excluded from new matching. Scheduler errors are isolated with savepoints and logged; a bad Unit never stops the scan.

## Implementation map

| Area | Main file(s) |
|---|---|
| Owner workflow API/service | `real_estate_crm_customs/owner_workflow.py` |
| Owner listing fields / documents | `doctype/real_estate_unit/`, `doctype/real_estate_unit_document/` |
| Buyer counter and final listing backstop | `doctype/lead_interest/lead_interest.py`, `api.py` |
| Owner lead status and layouts | `install.py`, `fixtures/custom_field.json`, `hooks.py` |
| Migration | `patches/v16_0/owner_unit_listing_workflow.py` |
| Owner panel and full Add Property intake | CRM fork `frontend/src/components/OwnerListingWorkflow.vue`, `pages/Lead.vue` |
| Unit insights / view logging | CRM fork `frontend/src/pages/RealEstateForm.vue` |
| Regression tests | `tests/test_owner_listing_workflow.py` plus existing closing/action/schema tests |

## References behind the approved design

- [RESO StandardStatus](https://dd.reso.org/DD2.0/Property/StandardStatus/) — explicit listing availability/status semantics.
- [NAR: Determining Asking Price](https://www.nar.realtor/determining-asking-price) — agent advice with Owner decision.
- [NAR: Listing Agreements](https://www.nar.realtor/the-facts/consumer-guide-listing-agreements) — agreement evidence concepts.
- [Bayut TruCheck](https://www.bayut.sa/blog/en/invest-in-saudi/trucheck/) — verified/stale listing control concept.
