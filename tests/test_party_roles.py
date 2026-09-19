import unittest

from real_estate_crm_customs.party_roles import normalize_party_role


class PartyRoleTests(unittest.TestCase):
    def test_seller_aliases(self):
        for value in ("Seller", "seller lead", " Owner ", "SELL"):
            self.assertEqual(normalize_party_role(value), "Seller")

    def test_buyer_aliases(self):
        for value in ("Buyer", "buyer lead", " Purchaser ", "BUY"):
            self.assertEqual(normalize_party_role(value), "Buyer")

    def test_unknown_values_do_not_guess(self):
        self.assertIsNone(normalize_party_role("Investor"))
        self.assertIsNone(normalize_party_role(""))


if __name__ == "__main__":
    unittest.main()
