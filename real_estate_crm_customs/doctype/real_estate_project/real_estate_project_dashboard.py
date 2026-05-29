from frappe import _


def get_data():
    return {
        "fieldname": "project",
        "transactions": [
            {
                "label": _("Real Estate"),
                "items": ["Real Estate Unit"],
            }
        ],
    }
