import unittest

from store_monitor.events import changes, empty_state, reconcile


def build(**changes):
    return {"kind": "build", "store": "apple", "app_id": "1", "app_name": "Sample Miner",
            "version": "1.0.2", "build_number": "42", "status": "VALID", "track": "IOS",
            "notes": "Added cloud saves", "notes_locale": "en-US", "notes_source": "TestFlight What to Test", **changes}


def scope(record):
    return {"apple/1/builds": {"kind": "builds", "store": "apple", "app_id": "1",
            "app_name": "Sample Miner", "releases": {"build-id": record}}}


class BuildEventsTest(unittest.TestCase):
    def test_processing_completion_includes_exact_build_and_notes(self):
        events = changes(build(status="PROCESSING"), build())
        self.assertEqual(len(events), 1)
        self.assertIn("version 1.0.2 (build 42) — Build completed processing", events[0])
        self.assertIn("TestFlight What to Test (en-US):\nAdded cloud saves", events[0])

    def test_feature_baseline_does_not_report_historical_builds(self):
        state = empty_state()
        state["initialized"] = ["apple"]  # Existing release monitor upgrade.
        state = reconcile(state, scope(build()), {}, set())
        self.assertEqual(state["outbox"], [])
        self.assertEqual(state["history"], [])
        self.assertEqual(state["builds_initialized"], ["apple"])

