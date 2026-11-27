import unittest

from spectra_alpha2.config import Alpha2Config
from spectra_alpha2.policy import (
    INITIAL_TARGET_POSITIONS,
    MAX_NEW_POSITIONS_PER_CYCLE,
    MAX_OPEN_POSITIONS,
)


def base_env():
    # Minimal safe paper-only env; no exchange credentials, no secrets printed.
    return {
        "SPECTRA_PAPER_ONLY": "true",
        "SPECTRA_ALLOW_LIVE_EXECUTION": "false",
        "SPECTRA_PAPER_CONNECTORS": "kraken_paper_trade",
        "SPECTRA_PAPER_CONNECTOR": "kraken_paper_trade",
        "SPECTRA_HB_API_USERNAME": "local-core",
        "SPECTRA_HB_API_PASSWORD": "internal-only",
        "SPECTRA_DB_PATH": ":memory:",
    }


class Alpha2CapacityConfigTests(unittest.TestCase):
    def test_policy_constants(self):
        self.assertEqual(MAX_OPEN_POSITIONS, 74)
        self.assertEqual(INITIAL_TARGET_POSITIONS, 50)
        self.assertEqual(MAX_NEW_POSITIONS_PER_CYCLE, 10)

    def test_application_configuration_defaults_target_50_per_cycle_10(self):
        cfg = Alpha2Config.from_environment(base_env())
        self.assertEqual(cfg.target_positions, 50)
        self.assertEqual(cfg.max_new_positions_per_cycle, 10)
        self.assertEqual(cfg.target_positions, INITIAL_TARGET_POSITIONS)
        self.assertEqual(cfg.max_new_positions_per_cycle, MAX_NEW_POSITIONS_PER_CYCLE)
        # normal operating target remains 50 even though hard limit is 74
        self.assertEqual(MAX_OPEN_POSITIONS, 74)

    def test_configured_target_50_and_74_valid_75_rejected(self):
        for target in (50, 74):
            env = {**base_env(), "SPECTRA_MAX_POSITIONS": str(target)}
            self.assertEqual(Alpha2Config.from_environment(env).target_positions, target)
        for bad in ("75", "0", "-1", "1000", "not-an-int"):
            env = {**base_env(), "SPECTRA_MAX_POSITIONS": bad}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Alpha2Config.from_environment(env)

    def test_per_cycle_default_and_maximum_both_10_through_application_path(self):
        self.assertEqual(Alpha2Config.from_environment(base_env()).max_new_positions_per_cycle, 10)
        # A setting can lower the per-cycle maximum...
        env = {**base_env(), "SPECTRA_MAX_NEW_POSITIONS_PER_CYCLE": "1"}
        self.assertEqual(Alpha2Config.from_environment(env).max_new_positions_per_cycle, 1)
        env = {**base_env(), "SPECTRA_MAX_NEW_POSITIONS_PER_CYCLE": "10"}
        self.assertEqual(Alpha2Config.from_environment(env).max_new_positions_per_cycle, 10)
        # ...but an environment setting cannot silently raise it above 10.
        for bad in ("11", "50", "100", "0", "-1", "not-an-int"):
            env = {**base_env(), "SPECTRA_MAX_NEW_POSITIONS_PER_CYCLE": bad}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Alpha2Config.from_environment(env)



    def test_application_build_runtime_applies_limits_and_cannot_raise_per_cycle(self):
        import asyncio
        import os
        import tempfile
        from unittest.mock import patch

        from spectra_alpha2 import app

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **base_env(),
                "SPECTRA_DB_PATH": os.path.join(tmp, "app.sqlite3"),
                "SPECTRA_HB_API_URL": "http://localhost:8000",
            }
            with patch.dict(os.environ, env, clear=True):
                config, governor, data_client, execution, supervisor = app.build_runtime()
                try:
                    # Actual application path: Alpha2Config -> app -> PortfolioSupervisor -> CapitalGovernor.
                    self.assertEqual(config.target_positions, 50)
                    self.assertEqual(config.max_new_positions_per_cycle, 10)
                    self.assertEqual(supervisor.target_positions, 50)
                    self.assertEqual(supervisor.max_new_positions_per_cycle, 10)
                    self.assertIs(supervisor.governor, governor)
                    self.assertEqual(MAX_OPEN_POSITIONS, 74)
                    self.assertEqual(supervisor.target_positions, INITIAL_TARGET_POSITIONS)
                    self.assertEqual(supervisor.max_new_positions_per_cycle, MAX_NEW_POSITIONS_PER_CYCLE)
                finally:
                    asyncio.run(data_client.close())
                    asyncio.run(execution.close())
            # An environment variable cannot silently raise per-cycle admission above 10 in the real app path.
            with patch.dict(os.environ, {**env, "SPECTRA_MAX_NEW_POSITIONS_PER_CYCLE": "11"}, clear=True):
                with self.assertRaises(ValueError):
                    app.build_runtime()
            # 50-position target does not cap hard limit; configured target 74 is valid in the real app path.
            with patch.dict(os.environ, {**env, "SPECTRA_MAX_POSITIONS": "74"}, clear=True):
                config, governor, data_client, execution, supervisor = app.build_runtime()
                try:
                    self.assertEqual(supervisor.target_positions, 74)
                    self.assertEqual(MAX_OPEN_POSITIONS, 74)
                finally:
                    asyncio.run(data_client.close())
                    asyncio.run(execution.close())


if __name__ == "__main__":
    unittest.main()
