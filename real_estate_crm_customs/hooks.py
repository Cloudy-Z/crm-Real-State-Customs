app_name = "real_estate_crm_customs"
app_title = "CRM Real State Customs"
app_publisher = "Cloudy-Z"
app_description = "Real estate business workflow customizations for Frappe CRM"
app_email = "admin@example.com"
app_license = "mit"

required_apps = ["crm"]

after_install = "real_estate_crm_customs.install.after_install"

doc_events = {
    "Real Estate Unit": {
        "validate": "real_estate_crm_customs.real_estate_crm_customs.doctype.real_estate_unit.real_estate_unit.validate_resale_owner"
    }
}

fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            [
                "name",
                "in",
                [
                    "CRM Lead-party_type",
                    "CRM Lead-real_estate_section",
                    "CRM Lead-interested_in_units",
                ],
            ]
        ],
    }
]
