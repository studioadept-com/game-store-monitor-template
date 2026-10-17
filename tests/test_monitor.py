import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from store_monitor.events import changes, empty_state, reconcile
from store_monitor.http import ApiError
from store_monitor.providers import AppleProvider, GoogleProvider, cached_track_records, google_release
from store_monitor.state import GCSState, LocalState


def release(**kwargs):
    return {"store": "google", "app_id": "com.example.game", "app_name": "Example Game",
            "version": "1.0.2", "track": "production", "status": "published",
            "phase": "inProgress", "rollout": 10, **kwargs}


def snapshot(record):
    return {"google/com.example.game/production": {"store": "google", "app_id": "com.example.game",
            "app_name": "Example Game", "releases": {"12": record}}}


class EventsTest(unittest.TestCase):
    def test_requested_rollout_message(self):
        events = changes(release(), release(rollout=99))
        self.assertEqual(events, ["Example Game, version 1.0.2 — Rollout increased to 99%\nGoogle Play · production"])

    def test_draft_does_not_claim_production(self):
        record = google_release("id", "Example Game", "production", {"name": "1.0.2", "versionCodes": [12], "status": "completed"}, "IN_REVIEW")
        self.assertEqual(record["status"], "in review")
        self.assertIsNone(record["rollout"])
        self.assertFalse(any("production" in text.split("\n")[0] for text in changes(None, record)))

    def test_initial_baseline_then_no_duplicates(self):
        baseline = reconcile(empty_state(), snapshot(release()), {}, set())
        self.assertEqual(len(baseline["outbox"]), 1)
        self.assertIn("Monitoring connected", baseline["outbox"][0]["text"])
        baseline["outbox"].clear()
        self.assertEqual(reconcile(baseline, snapshot(release()), {}, set()), baseline)

    def test_failure_preserves_snapshot_and_alerts_once_then_recovers(self):
        state = reconcile(empty_state(), snapshot(release()), {}, set())
        state["outbox"].clear()
        baseline = copy.deepcopy(state["scopes"])
        for _ in range(5):
            state = reconcile(state, {}, {"google/com.example.game": "HTTP 403"}, set())
        self.assertEqual(state["scopes"], baseline)
        self.assertEqual(len(state["outbox"]), 1)
        state = reconcile(state, {}, {}, {"google/com.example.game"})
        self.assertEqual(len(state["outbox"]), 2)
        self.assertIn("recovered", state["outbox"][-1]["text"])


class ProviderTest(unittest.TestCase):
    def test_google_collect_paginates_discovers_tracks_and_never_commits(self):
        provider = GoogleProvider.__new__(GoogleProvider)
        provider.client = Mock()
        def request(method, url, **kwargs):
            if "apps:search" in url:
                if kwargs["params"].get("pageToken"):
                    return {"apps": [{"packageName": "com.second", "displayName": "Second"}]}
                return {"apps": [{"packageName": "com.example.game", "displayName": "Example Game"}], "nextPageToken": "p2"}
            if method == "POST":
                self.assertTrue(url.endswith("/edits"))
                return {"id": "edit"}
            if url.endswith("/edits/edit/tracks"):
                return {"tracks": [{"track": "production", "releases": [{"name": "1.0.2", "versionCodes": [12], "status": "inProgress", "userFraction": 0.99}]}]}
            if url.endswith("/tracks/production/releases"):
                return {"releases": [{"releaseName": "1.0.2", "activeArtifacts": [{"versionCode": 12}], "releaseLifecycleState": "RELEASE_LIFECYCLE_STATE_PUBLISHED"}]}
            if method == "DELETE":
                return {}
            self.fail("Unexpected API operation")
        provider.client.request.side_effect = request
        snapshots, errors, successes = {}, {}, set()
        provider.collect({"discover_apps": True}, {}, snapshots, errors, successes)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(errors, {})
        self.assertEqual(snapshots["google/com.example.game/production"]["releases"]["12"]["rollout"], 99)
        self.assertEqual(sum(call.args[0] == "DELETE" for call in provider.client.request.call_args_list), 2)

    def test_apple_pagination_and_modern_state(self):
        provider = AppleProvider.__new__(AppleProvider)
        provider.headers, provider.client = {}, Mock()
        provider.client.request.side_effect = [
            {"data": [{"id": "1", "attributes": {"name": "Example Game"}}], "links": {"next": "https://api.appstoreconnect.apple.com/v1/apps?cursor=next"}},
            {"data": []},
            {"data": [{"id": "v1", "attributes": {"versionString": "1.0.2", "appVersionState": "READY_FOR_DISTRIBUTION", "platform": "IOS"}, "relationships": {"appStoreVersionPhasedRelease": {"data": {"id": "phase1"}}}}], "included": [{"id": "phase1", "type": "appStoreVersionPhasedReleases", "attributes": {"phasedReleaseState": "ACTIVE", "currentDayNumber": 6}}]},
        ]
        snapshots, errors, successes = {}, {}, set()
        provider.collect({}, {}, snapshots, errors, successes)
        self.assertEqual(errors, {})
        self.assertEqual(snapshots["apple/1"]["releases"]["v1"]["rollout"], 50)
        self.assertEqual(snapshots["apple/1"]["releases"]["v1"]["status"], "published")

    def test_quota_fallback_never_claims_new_release_is_live(self):
        records = cached_track_records("id", "game", "production", [{"name": "1.0.3", "versionCodes": [13], "status": "completed"}], {"12": release()})
        self.assertIn("publication not yet confirmed", records["13"]["status"])
        self.assertIsNone(records["13"]["rollout"])


class PersistenceTest(unittest.TestCase):
    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"schema": 999}')
            with self.assertRaises(RuntimeError):
                LocalState(path).load()

    def gcs_backend(self, *responses):
        backend = GCSState.__new__(GCSState)
        backend.bucket, backend.name, backend.generation = "bucket", "state.json", 0
        backend.url = "https://storage.googleapis.com/storage/v1/b/bucket/o/state.json"
        backend.client = Mock()
        backend.client.request.side_effect = list(responses)
        return backend

    def test_cloud_storage_state_starts_empty_and_rejects_concurrent_writer(self):
        backend = self.gcs_backend(ApiError("Cloud Storage", 404), ApiError("Cloud Storage", 412))
        self.assertEqual(backend.load(), empty_state())
        with self.assertRaisesRegex(RuntimeError, "another monitor run"):
            backend.save(empty_state())
        self.assertEqual(backend.client.request.call_args.kwargs["params"]["ifGenerationMatch"], 0)


if __name__ == "__main__":
    unittest.main()
