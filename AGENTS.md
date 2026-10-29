# AGENTS.md — Real Estate CRM knowledge base for AI agents

This file is the working memory of the project. Read it fully before you change anything in this repository or in the CRM fork. It records the business model, the rules that must never be broken, how the code is organised, how to make each kind of change safely, and the mistakes that already happened so they are not repeated.

The human-readable explanation of the same system is in [`docs/TECHNICAL_GUIDE.md`](docs/TECHNICAL_GUIDE.md). Editable Mermaid diagrams are in [`docs/diagrams/`](docs/diagrams/).

> Snapshot: updated 29 Oct 2026 for the Owner Lead / Unit listing workflow release. Current verification baseline: **144 backend tests OK, 159/159 frontend tests (11 files)**. If the code and this file disagree, the code wins. Fix this file in the same commit.

---

## 1. What the system is

This system is a real-estate brokerage and developer CRM built on **Frappe Framework v16** and **Frappe CRM**, hosted on **Frappe Cloud**. It tracks Buyer Leads, Owner Leads (property owners), inventory Units, Compounds and Phases, Developers and Destinations. Each Buyer requirement and each Unit offered to a Buyer moves through its own workflow, from first call to signed contract.

It is split into two repositories that are deployed to the same site.

| Repository | Branch | Role |
|---|---|---|
| `Cloudy-Z/crm-Real-State-Customs` (this repo, app `real_estate_crm_customs`) | `main` | **Backend authority.** DocTypes, controllers, whitelisted API, workflow rules, Smart Match, migrations, fixtures, install/migrate setup, Desk client scripts. |
| `Cloudy-Z/crm-Real-State` (Frappe CRM fork, app `crm`) | `develop` | **Portal UI** at `/crm` (Vue 3 + frappe-ui + Tailwind + Vite). Lead page, unified action dialog, Smart Match picker, real-estate list/form pages, sidebars, Data Import map. Changes to upstream CRM Python are kept minimal. |

Rule: business rules and data-model changes go in **this app**. UI goes in the **CRM fork**. The frontend never enforces a rule that the server does not also enforce.

## 2. Glossary

| Term in UI | Technical name | Meaning |
|---|---|---|
| Lead | `CRM Lead` | A person. Its role comes from `party_type`. |
| Party Role | `CRM Lead.party_type` | Exactly `Buyer` or `Owner`. The **only** role field. |
| Owner / Owner Lead | `party_type = "Owner"` | Property owner who lists Units. Was called **Seller** before 28 Oct 2026. Stored `Seller` values are migrated to `Owner`. Portal list `/crm/owners`; old `/crm/sellers` links redirect there. |
| Compound | `Real Estate Project` | A compound, or a Phase of one (`record_type = Compound | Phase`, `parent_compound`). |
| Unit | `Real Estate Unit` | Inventory item with a generated SKU. |
| Interest | `Lead Interest` (standalone DocType) | One Buyer requirement or one Unit offered to a Buyer. It has its own workflow status, history and lock. |
| Action | `Lead Action Execution` | A planned or completed agent action (Call, Meeting, Showing, Send Offer, Closing, ...). |
| Workboard | `LeadInterestWorkboard.vue` | Per-Interest rows with row-level action menus. |
| Smart Match | `get_smart_matched_units` | Scored, explainable inventory matching for an Interest. |
| Legacy mirror | `Lead Interested Unit` (child table `interested_in_units`) | Compatibility copy only. **Never authoritative.** |

## 3. Non-negotiable invariants

Breaking any of these has already caused production incidents or violates an explicit user decision.

1. **`party_type` is the only Lead role authority.** Values are `Buyer` or `Owner`. `custom_type` and `lead_type` were removed; they may only appear inside migration/repair code. Use `party_roles.py` helpers (`normalize_party_role`, `is_owner_role`, `OWNER_ROLE`), never string literals scattered in code. Legacy `Seller` input is normalised to `Owner`.
2. **Unit ownership is Owner-only.** `Real Estate Unit.owner_lead` may only point to an Owner Lead. A Buyer is linked to Units **only through `Lead Interest`**. A Lead that owns Units cannot change its role, and a Buyer that has Interests cannot change its role.
3. **`Lead Interest` is the workflow authority.** Each Interest has its own `workflow_status`, transitions (`Lead Interest Transition`), and lock. The child table `Lead Interested Unit` is a dual-write mirror for old screens. Do not read state from it, and do not promote it back to authority.
4. **Lead `status` is a read-only rollup.** Agents never set it. Buyer status is recomputed from Interests by `_rollup_target()` in `interest_workflow.py`; Owner status is derived only from owned Units by `reconcile_owner_lead_rollup()` in `owner_workflow.py`. Used statuses are `New`, `Fresh Lead`, `Requested`, `Pricing`, `Documents Pending`, `Listed`, `Offer Sent`, `Negotiating`, `Closing`, and `Closed Won`. Never use a Buyer Interest to drive an Owner Lead or an Owner Unit to drive a Buyer Lead.
5. **Qualification is separate from status.** `interest_status` (`Interested` / `Not Interested`) is read-only for agents. Only a Sales Manager or System Manager can change it, through `requalify_lead`.
6. **Interests are created only by the workflow.** Direct inserts are blocked unless the code runs inside `interest_creation_context()` (`workflow_context.py`). Do not add "Add Interest" buttons outside the action cycle.
7. **One popup, one atomic submit.** A Buyer action is entered in `LeadUnifiedActionDialog.vue`. Its fields are revealed in sequence and saved with **one** call, `submit_lead_action_bundle`. Agents must be able to fill every requirement field **before** choosing the Next Action. Next Action is validated only at final submit, and only when the workflow requires it. Never remove that final validation.
8. **Notes and comments are optional** on actions.
9. **Closing means a signed contract.** `Closing` comes before `Closed Won`. `Closed Won` needs `contract_reference` **and** `contract_signed_date`. The contract attachment is optional and always **private**. Do not replay or re-run historical action migrations over signed or terminal rows.
10. **Meeting purposes are exactly** `Explore Meeting` (any category), `Owner Meeting` (Resale Unit with an Owner Lead), and `Developer Meeting` (Primary Unit with a Developer). There is no general meeting.
11. **Destination is required** on property requirements. Budgets are Currency, with `*_set` presence flags so that blank is not treated as zero.
12. **Rent exists only under Resale.** Inventory Type is `Resale | Primary | International`. `Listing Purpose` is `For Sale | For Rent | For Sale or Rent`. Interest `deal_type` is `Buy | Rent`. A signed Rent deal makes the Unit `Rented`, not `Sold`.
13. **Unit financial formula** (`unit_financials.py`, `Decimal`, `ROUND_HALF_UP`):
    - Total Price Net = Paid + Over Net + Remaining
    - Property Taxes = Paid × 2.5%
    - Commission = Total Price Net × 2.5%
    - Total Gross = Paid + (Over Net + Property Taxes + Commission) + Remaining
    - If Over Price is entered as gross (`over_is_gross`): Over Net = (Over Price − 5% × Paid − 2.5% × Remaining) / 1.025
14. **Owner listing eligibility is server-enforced.** A Unit linked to an Owner must be `Listed` (or in the 14-day resolution grace after Verification Due), available, correctly priced, and have accepted **privately uploaded** ownership/ID evidence plus a signed, privately uploaded listing agreement before new Buyer matching or direct Interest binding. Primary Units and historical Units without an Owner listing state are unaffected.
15. **Migrations are additive and idempotent.** Never drop columns or delete user data in a patch. Never call `frappe.db.commit()` in code reachable from `after_migrate`. Wrap per-record repair in savepoints and log skipped records instead of failing the whole migrate.
16. **The user's process:** for complex workflow or data changes, first deliver a design with **editable Mermaid** diagrams. Get approval, then code. Review every affected DocType JSON, controller, API, patch and UI **before** editing to avoid repeated field/schema failures.

## 4. Repository map (backend)

```
real_estate_crm_customs/
  hooks.py                 after_install/after_migrate, scheduler, doc_events, fixtures
  api.py                   ~4100 lines: whitelisted API, action engine, Smart Match, guards
  interest_workflow.py     Interest lifecycle: ALLOWED_TRANSITIONS, locks, closing, Buyer Lead rollup, legacy promotion
  owner_workflow.py        Owner Lead/Unit acquisition, pricing, documents, listing gate, view/count APIs, daily verification
  requirement_policy.py    pure validators: currency/presence flags, delivery date, scope, deal type, listing purposes
  party_roles.py           canonical role helpers (Buyer/Owner, legacy Seller alias)
  lead_role_migration.py   role repair, saved-view filter/route repair, enforce_canonical_party_role_schema
  workflow_context.py      request-local guards: interest_creation_context, interest/unit mutation contexts
  unit_financials.py       Decimal financial formula
  unit_taxonomy.py         DEFAULT_UNIT_CATEGORIES (16 categories with their Unit Types)
  master_data.py           idempotent seeding of Unit Types / Unit Categories
  migration_policy.py      pure helpers used by patches (normalisation decisions)
  install.py               custom fields, statuses, property setters, Desk client scripts, portal layouts/views/quick filters
  fixtures/custom_field.json   CRM Lead + User custom fields (synced by fixtures)
  patches.txt              [pre_model_sync] / [post_model_sync] sections (both headers required)
  patches/v16_0/*.py       additive, idempotent patches
  real_estate_crm_customs/doctype/<19 DocTypes>   JSON + same-name .py controller each
tests/                     12 pure-Python test modules (no bench needed)
docs/                      TECHNICAL_GUIDE.md, diagrams/*.mmd, proposals/
tools/compound_import/     workbook builder/verifier for Compound + Phase + Amenity import
```

Key DocTypes (all in module `Real Estate CRM Customs`):

| DocType | Notes |
|---|---|
| `Real Estate Unit` | `allow_import`, `track_changes`, `track_views`. Owner listing fields are workflow-controlled: pricing method/status, proposal, documents, agreement, verification dates and Buyer counters. Read-only projections include master compound, destination, developer, SKU, financial totals, delivery status, closing links and counters. Children: `Unit Scheduled Showing`, `Real Estate Unit Document`. |
| `Real Estate Project` (Compound) | `allow_import`, `track_changes`. `record_type` Compound/Phase. A Phase inherits Developer and Destination from `parent_compound`. Status is optional. Old `phases` child table is legacy and read-only. Child: `Real Estate Compound Amenity`, `Real Estate Compound Unit Type`. |
| `Property Developer` | Only `developer_name` is required. `founded_year` is an optional **Date** (label "Founded In"). Founders child rows are optional; empty rows are dropped. |
| `Real Estate Destination`, `Real Estate Amenity`, `Real Estate Unit Type`, `Real Estate Unit Category` | Importable masters. Unit Type links to a Unit Category. |
| `Lead Interest` | `track_changes`. Required: lead, record_type, category, deal_type, workflow_status. Holds the requirement snapshot, contract fields, replacement links, and deletion-request fields. |
| `Lead Interest Transition`, `Lead Status Transition` | Audit logs. Never importable. |
| `Lead Action Execution` + child `Lead Action Interest Scope` | Action records. `scope_type` is Lead, Interest, Batch or Unit. Buyer scope uses Interests; Owner Unit actions never carry Interest scopes. `client_request_id` makes submits idempotent. `seller_lead` is labelled "Related Owner Lead" (fieldname kept for compatibility). |
| `Lead Interested Unit` | Legacy mirror child table on CRM Lead. |

Import policy: the 7 masters are importable. Workflow and audit DocTypes (Interest, Action, Transitions, Scopes) are **not** importable, and a schema-contract test enforces this.

## 5. Workflow model

### 5.1 Lead origin and qualification
- A Lead starts as **New** (created manually or by import) or **Fresh Lead** (created through the API or a campaign). The origin is stored in `workflow_origin_status` / `previous_status`. If neither is set, the code falls back to `Fresh Lead` when `source` is set, otherwise `New`.
- `lead_age` is a read-only Data field refreshed every hour by `api.update_all_lead_ages` (format `3d 4h`).
- No-answer logic: each No Answer increases `no_answer_consecutive_count` (current streak) and `no_answer_total_count` (lifetime). An Answered call resets only the streak. `last_call_outcome` and `last_call_at` record the latest call. The call log keeps every No Answer.
- WhatsApp: the agent types a subject, the system records it (`record_whatsapp_subject`), and then opens the chat.

### 5.2 Allowed Lead-level actions (`_allowed_action_definitions`, api.py)
- **Owner Leads use the independent `owner_workflow.py` action service**, not the Buyer action engine. Its Lead-level actions are Initial Qualification Call, Add Property, General Follow-up, and Explore Meeting; Unit-level actions are Agent Pricing, Price Decision, Listing Documents and Availability Verification.
- Buyer that is not yet Interested: only **Call → Initial Qualification**.
- Interested Buyer: **Add Interest** (Requirements Discovery), **Call** (General Follow-up), **Meeting** (Explore Meeting), plus **Send Offer** (Batch) when there are Matched Interests with no open action.

### 5.3 Interest lifecycle (`ALLOWED_TRANSITIONS`, interest_workflow.py)

```
Requested        -> Matched, Superseded, Fulfilled, Cancelled
Matched          -> Offer Sent, Superseded, Cancelled
Offer Sent       -> Offer Viewed, Offer Accepted, Rejected, Superseded
Offer Viewed     -> Offer Accepted, Rejected, Superseded
Offer Accepted   -> Negotiating, Showing Scheduled, Closing, Rejected, Superseded
Negotiating      -> Showing Scheduled, Closing, Rejected, Superseded
Showing Scheduled-> Shown - Interested, Shown - Considering, Negotiating, Rejected
Shown - Considering -> Negotiating, Showing Scheduled, Shown - Interested, Rejected
Shown - Interested  -> Negotiating, Showing Scheduled, Closing, Rejected
Closing          -> Negotiating, Rejected, Closed Won
Terminal: Closed Won, Rejected, Superseded, Fulfilled, Cancelled
```

Row actions per state are defined in `_allowed_interest_action_definitions` (api.py). An Interest on a Unit that is Sold or Rented, or Reserved by **another** Interest, has no actions.

### 5.4 Lead status rollup (`_rollup_target`)
The first matching rule wins:
1. **Closed Won**: any Interest is Closed Won with contract evidence.
2. **Closing**: any active Interest is in Closing.
3. **Negotiating**: any active Interest is Offer Accepted, Negotiating, Showing Scheduled, Shown - Considering or Shown - Interested.
4. **Offer Sent**: any active Interest is Offer Sent or Offer Viewed.
5. **Requested**: any active Interest is Requested.
6. Otherwise the **origin** status (New or Fresh Lead).

Matched alone does not move the Lead. A rejected offer therefore drops the Lead back to its previous stage; it does not mark the Lead lost.

### 5.5 Next Action rule (`_next_action_is_required`)
- Not required when the outcome is Rescheduled, or after a batch Send Offer (each offered row gets its own menu instead).
- Initial Qualification call: required when the call was not answered, or when the Lead was qualified as Interested.
- Otherwise: required while any Interest in the action's scope is still active, or, for Lead-scoped actions, while the Lead is Interested.

### 5.6 Owner linkage from the Buyer cycle
- When an **Owner Meeting** is planned, an Event is created on the Unit's Owner Lead.
- When a **Showing** is planned, a row is appended to `Unit.scheduled_showings` and an Event is created on the Owner Lead.

### 5.7 Closing and Unit status
- `begin_interest_closing`: allowed only from Offer Accepted, Negotiating or Shown - Interested, and only when the Unit is Available. It locks the **Unit first, then the Interest**, sets the Unit to `Reserved`, and sets `closing_interest`.
- `finalize_interest_contract`: requires the contract reference and signed date. It sets the Interest to Closed Won and the Unit to `Sold` (Buy) or `Rented` (Rent), and sets `sold_interest`. A replay with the same values is a no-op; a replay with different values raises an error.
- `abandon_interest_closing(target="Negotiating" | "Rejected")`: returns the Unit to `Available` and moves the Interest to the target. Any other target is rejected.
- `release_rented_unit` (manager only): moves a Rented Unit back to Available but marks its Owner listing Verification Due and creates/reconciles an Owner confirmation action before it becomes Buyer-matchable again.
- Agents cannot set Unit status to Reserved, Sold or Rented by hand.

### 5.8 Smart Match (`get_smart_matched_units` → `get_available_units_for_selection` → `_score_property_match`)
- **Hard filters:** status Available; owned Units must pass `unit_is_buyer_eligible` (Listed or limited Verification Due grace); `listing_purpose` fits the deal type (`listing_purposes_for_deal`); inventory type within the search scope (`Resale | Primary | Both`); Rent searches Resale only; a Compound filter includes its Phases through `master_compound`.
- **Relation validation:** the Compound must belong to the selected Destination and Developer, and the Unit Type must belong to the selected Unit Category.
- **Weights:** Destination 30, Budget 30, Unit Type 22, Compound 16, Developer 12, Delivery 12, Finishing 10. Score = weighted sum / applicable weight × 100.
- **Budget bands:** within budget 1.0; up to 10% over 0.75; up to 20% over 0.45; more 0. Rent deals compare the monthly rent budget.
- **Levels:** Excellent ≥ 85, Good ≥ 65, Possible ≥ 40, otherwise Alternative. "Unscored" when no criteria are given. Each card returns at most 4 reasons and 3 gaps. Results are sorted by score descending, then price ascending.

### 5.9 Permissions
- "Manager" means `System Manager` or `Sales Manager` (`_is_manager`).
- Manager-only actions: `requalify_lead`, `review_interest_deletion`, `release_rented_unit`.
- Deleting an Interest goes through a request (`request_interest_deletion` creates a ToDo), followed by a manager decision.
- `_lock_lead_workflow` uses `SELECT ... FOR UPDATE` on the Lead row to serialise Buyer workflow writes. `owner_workflow._lock_lead` / `_lock_unit` provide the same serialization for Owner actions.

### 5.10 Retired endpoints
These still exist only to throw "Use the current scoped workflow action form": `schedule_next_action`, `log_meeting_result`, `send_offer_to_lead`, `mark_lead_negotiating`, `mark_unit_interest_lost`. Do not revive them. All Buyer writes go through `plan_lead_action`, `start_lead_action`, `complete_lead_action`, `cancel_lead_action` and `submit_lead_action_bundle`.

## 6. Install and migrate lifecycle (Frappe v16)

Order inside `bench migrate`:
1. `[pre_model_sync]` patches.
2. DocType JSON sync.
3. `[post_model_sync]` patches.
4. Fixtures (Custom Fields on CRM Lead and User).
5. `after_migrate` → `sync_real_estate_crm_defaults()`.

Consequences:
- A post-model patch **cannot rely on fixture Custom Fields** (for example `CRM Lead.party_type`) existing yet. Guard with `frappe.db.has_column` / `meta.has_field`, or re-run the idempotent part from `after_migrate`. `closing_requirements_and_meetings.execute()` is re-run there for exactly this reason.
- Changing a field type that needs a data conversion requires a **pre_model_sync** patch. Example: `convert_developer_founded_year_to_date` turns integers like `2005` into `2005-01-01`, because strict MariaDB refuses the Int→Date column change otherwise.
- Frappe re-syncs a DocType JSON when its file hash or `modified` stamp changes. Bump `"modified"` whenever you edit a DocType JSON.
- `sync_real_estate_crm_defaults()` runs: module def, default Unit Types, Lead custom fields, User agent fields, Lead statuses, phone field cleanup, status read-only Property Setter, Desk client scripts, portal defaults (layouts, standard views, quick filters), standalone Interest backfill, then the closing patch re-run. `after_install` also seeds Unit Categories **once**; later migrations never re-create categories a user deleted.

`patches.txt` must contain **both** section headers. Current order:

```
[pre_model_sync]
real_estate_crm_customs.patches.v0_0_1.setup_crm_lead_customizations
real_estate_crm_customs.patches.v16_0.convert_developer_founded_year_to_date
[post_model_sync]
real_estate_crm_customs.patches.v16_0.unify_real_estate_fields
real_estate_crm_customs.patches.v16_0.remove_legacy_lead_role_fields
real_estate_crm_customs.patches.v16_0.repair_historical_lead_roles
real_estate_crm_customs.patches.v16_0.repair_legacy_lead_view_filters
real_estate_crm_customs.patches.v16_0.repair_legacy_lead_view_schema
real_estate_crm_customs.patches.v16_0.closing_requirements_and_meetings
real_estate_crm_customs.patches.v16_0.compound_phases_and_unit_categories
real_estate_crm_customs.patches.v16_0.rename_seller_to_owner_and_rentals
real_estate_crm_customs.patches.v16_0.owner_unit_listing_workflow
```

A Frappe patch runs once per site (it is recorded in `Patch Log`). To run new logic, add a **new** patch line. Do not edit an old patch and expect it to run again.

## 7. How to make common changes

### Add or change a field on a custom DocType
1. Read the DocType JSON, its controller, every API/serializer that lists its fields, `install.py` layouts/views/quick filters, frontend config (`frontend/src/utils/realEstate.js`), and tests that assert the schema.
2. Edit the JSON at text level so the original formatting is preserved (some files are compact, some expanded). Add the field to `field_order` and bump `modified`.
3. Validate with a strict parser: `orjson` (Frappe's parser) rejects things `json` accepts.
4. If old rows need values, add a new idempotent post-model patch that uses ORM or portable SQL (no MariaDB-only `UPDATE ... JOIN`).
5. Update `install.py` layouts (Desk and portal), list columns and quick filters if the field should be visible.
6. Add or adjust tests in `tests/test_schema_contract.py` and the feature test file.

### Add a field to CRM Lead
Add it to `fixtures/custom_field.json` **and** to the fixture name filter in `hooks.py`. Mind `insert_after` ordering: a cycle broke a migration before. Remember that fixtures sync **after** post-model patches.

### Add a new DocType
Every DocType, including child tables, needs a JSON file **and** a same-name Python controller module (`class X(Document): pass` is enough). Missing child controllers broke v16 sync once. `link_filters` must be four-value rows: `[[doctype, field, operator, value]]`. Decide `allow_import` / `track_changes` explicitly; schema tests lock these flags.

### Change a workflow rule
Change `ALLOWED_TRANSITIONS` or the action definitions in one place. Update the rollup only if a new state must affect Lead status. Then update the matrix in `docs/TECHNICAL_GUIDE.md` and `docs/diagrams/interest_state_machine.mmd`, and add tests in `tests/test_closing_services.py` / `tests/test_action_contracts.py`.

### Add a portal real-estate master page
Add an entry to `REAL_ESTATE_ENTITIES` in `frontend/src/utils/realEstate.js` (routes, list columns, summary fields). Add it to `AppSidebar.vue` and `MobileSidebar.vue`, to the Data Import map (shared helper in realEstate.js), and to the backend master list in `api.py` / `install.py`. Update `tests/unit/realEstateConfig.test.js`.

## 8. Verification commands

Backend (no bench required; pure tests use fake `frappe` modules):

```bash
cd crm-Real-State-Customs
PYTHONPATH=tests python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m venv /tmp/oj && /tmp/oj/bin/pip install -q orjson
/tmp/oj/bin/python -c "import orjson,glob;[orjson.loads(open(f,'rb').read()) for f in glob.glob('real_estate_crm_customs/**/*.json',recursive=True)];print('json ok')"
python3 -m py_compile $(git ls-files '*.py')
git diff --check
```

Frontend (Yarn lockfile; **do not** commit pnpm/npm artifacts; restore `frontend/auto-imports.d.ts` and `components.d.ts` if the build regenerates them):

```bash
cd crm-Real-State/frontend
yarn install --frozen-lockfile
yarn test:run
npx eslint <changed files>
yarn build          # vite build --base=/assets/crm/frontend/ && copy-html-entry
git checkout -- auto-imports.d.ts components.d.ts   # if only regenerated
```

There is no local bench or site database in the agent sandbox. Real migrations happen only on Frappe Cloud, so tests must emulate lifecycle order, historical data, and Frappe parsing rules.

## 9. Deployment

1. Push the backend to `main`. Deploy the custom app on Frappe Cloud **first** and let migrate finish.
2. Push the CRM fork to `develop`. Deploy it **second** (rebuilds the portal assets).
3. In the browser, hard refresh (Ctrl+Shift+R) or clear the service worker cache.
4. Run the acceptance checklist of the relevant release guide.

## 10. Incident log: what went wrong and the lesson

| Symptom | Real cause | Fix / lesson |
|---|---|---|
| `Value missing for CRM Lead: Mobile No.` / `Phone` while WhatsApp was filled | Duplicate phone fields with competing `reqd` Property Setters | One phone field kept (`mobile_no`, fieldtype Phone). Property Setters for reqd/hidden/unique/length are cleaned by `enforce_crm_lead_phone_mandatory`. |
| `Please select a country code for field whatsapp_number` | Frappe validates **every** Phone-type field with `phonenumbers` and needs `+CC` | Portal `PhoneInput.vue` (libphonenumber-js) stores the Desk format `+CC-NUMBER`, for example `+20-1070009839`. |
| Internal Server Error when entering `+20...` | `phonenumbers` Python package not installed (ImportError) | Declared in `pyproject.toml` dependencies. |
| `'Mobile No.' will get truncated, max 11` | Old `length` Property Setter made the column `varchar(11)` | Property Setter removed and column restored to the Phone default `varchar(140)`, done by install code, not manual SQL. |
| Phone looked wrong only in the portal | The portal had no Phone control; Desk did | Built `PhoneInput.vue` to read and write the same format as Desk, with no data migration. |
| `You are not permitted to access this resource. Login to access` / `frappe.client.insert is not whitelisted` (seen as Administrator) | `frappe.is_whitelisted()` shows this one combined message when the method is not whitelisted **or the request runs as Guest**. `frappe.client.insert` is whitelisted, so the real cause is a lost/expired session or a request sent without the session cookie | Check the session first (log out and in, check `frappe.session.user` in the Network response). Do not add whitelists for core methods. |
| Buyer/Seller lists both showed Sellers | Duplicate role fields (`custom_type`, `lead_type`) and one shared `Leads` route that cached query filters | Single `party_type`, historical repair patches, dedicated Buyers/Owners routes. |
| Administrator alone saw an empty Lead list | Their private `CRM View Settings` still filtered/columned on the removed `custom_type` | Patch rewrites saved-view filters, columns and rows to `party_type`. Check user-owned views when only one user is affected. |
| Patch registry read as an app name | `patches.txt` lacked a complete section header | Both `[pre_model_sync]` and `[post_model_sync]` are required; a test enforces it. |
| v16 sync failed on child DocType | Child DocType had no Python controller | Every DocType needs a controller file; a test enforces it. |
| Link filter tuple validation error | `link_filters` used object syntax | Use four-value rows. |
| `after_migrate` stopped on one bad Interest (Resale vs Primary) | Migration copied contradictory legacy data | Derive the category from the Unit, use per-record savepoints, log and continue. |
| Developer JSON failed to parse after a GitHub web edit | Hand edit left invalid JSON that Frappe's `orjson` rejects | Always validate JSON with orjson. Int→Date needed a pre-model conversion patch. |
| `A Developer requires at least one founder.` | Controller rule plus required child field | Only Developer Name is required; empty founder rows are dropped. |
| Requirement fields could not be typed until Next Action was chosen | Link dropdown buttons inside `<form>` had no `type`, so they defaulted to `submit` and triggered final validation | `type="button"` on Autocomplete buttons; regression tests in `autocompleteFormSafety.test.js`. |
| `Cannot read properties of null (reading 'action_type')` on "Start now" (`Lead.vue`) | The page read the planned action from a response that was still empty when "Do now" ran | Fixed in the fork (`6a6ab4e3`, `e575b11e`, `eaef924e`), later replaced by the atomic single-submit bundle (backend `4568f36`). Never dereference an optional action result. |
| Unused Lead statuses still offered after deleting all Leads | `CRM Lead Status` records (including upstream CRM defaults) remain until removed explicitly | Migrate references before retiring a status. `ensure_real_estate_lead_statuses` upserts every used Buyer and Owner status, including Pricing, Documents Pending and Listed. |

General diagnosis method: find the exact lifecycle phase that fails (patch discovery, DocType sync, fixtures, `after_migrate`, request). Read the server traceback in Frappe Cloud → Site → Error Logs, or in the browser Network tab (`exc` field). Trace every input that reaches the failing validator, then reproduce it in a pure test before fixing.

## 11. Owner workflow — implemented contract

- **Service:** `owner_workflow.py` is intentionally separate from Buyer `Lead Interest` lifecycle. Both write `Lead Action Execution`, but Owner Unit actions use `scope_type = Unit` and are excluded from Interest-scope migration.
- **Cycle:** Initial qualification call → full sectioned Add Property intake → either Agent Pricing → Price Decision or Owner Price → Documents Collection → Listed → recurring Availability Verification every three months. Notes remain optional; each completed row creates at most one successor action/Event.
- **Evidence gate:** Listing requires a sale/rent price appropriate to Listing Purpose, ownership proof, Owner ID/passport, agreement type and signed date. Optional uploaded evidence must be private. No scheduler marks a Unit available by itself.
- **Visibility:** `unit_is_buyer_eligible()` is used by Smart Match, manual Unit selection and direct Buyer binding. `LeadInterest.validate` is the final backstop.
- **Buyer linkage:** Buyer Owner Meetings and Showings continue to create their Owner Lead/Unit projections. Buyer closing and lease-release events refresh Owner rollups without changing Buyer authority.
- **Quarterly job:** `run_daily_listing_verification` changes a due Listed Unit to Verification Due, creates exactly one open verification action/Event, and logs a manager-visible comment after seven overdue days. After 14 days it is no longer matched to newly created Buyer Interests.
- **Insights:** `Lead Interest` hooks refresh distinct active/total Buyer counts on each linked Unit. Portal Unit forms call `log_unit_view` because `frappe.client.get` does not write View Log; the form displays view and Buyer counters.
- **Manager recovery:** Only a manager may reactivate Withdrawn Units or return a Rented Unit to market; the latter requires fresh Owner verification.

## 12. Working style expected by the user

- The user writes in English mixed with Arabic. "استمرار" means "continue".
- Give an editable visual design (Mermaid) before complex changes, and do the full review before editing.
- Prefer industry best practice, cite sources when claiming it, and adapt it to this CRM.
- Fix things in the app (install/patches), not by asking the user to run SQL on the live database.
- Make fresh installs clean: the same code path must work on a new site and on an upgraded one.
- After a fix, explain the cause, the fix and the method, and give a retest checklist.
- Commits: conventional messages (`feat:`, `fix:`, `docs:`, `test:`) with the trailer `Co-authored-by: Manus <dev-agent@manus.ai>`.
