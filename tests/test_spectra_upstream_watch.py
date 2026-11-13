import json
import unittest
from pathlib import Path

from scripts.spectra_upstream_watch import API, build_candidate

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads(
    (ROOT / "release" / "spectra-release-manifest.json").read_text(encoding="utf-8")
)


class UpstreamWatchTests(unittest.TestCase):
    def fixture_fetcher(self, overrides=None):
        data = {
            f"{API}/repos/hummingbot/hummingbot/releases/latest": {
                "tag_name": "v2.17.0",
                "published_at": "2026-09-22T08:55:11Z",
            },
            f"{API}/repos/hummingbot/gateway/releases/latest": {
                "tag_name": "v2.17.0",
            },
            f"{API}/repos/hummingbot/condor/commits/main": {
                "sha": "2442752df1259651002034055a32d974da29a361",
            },
            f"{API}/repos/andredepaulnash-cmd/spectra-hummingbot-api/commits/main": {
                "sha": "f8fa4591ed09a966188eb2091a3cde966eac3247",
            },
        }
        data.update(overrides or {})
        return lambda url: data[url]

    def test_no_candidate_when_upstream_matches_manifest(self):
        self.assertIsNone(build_candidate(MANIFEST, self.fixture_fetcher()))

    def test_new_upstream_release_is_candidate_not_approved(self):
        url = f"{API}/repos/hummingbot/hummingbot/releases/latest"
        fetcher = self.fixture_fetcher({url: {
            "tag_name": "v2.18.0",
            "published_at": "2026-11-13T00:00:00Z",
        }})
        candidate = build_candidate(MANIFEST, fetcher)
        self.assertEqual(candidate["changed_components"], ["hummingbot_client"])
        self.assertIn("human review", candidate["promotion_note"])
        self.assertEqual(MANIFEST["release_status"], "candidate")

    def test_prior_candidate_becomes_the_comparison_baseline(self):
        prior = {
            "upstream": {
                "hummingbot_client": {
                    "repository": "hummingbot/hummingbot",
                    "release": "v2.18.0",
                    "published_at": "2026-11-13T00:00:00Z",
                },
                **MANIFEST["upstream_snapshot"],
            }
        }
        url = f"{API}/repos/hummingbot/hummingbot/releases/latest"
        fetcher = self.fixture_fetcher({url: {
            "tag_name": "v2.18.0",
            "published_at": "2026-11-13T00:00:00Z",
        }})
        self.assertIsNone(build_candidate(MANIFEST, fetcher, prior_candidate=prior))

    def test_missing_upstream_version_fails_closed(self):
        url = f"{API}/repos/hummingbot/gateway/releases/latest"
        fetcher = self.fixture_fetcher({url: {}})
        with self.assertRaisesRegex(RuntimeError, "Gateway latest release"):
            build_candidate(MANIFEST, fetcher)


if __name__ == "__main__":
    unittest.main()
