import unittest
from unittest.mock import Mock, patch

from store_monitor.http import ApiError
from store_monitor.providers import GoogleProvider


class PollingTest(unittest.TestCase):
    def setUp(self):
        self.now = 100_000
        self.clock = patch("store_monitor.providers.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sleep = patch("store_monitor.providers.time.sleep").start()
        self.addCleanup(patch.stopall)
        self.config = {"discover_apps": False, "apps": {f"com.game{i}": f"Game {i}" for i in range(9)},
                       "monitor_builds": True, "lifecycle_max_requests": 6}
        self.calls = []
        self.failure = None
        self.fraction = 0.1
        self.provider = GoogleProvider.__new__(GoogleProvider)
        self.provider.client = Mock()
        self.provider.client.request.side_effect = self.request

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST" and url.endswith("/edits"):
            return {"id": "read-edit"}
        if method == "DELETE":
            return {}
        if url.endswith("/bundles"):
            return {"bundles": [{"versionCode": 42}]}
        if url.endswith("/apks"):
            return {"apks": []}
        if url.endswith("/edits/read-edit/tracks"):
            return {"tracks": [{"track": "production", "releases": [{"name": "1.0.2", "versionCodes": [42],
                                                                       "status": "inProgress", "userFraction": self.fraction}]}]}
        if url.endswith("/releases"):
            if self.failure:
                raise self.failure
            return {"releases": [{"releaseName": "1.0.2", "activeArtifacts": [{"versionCode": 42}],
                                   "releaseLifecycleState": "RELEASE_LIFECYCLE_STATE_PUBLISHED"}]}
        self.fail(f"Unexpected method: {method}")

    def collect(self, previous=None):
        self.calls.clear()
        snapshots, errors, successes = {}, {}, set()
        self.provider.collect(self.config, previous or {}, snapshots, errors, successes)
        return snapshots, errors, successes

    def lifecycle_calls(self):
        return [call for call in self.calls if call[1].endswith("/releases")]

    def test_quota_stops_after_one_call_and_cooldown_survives_restart(self):
        for error in (ApiError("Google Play", 403, "PERMISSION_DENIED", "Listing releases quota exceeded."),
                      ApiError("Google Play", 429)):
            with self.subTest(status=error.status):
                self.failure = error
                previous, errors, _ = self.collect()
                self.assertEqual(len(self.lifecycle_calls()), 1)
                self.assertIn("google/lifecycle_api", errors)
                self.assertEqual(len(previous), 18)
                self.now += 301
                previous, errors, successes = self.collect(previous)
                self.assertEqual(self.lifecycle_calls(), [])
                self.assertIn("google/lifecycle_api", errors)
                self.assertNotIn("google/lifecycle_api", successes)
                self.failure = None
                self.now += 3600
                _, errors, successes = self.collect(previous)
                self.assertNotIn("google/lifecycle_api", errors)
                self.assertIn("google/lifecycle_api", successes)
                self.assertLessEqual(len(self.lifecycle_calls()), 6)
