app_name = "real_estate_crm_customs"
app_title = "CRM Real State Customs"
app_publisher = "Cloudy-Z"
app_description = "Real estate business workflow customizations for Frappe CRM"
app_email = "admin@example.com"
app_license = "mit"

required_apps = ["crm"]

after_install = "real_estate_crm_customs.install.after_install"
after_migrate = "real_estate_crm_customs.install.after_migrate"

scheduler_events = {
    "cron": {
        "0 * * * *": [
            "real_estate_crm_customs.api.update_all_lead_ages",
        ]
    }
}

doc_events = {
    "CRM Lead": {
        "validate": "real_estate_crm_customs.api.guard_crm_lead_workflow",
    },
    "Real Estate Unit": {
        "before_insert": "real_estate_crm_customs.real_estate_crm_customs.doctype.real_estate_unit.real_estate_unit.before_insert_generate_sku",
        "validate": "real_estate_crm_customs.real_estate_crm_customs.doctype.real_estate_unit.real_estate_unit.validate_resale_owner",
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
                    "CRM Lead-real_estate_section",
                    "CRM Lead-party_type",
                    "CRM Lead-whatsapp_number",
                    "CRM Lead-selection_tier",
                    "CRM Lead-buyer_requirements_section",
                    "CRM Lead-buyer_budget",
                    "CRM Lead-area_unit",
                    "CRM Lead-preferred_unit_type",
                    "CRM Lead-preferred_area",
                    "CRM Lead-preferred_developer",
                    "CRM Lead-preferred_compound",
                    "CRM Lead-preferred_finishing_type",
                    "CRM Lead-preferred_delivery_time",
                    "CRM Lead-interest_status",
                    "CRM Lead-previous_status",
                    "CRM Lead-no_answer_consecutive_count",
                    "CRM Lead-no_answer_total_count",
                    "CRM Lead-last_call_outcome",
                    "CRM Lead-last_call_at",
                    "CRM Lead-lead_age",
                    "CRM Lead-is_primary_buyer",

                    "CRM Lead-interested_in_units",
                    "CRM Lead-seller_property_section",
                    "CRM Lead-property_title",
                    "CRM Lead-target_asking_price",
                    "CRM Lead-property_code",
                    "CRM Lead-location_reference",
                    "CRM Lead-seller_compound",
                    "CRM Lead-seller_developer",
                    "CRM Lead-seller_unit_type",
                    "CRM Lead-unit_area",
                    "CRM Lead-seller_finishing_type",
                    "CRM Lead-property_documents",
                ],
            ]
        ],
    },
    {
        "dt": "Custom Field",
        "filters": [
            [
                "name",
                "in",
                [
                    "User-real_estate_agent_outreach_section",
                    "User-real_estate_agent_whatsapp_number",
                    "User-real_estate_agent_outreach_email",
                ],
            ]
        ],
    },
]
