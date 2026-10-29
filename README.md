# CRM Real Estate Customs

A Frappe custom app that turns **Frappe CRM** into a CRM for a real-estate brokerage and for developer (primary) sales. It runs on **Frappe Framework v16** and **Frappe Cloud**, alongside the CRM fork [`Cloudy-Z/crm-Real-State`](https://github.com/Cloudy-Z/crm-Real-State) (branch `develop`), which provides the `/crm` portal UI.

This app owns the data model, business rules, workflow engine, Smart Match, migrations, Desk scripts and portal layouts. The CRM fork owns the Vue screens.

## Documentation

| Document | For |
|---|---|
| [docs/TECHNICAL_GUIDE.md](docs/TECHNICAL_GUIDE.md) | Full technical guide for people: architecture, data model, Buyer workflow, Interest lifecycle, closing, Smart Match, Compounds and Phases, Sale/Rent, APIs, migrations, testing, deployment, troubleshooting. |
| [AGENTS.md](AGENTS.md) | Rulebook and knowledge base for AI coding agents: invariants, file map, how to make changes safely, verification commands, incident lessons. |
| [docs/diagrams/](docs/diagrams/) | Editable Mermaid sources for every diagram. |
| [docs/proposals/owner_unit_workflow/](docs/proposals/owner_unit_workflow/seller_unit_workflow_design.md) | Implemented Owner Lead/Unit listing workflow contract and editable Mermaid diagrams. |
| [tools/compound_import/](tools/compound_import/IMPORT_GUIDE.md) | Converter and verifier for importing Destinations, Developers, Amenities, Compounds and Phases from a spreadsheet. |
| [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) | Early implementation notes (historical; the technical guide is current). |

## What it adds

- **Buyer and Owner Leads.** `party_type` on CRM Lead is the only role field.
- **Guided Buyer action cycle.** Call or WhatsApp first, then qualification, requirements, Smart Match, offers, meetings, showings and closing. Every action is recorded in one popup with one atomic submit and a required next action.
- **Independent Interests.** Each requirement or offered Unit is its own `Lead Interest`, with its own status, history and lock. The Lead status is a read-only summary of its Interests.
- **Signed-contract closing.** Closing reserves the Unit. Contract Signed (reference and signed date required, private file optional) marks the Interest Closed Won and the Unit Sold or Rented.
- **Inventory masters.** Units, Compounds (with Phases), Developers, Destinations, Unit Categories, Unit Types and Amenities, each with portal list and form pages and import support.
- **Smart Match.** Ranks available Units against a requirement with explainable scores, Resale/Primary/Both scope, Buy/Rent deal type and dependent filters.
- **Sale and Rent.** Units are listed For Sale, For Rent or both. Buyer Interests are Buy or Rent.
- **Guided Owner listing workflow.** Initial qualification, full Unit intake, Owner-vs-agent pricing, private ownership/ID/agreement evidence, Listed gate and a three-month availability verification.
- **Owner listing insights.** A Unit tracks changes/views and shows distinct active and total linked-Buyer counts in the portal.

## Requirements

- Frappe Framework v16 (package metadata allows `>=15,<17`; v16 is the tested target).
- Frappe CRM fork `Cloudy-Z/crm-Real-State`, installed **before** this app.
- Python 3.10 or later, and the Python dependency `phonenumbers` (declared in `pyproject.toml`).

## Install

```bash
bench get-app https://github.com/Cloudy-Z/crm-Real-State.git --branch develop
bench get-app https://github.com/Cloudy-Z/crm-Real-State-Customs.git --branch main
bench --site your-site install-app crm
bench --site your-site install-app real_estate_crm_customs
bench --site your-site migrate
bench build --app crm
bench --site your-site clear-cache
```

All setup (custom fields, statuses, property setters, client scripts, layouts, views, default master data) runs automatically in `after_install` and `after_migrate`. Do not run setup functions by hand, and do not fix data with manual SQL. Every fix belongs in a patch or in the migrate hooks, so fresh and upgraded sites behave the same.

## Deploy on Frappe Cloud

1. Deploy this app first and wait for the migrate to finish.
2. Deploy the CRM fork second, so the portal assets are rebuilt.
3. Hard refresh the browser (Ctrl+Shift+R) or clear the service worker.

## Test

```bash
# backend (pure Python, no bench needed)
PYTHONPATH=tests python3 -m unittest discover -s tests -p 'test_*.py'

# frontend (in the CRM fork)
cd frontend && yarn install --frozen-lockfile && yarn test:run && yarn build
```
