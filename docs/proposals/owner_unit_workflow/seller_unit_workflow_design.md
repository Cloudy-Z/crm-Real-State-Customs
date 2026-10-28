# Seller and Unit Workflow — Design v2 for Approval

> **Status: proposal awaiting approval (not implemented).** Written before the 28 Oct 2026 rename of the **Seller** role to **Owner**, so it still says "Seller". Read "Seller" as "Owner Lead". Approve or change the decisions in section 5 before any code is written.

**Status:** This is a proposal only, and no workflow code has been written. The DocType settings from the first part of your request are already done and pushed as backend `a5072b2`. Those settings are import on all six masters, Track Changes on Unit and Compound, and Track Views on Unit.

**What changed from v1:** The Seller no longer has a separate design. It runs on the **same guided-action engine as the Buyer**. That means the same popup, the same flags, the same "Call or WhatsApp first" rule, the same atomic final submit, and the same required next action. The Seller's **Units play the role that Interest rows play for the Buyer**. Each Unit has its own row status, actions and lock, and the Lead Status is a read-only summary of those rows. The status list is also cleaned so that only statuses one of the two cycles actually reaches remain.

The editable diagrams are the [side-by-side Buyer/Seller cycle](seller_lead_workflow.mmd), the [Unit row states](unit_listing_state_machine.mmd), the [shared Lead status map](lead_status_map.mmd), the [3-month verification sequence](quarterly_verification_sequence.mmd), and the [data model](seller_unit_erd.mmd).

## 1. Buyer → Seller mirror

| Buyer cycle (existing, unchanged) | Seller cycle (proposed, same engine) |
|---|---|
| New / Fresh Lead: only **Call** or **WhatsApp** allowed | Same |
| Call result: Answered / No Answer / Wrong Number / Invalid; no-answer streak resets on answer, total history kept | Same fields and logic. These flags become visible for Sellers too. |
| Answered → **Interested / Not Interested** flag (read-only, system-set) | Same flag, meaning *interested to sell/list* |
| Interested → same popup shows the **Interest requirements** + Smart Match | Interested → same popup shows **Add Property** (unit details) + **who prices: owner or agent** |
| Interest rows: Requested, Matched, Offer Sent … | Unit rows: Pricing Requested, Price Proposed, Listed … |
| **Send Offer** (WhatsApp) → **Offer Decision** | **Agent Pricing** (proposal sent by WhatsApp) → **Price Decision** by the owner |
| Owner / Developer / Explore Meeting | Buyer Owner Meetings and Showings on his Units appear automatically on the Seller Lead and the Unit. Explore Meeting can be reused as an on-site unit visit. |
| Closing needs contract reference + signed date → Closed Won | Same closing, driven from the buyer's Interest on his Unit; the Seller Lead and Unit follow automatically |
| Every action needs a next action at final submit; notes optional | Same. A Unit becoming Listed auto-plans the **3-month Availability Verification** as its next action. |
| Not Interested → only a manager can requalify | Same |
| Interest Workboard with row actions | Seller **Unit Workboard** with the same layout and row actions |

Pricing follows standard practice: the agent recommends a price from comparable sales and condition, but the owner has the final say [2] [3]. For resale, Paid and Remaining are contract facts, so the agent's proposal targets the **Over Price**, and the existing financial formula recalculates every total. The 3-month check applies the same time-bound "available and up to date" idea that portals such as Bayut TruCheck use [4]. The Unit row states map onto the RESO listing-status standard [1].

## 2. Status cleanup — only used statuses remain

**Lead Status (one shared list, system-managed and read-only):**

| Status | Used by | Set when |
|---|---|---|
| New | Buyer + Seller | Lead added manually or by import |
| Fresh Lead | Buyer + Seller | Lead added by API or campaign |
| Requested | Buyer | An active Requested Interest exists |
| Offer Sent | Buyer | An active Offer Sent / Viewed Interest exists |
| **Pricing** (new) | Seller | A Unit is Pricing Requested or Price Proposed |
| **Listed** (new) | Seller | A Unit is Listed or Verification Due |
| Negotiating | Buyer + Seller | Buyer: Interest accepted or negotiating. Seller: any buyer Interest on his Unit is negotiating. |
| Closing | Buyer + Seller | Interest in Closing / Unit Under Contract |
| Closed Won | Buyer + Seller | Contract signed with evidence / Unit Sold |

**Removed:** Contacted, Nurture, Qualified, Converted, Unqualified and Junk (the upstream CRM defaults), plus the legacy Offer Selected. None of these is reachable in either cycle.

How the removal stays safe:
- The cleanup runs from the app, not SQL, on migrate and on new installs.
- Any Lead still holding a removed status is **recalculated by the rollup**, not forced to New.
- Status-change history keeps the old names as text, so the audit trail is preserved.
- The upstream "Convert to Deal" sets Qualified only if that status exists, so removing it is safe.

The earlier cleanup (commit `7d3da55`) reset Leads to New with raw SQL. This version avoids that.

**Other status lists checked:** All 15 Interest row statuses, all 7 action statuses and all 3 Unit statuses are used, so they stay. These old meeting purposes are no longer offered to agents: General Meeting, Meeting with Owner, Discovery Meeting, Offer Review, Negotiation Meeting and Meeting Confirmation. They stay in the field options only so that historical records still validate when saved.

## 3. Seller actions and next states

**Lead-level actions (same gate as the Buyer):**

| Seller qualification | Allowed actions | Effect |
|---|---|---|
| Not yet qualified | Call (Initial Qualification), WhatsApp | Answered requires Interested / Not Interested. Interested requires Add Property in the same submit. |
| Interested | Add Property (another Unit), General Follow-up call, Explore Meeting (unit visit) | Each new Unit becomes an independent row |
| Not Interested | None; a manager requalifies with a reason | — |

**Unit row actions:**

| Unit row status | Row actions | Next row statuses |
|---|---|---|
| Pricing Requested | **Agent Pricing** (proposed Over Price, basis, optional private file); Pricing Follow-up call | Price Proposed, Withdrawn |
| Price Proposed | **Price Decision**: Accept / Owner sets own price / Re-price / Declines; Pricing Follow-up call | Listed, Pricing Requested, Withdrawn |
| Listed | Early **Availability Verification** / unit update call (price change, unavailable) | Verification Due (when the 3-month date arrives), Pricing Requested, Off Market, Withdrawn, Under Contract (buyer Closing) |
| Verification Due | Complete the open **Availability Verification** call | Listed (+3 months), Pricing Requested, Off Market, Withdrawn, Under Contract |
| Off Market | Follow-up call (required, with date) | Listed, Withdrawn |
| Under Contract | Read-only; the buyer's closing workflow drives it | Sold, or Listed if the closing is abandoned |
| Sold | Read-only | Terminal |
| Withdrawn | Sales Manager reactivation only | Pricing Requested or Listed |

**Seller Lead rollup:** The Lead takes the first status that applies, in this order:
1. Closed Won, if any Unit is Sold through the CRM.
2. Closing, if any Unit is Under Contract.
3. Negotiating, if a buyer Interest on any of his Units is negotiating.
4. Listed, if any Unit is Listed or Verification Due.
5. Pricing, if any Unit is Pricing Requested or Price Proposed.
6. Otherwise, New or Fresh Lead (the origin).

## 4. Data changes (additive only)

**On the Unit:**
- `listing_status` (read-only)
- `listing_reason`
- `pricing_method`
- `proposed_over_price`
- `pricing_basis`
- `pricing_file` (private, optional)
- `last_verified_on`
- `next_verification_due`
- `verification_overdue`
- `active_buyer_interest_count` and `total_buyer_interest_count`

The counters are refreshed on every Interest transition and reconciled on migrate.

**On actions:** The existing Lead Action Execution gains three new action types (Add Property, Agent Pricing, Price Decision), two new purposes (Pricing Follow-up, Availability Verification) and a new **Unit** scope. That scope gives each Unit its own lock: one open action per Unit, with no duplicate verifications.

**Lead fields:** No new Lead fields. The existing Interested flag, no-answer counters and last-call fields are made visible for Sellers.

**Scheduler:** Every day, a scheduled job does four things:
- It marks Units Verification Due when their planned check date arrives.
- It creates the verification action for any Listed Unit that lacks one, which covers migrated Units.
- It flags overdue checks.
- It alerts the Sales Manager.

It never changes availability by itself; only the agent's recorded result does.

## 5. Decisions needed (recommended answer in bold)

1. **Seller-only statuses.** **Add only Pricing and Listed**, and reuse the shared ones. The alternative is to reuse Requested and Offer Sent for sellers, which is fewer names but confusing in reports.
2. **Remove** Contacted, Nurture, Qualified, Converted, Unqualified, Junk and Offer Selected, with affected Leads recalculated by the rollup. **Yes.**
3. Hide Units that are not Listed (Pricing Requested, Price Proposed, Off Market, Withdrawn) from buyer Smart Match. **Yes.** Primary developer Units are unaffected.
4. Overdue verification: **7 days → Sales Manager alert; 14 days → excluded from new Smart Match results.** Existing buyer Interests are untouched.
5. Unit sold elsewhere or withdrawn while buyers are interested: **block if a buyer is in Closing; otherwise flag the buyer Interests and notify their agents** (no auto-cancel).
6. Existing Units at migration. Priced seller Units → **Listed, with the first verification spread over 30 days**. Unpriced seller Units → **Pricing Requested**, shown on the workboard without auto-creating actions.
7. Agent's proposed price goes **directly to the owner**, with no manager approval. History is kept by Track Changes.
8. Scope: the workflow applies to **any Unit owned by a Seller Lead (Resale, and Rental when owned)**. Primary developer Units have no seller workflow.
9. Unit counter: **show active interested buyers as the headline and total ever in the details.**
10. Log CRM-portal Unit views so the view count includes portal opens, not only Desk opens. **Yes.**

If you approve, the build order is:
1. Backend schema and patch.
2. The shared engine extended to Sellers.
3. Scheduler.
4. Frontend (Seller flags card, Unit Workboard, Unit form panel).
5. Tests.

The two repositories will then be pushed with a deployment and test guide.

## References

[1]: https://dd.reso.org/DD2.0/Property/StandardStatus/ "RESO Data Dictionary 2.0 - StandardStatus field and lookup definitions"
[2]: https://www.nar.realtor/the-facts/consumer-guide-what-goes-into-pricing-your-home "NAR Consumer Guide - What Goes Into Pricing Your Home"
[3]: https://www.nar.realtor/determining-asking-price "NAR - Determining Asking Price"
[4]: https://www.bayut.sa/blog/en/invest-in-saudi/trucheck/ "Bayut Saudi Arabia - TruCheck listing verification"
