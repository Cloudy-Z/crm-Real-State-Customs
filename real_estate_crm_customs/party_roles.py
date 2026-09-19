"""Canonical real-estate party roles shared by runtime validation and migrations."""


PARTY_ROLE_ALIASES = {
    "buyer": "Buyer",
    "buyer lead": "Buyer",
    "buy": "Buyer",
    "purchaser": "Buyer",
    "seller": "Seller",
    "seller lead": "Seller",
    "sell": "Seller",
    "owner": "Seller",
}


def normalize_party_role(value):
    normalized = " ".join(str(value or "").strip().lower().split())
    return PARTY_ROLE_ALIASES.get(normalized)
