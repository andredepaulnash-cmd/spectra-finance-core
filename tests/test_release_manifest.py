import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseManifestTests(unittest.TestCase):
    def test_component_inventory_and_tracking_modes_are_explicit(self):
        manifest = json.loads((ROOT / "release" / "components.json").read_text())
        self.assertEqual(manifest["schema_version"], 1)
        components = {item["id"]: item for item in manifest["components"]}
        self.assertEqual(
            set(components),
            {"spectra-core", "condor", "hummingbot-api", "hummingbot-client", "gateway"},
        )
        self.assertEqual(components["condor"]["tracking"], "branch")
        self.assertEqual(components["hummingbot-api"]["tracking"], "branch")
        self.assertEqual(components["hummingbot-client"]["tracking"], "latest-release")
        self.assertEqual(components["gateway"]["tracking"], "latest-release")

    def test_no_component_is_preapproved_without_runtime_baseline(self):
        manifest = json.loads((ROOT / "release" / "components.json").read_text())
        self.assertTrue(all(item["approved_ref"] is None for item in manifest["components"]))

    def test_release_policy_is_fail_closed(self):
        policy = json.loads((ROOT / "release" / "components.json").read_text())["policy"]
        self.assertFalse(policy["automatic_apply"])
        self.assertFalse(policy["automatic_merge"])
        self.assertTrue(policy["require_compatibility_ci"])
        self.assertTrue(policy["require_operator_approval"])
        self.assertTrue(policy["preserve_local_state"])
        self.assertTrue(policy["paper_only_default"])
        self.assertFalse(policy["live_execution_default"])


if __name__ == "__main__":
    unittest.main()
