# CRM Real State Customs

This repository is an isolated Frappe custom app that extends the `Cloudy-Z/crm-Real-State` Frappe CRM fork for real-estate brokerage and developer workflows.

The app intentionally keeps business entities, validations, fixtures, patches, and install hooks outside the CRM fork. Portal-facing UI behavior is implemented in the CRM fork through a minimal file-based CRM Lead controller, while this app owns the backend model.

## Main objects

| Object | Purpose |
|---|---|
| Real Estate Project | Developer project / compound / building master. |
| Real Estate Unit | Sellable or resale inventory unit linked to a project. |
| Lead Interested Unit | Child table linking buyer leads to units of interest. |
| CRM Lead custom fields | Adds `party_type` and `interested_in_units` to the existing CRM Lead DocType. |

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
