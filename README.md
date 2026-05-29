# CRM Real State Customs

This repository is an isolated Frappe custom app that extends the `Cloudy-Z/crm-Real-State` Frappe CRM fork for real-estate brokerage and developer workflows.

The app intentionally keeps business entities, validations, fixtures, patches, and install hooks outside the CRM fork. Portal-facing UI behavior is implemented in the CRM fork through a minimal file-based CRM Lead controller, while this app owns the backend model.

## Main objects

| Object | Purpose |
|---|---|
| Property Developer | Developer/company master record; names are generated from `developer_name`. |
| Real Estate Project | Developer project / compound / building master linked to a Property Developer; names are generated from `project_name`. |
| Real Estate Unit | Sellable inventory unit linked to a project and developer; each unit receives a unique read-only SKU before insert. |
| Lead Interested Unit | Child table linking buyer leads to units of interest. |
| CRM Lead custom fields | Adds `party_type` and `interested_in_units` to the existing CRM Lead DocType. |

## Compatibility
This app is maintained for Frappe Framework v15 and v16, with Frappe CRM installed before this custom app. The package metadata intentionally declares `frappe >=15.0.0,<17.0.0` so Frappe Cloud can resolve it on both supported framework branches.

## Installation

Install this app after `frappe` and `crm` are installed on the site.

```bash
bench get-app crm_real_state_customs https://github.com/Cloudy-Z/crm-Real-State-Customs.git
bench --site your-site.local install-app real_estate_crm_customs
bench --site your-site.local migrate
bench build --app crm
bench --site your-site.local clear-cache
```

If you already installed the app before the latest fields were added, run:

```bash
bench --site your-site.local execute real_estate_crm_customs.install.after_install
bench --site your-site.local migrate
bench --site your-site.local clear-cache
```

## Real Estate Inventory and Portal Configuration

This app now defines a three-tier real-estate inventory hierarchy: **Property Developer**, **Real Estate Project**, and **Real Estate Unit**. Units generate a unique read-only SKU before insert using the project abbreviation, unit type, floor, and a four-digit serial number, for example `COMP-APT-FL02-0042`.

To expose **Real Estate Unit** through Frappe native website portal access, open **DocType > Real Estate Unit** in Desk, enable **Has Web View**, set a route such as `real-estate-units`, and keep access limited to authenticated users by granting read/create permissions only to the relevant portal role or logged-in user role. Do not enable guest read permission for inventory records. If you want end users to create records from the portal, also enable web-form or website permission rules for the intended logged-in role only. After changing the DocType, run `bench --site <site-name> migrate`, clear cache, and verify from a logged-in portal user account.

The installation hook also seeds Client Scripts for dynamic unit defaults, strict CRM Lead phone validation, and a seller workflow button named **Assign Property Unit** that only lists units with status `Available` and adds the selected unit to the lead's interested-units child table.
