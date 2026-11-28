from decimal import Decimal
import unittest

from inbox.balance_profiles import (
    carriers, catalog_profiles, manual_profile, network_hint, parse_balance,
    validate_recognition,
)


class BalanceParsingTests(unittest.TestCase):
    def test_decimal_formats(self):
        for value, expected in (
            ("0", "0"), ("12.50", "12.50"), ("12,50", "12.50"),
            ("1,234.56", "1234.56"), ("1.234,56", "1234.56"),
            ("1 234,56", "1234.56"), ("1\u202f234,56", "1234.56"),
            ("-12.50", "-12.50"), ("−12,50", "-12.50"),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_balance(f"Balance: {value} EUR", {"currency": "EUR"}), Decimal(expected))

    def test_currency_prefix_and_data_coexist(self):
        self.assertEqual(parse_balance("Your balance is £12.50, data 2 GB", {"currency": "GBP"}), Decimal("12.50"))
        self.assertEqual(parse_balance("Guthaben: 12,50 €", {"currency": "EUR"}), Decimal("12.50"))

    def test_fail_closed_on_ambiguous_or_unrelated_numbers(self):
        for value in (
            "Balance: 1,234 EUR", "Balance: 1.234 EUR", "Balance: 12.05.2026 EUR",
            "Balance: 12/05/2026 EUR", "Balance 2 GB", "Balance: 12.50 USD",
            "Balance EUR 10; bonus EUR 20", "Balance EUR 10; renewal EUR 10",
            "Renewal price EUR 10", "Balance 2 GB; top up EUR 10", "Balance expires 2026; EUR 10",
            "Balance: EUR 1 23", "Balance: EUR 12.1234", "Balance: 10 EUR MB",
            "Balance bonus: EUR 10", "Balance EUR 10; bonus USD 20", "Balance: 9999999999999 EUR", "Balance: \x0010 EUR",
            "Balance: EUR 10\n" + "x" * 4096,
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_balance(value, {"currency": "EUR"}))

    def test_literal_marker_pins_amount_and_agreed_currency(self):
        profile = {"currency": "USD", "marker": "Main credit: "}
        self.assertEqual(parse_balance("Data 3GB; Main credit: 10.25; bonus $5", profile), Decimal("10.25"))
        self.assertEqual(parse_balance("Main credit: $0", profile), Decimal("0"))
        for text in ("Main credit: 2 GB", "Main credit: 12/05/2026", "Main credit: 10 EUR", "Main credit: 10e5", "Main credit: 10GB", "Main credit: 10; Main credit: 20"):
            self.assertIsNone(parse_balance(text, profile))
        self.assertIsNone(parse_balance("Main credit: 10", {"currency": "", "marker": "Main credit: "}))
        self.assertEqual(parse_balance("Balance: 350 XOF", {"currency": "XOF"}), Decimal("350"))

    def test_invalid_profile_does_not_parse(self):
        for profile in ({}, None, {"currency": "eur"}, {"currency": "EUR", "marker": ".*\n"}):
            self.assertIsNone(parse_balance("Balance: 10 EUR", profile))


class BalanceProfileTests(unittest.TestCase):
    def test_catalogue_is_detached_and_explicit(self):
        self.assertGreaterEqual(len(carriers()), 7)
        for carrier in carriers():
            self.assertTrue(network_hint(carrier["network"]))
            for route in catalog_profiles(carrier["id"]):
                self.assertIn("source", route)
                self.assertEqual(route["eligible"], route["transport"] == "ussd")
                if route["transport"] != "ussd":
                    self.assertEqual(route["sender"], "")
        profiles = catalog_profiles("telekom-de")
        profiles[0]["code"] = "changed"
        self.assertEqual(catalog_profiles("telekom-de")[0]["code"], "*100#")
        self.assertEqual(catalog_profiles("unknown"), [])

    def test_manual_route_and_recognition_validation(self):
        payload = {"transport": "sms", "destination": "333", "text": "Saldo", "sender": "333", "currency": "MXN"}
        profile = manual_profile(payload)
        self.assertEqual(profile["sender"], "333")
        self.assertEqual(profile["timeout"], 90)
        self.assertEqual(manual_profile({**payload, "text": ""})["text"], "")
        self.assertEqual(profile["id"], "manual")
        self.assertEqual(validate_recognition(profile), {"currency": "MXN", "marker": ""})
        self.assertEqual(manual_profile({**payload, "currency": ""})["currency"], "")
        for change in (
            {"sender": ""}, {"sender": ".*"}, {"currency": "mxn"}, {"currency": "ZZ"},
            {"marker": "\n"}, {"marker": " "}, {"marker": "x" * 81}, {"timeout": True},
            {"timeout": "30"}, {"timeout": 4}, {"timeout": 301},
            {"destination": "333;whoami"}, {"code": "*100#"}, {"transport": "voice"},
            {"eligible": True}, {"label": "bad\nlabel"}, {"currency": None},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                manual_profile({**payload, **change})

    def test_code_replies_require_exact_sender(self):
        payload = {"transport": "code", "code": "*777#", "currency": "USD", "sender": "+15551234567"}
        self.assertEqual(manual_profile(payload)["sender"], "+15551234567")
        with self.assertRaises(ValueError):
            manual_profile({**payload, "sender": ""})
        with self.assertRaises(ValueError):
            manual_profile({**payload, "code": "*777#;x"})
        self.assertEqual(manual_profile({"transport": "ussd", "code": "*100#"})["currency"], "")

    def test_network_hints_are_only_mcc_mnc(self):
        for value in ("26201", "310410"):
            self.assertEqual(network_hint(value), value)
        for value in (None, 26201, "262", "2620100", "2620x", "26201 "):
            self.assertEqual(network_hint(value), "")
