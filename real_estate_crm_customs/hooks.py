app_name = "real_estate_crm_customs"
app_title = "CRM Real Estate Customs"
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
    },
    "daily": [
        "real_estate_crm_customs.real_estate_crm_customs.doctype.real_estate_unit.real_estate_unit.refresh_delivery_statuses",
    ],
}

doc_events = {
    "CRM Lead": {
        "validate": "real_estate_crm_customs.api.guard_crm_lead_workflow",
    },
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
                    "CRM Lead-preferred_unit_type",
                    "CRM Lead-preferred_destination",
                    "CRM Lead-preferred_developer",
                    "CRM Lead-preferred_compound",
                    "CRM Lead-preferred_finishing_type",
                    "CRM Lead-preferred_delivery_time",
                    "CRM Lead-interest_status",
                    "CRM Lead-previous_status",
                    "CRM Lead-workflow_origin_status",
                    "CRM Lead-no_answer_consecutive_count",
                    "CRM Lead-no_answer_total_count",
                    "CRM Lead-last_call_outcome",
                    "CRM Lead-last_call_at",
                    "CRM Lead-lead_age",
                    "CRM Lead-is_primary_buyer",

                    "CRM Lead-interested_in_units",
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
