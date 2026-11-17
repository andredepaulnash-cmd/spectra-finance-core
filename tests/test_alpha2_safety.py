import unittest

from spectra_alpha2.safety import PaperSafetyError, PaperSafetySettings


class PaperOnlySafetyTests(unittest.TestCase):
    def test_exact_paper_environment_is_accepted(self):
        settings = PaperSafetySettings.from_environment({
            "SPECTRA_PAPER_ONLY": "true",
            "SPECTRA_ALLOW_LIVE_EXECUTION": "false",
        })
        self.assertTrue(settings.paper_only)
        self.assertFalse(settings.allow_live_execution)
        self.assertEqual(settings.connector, "kraken_paper_trade")

    def test_missing_or_false_paper_flag_fails_closed(self):
        for env in ({}, {"SPECTRA_PAPER_ONLY": "false", "SPECTRA_ALLOW_LIVE_EXECUTION": "false"}):
            with self.subTest(env=env), self.assertRaises(PaperSafetyError):
                PaperSafetySettings.from_environment(env)

    def test_true_or_missing_live_flag_fails_closed(self):
        for live in ("true", None):
            env = {"SPECTRA_PAPER_ONLY": "true"}
            if live is not None:
                env["SPECTRA_ALLOW_LIVE_EXECUTION"] = live
            with self.subTest(live=live), self.assertRaises(PaperSafetyError):
                PaperSafetySettings.from_environment(env)

    def test_exchange_credentials_are_rejected(self):
        env = {
            "SPECTRA_PAPER_ONLY": "true",
            "SPECTRA_ALLOW_LIVE_EXECUTION": "false",
            "KRAKEN_API_KEY": "must-never-be-present",
        }
        with self.assertRaises(PaperSafetyError):
            PaperSafetySettings.from_environment(env)

    def test_live_connector_override_is_rejected(self):
        env = {
            "SPECTRA_PAPER_ONLY": "true",
            "SPECTRA_ALLOW_LIVE_EXECUTION": "false",
            "SPECTRA_PAPER_CONNECTOR": "kraken",
        }
        with self.assertRaises(PaperSafetyError):
            PaperSafetySettings.from_environment(env)

    def test_order_boundary_requires_paper_connector_and_governor_auth(self):
        settings = PaperSafetySettings.from_environment({
            "SPECTRA_PAPER_ONLY": "true",
            "SPECTRA_ALLOW_LIVE_EXECUTION": "false",
        })
        settings.validate_order_boundary("kraken_paper_trade", "auth-1")
        for connector, auth in (("kraken", "auth-1"), ("kraken_paper_trade", None)):
            with self.subTest(connector=connector, auth=auth), self.assertRaises(PaperSafetyError):
                settings.validate_order_boundary(connector, auth)


if __name__ == "__main__":
    unittest.main()
