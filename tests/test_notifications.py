import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from store_monitor import discord
from store_monitor.delivery import drain, enabled, prepare, routes
from store_monitor.events import detect, empty_state, reconcile
from store_monitor.formatting import TELEGRAM_LIMIT, discord_payload, length, telegram_html
from store_monitor.http import ApiError
from store_monitor.state import LocalState

WHEN = datetime(2026, 10, 17, 12, 5, tzinfo=timezone.utc)
TZ = "UTC"
ROUTER = routes
SHIPPED = json.loads((Path(__file__).parent.parent / "config.json").read_text())
# Telegram gets everything; Discord uses the shipped milestone filter and leaves one game out.
CONFIG = {"telegram": {"enabled": True},
          "discord": {**SHIPPED["discord"], "enabled": True, "exclude_apps": ["com.example.legacy"]}}


def google(**fields):
    return {"store": "google", "app_id": "com.example.runner", "app_name": "Sample Runner", "version": "1.0.21",
            "track": "production", "status": "published", "phase": "inProgress", "rollout": 20,
            "build_number": "210", "notes": "Bug fixes", "notes_locale": "en-US",
            "notes_source": "Google Play release notes", **fields}


def apple(**fields):
    return {"store": "apple", "app_id": "1000000001", "bundle_id": "com.example.clicker", "app_name": "Sample Clicker",
            "version": "1.1.1", "track": "IOS", "status": "waiting for review", "phase": None, "rollout": None,
            "build_number": "57", "notes": "New levels", "notes_locale": "en-US",
            "notes_source": "App Store release notes", **fields}


def queued(old, new, config=CONFIG):
    """The outbox after one observed change on an already baselined store."""
    state = empty_state()
    state["initialized"] = ["apple", "google"]
    scope = f"{new['store']}/{new['app_id']}"
    state["scopes"][scope] = {"store": new["store"], "app_id": new["app_id"], "app_name": new["app_name"],
                              "releases": {"r": old} if old else {}}
    snapshot = {scope: {**state["scopes"][scope], "releases": {"r": new}}}
    return reconcile(state, snapshot, {}, set(), observed_at=WHEN, destinations=routes(config))["outbox"]


class RoutingTest(unittest.TestCase):
    def test_kinds_and_filters(self):
        kinds = lambda old, new: [found["kind"] for found in detect(old, new)]
        self.assertEqual(kinds(apple(status="draft"), apple()), ["waiting_for_review"])
        self.assertEqual(kinds(google(status="in review", phase=None, rollout=None), google()), ["published", "rollout_started"])
        self.assertEqual(kinds(google(), google(rollout=50)), ["rollout_changed"])
        self.assertEqual(kinds(google(rollout=50), google(phase="completed", rollout=100)), ["rollout_completed"])
        both, internal = [["telegram", "discord"]], [["telegram"]]
        pending = lambda old, new, config=CONFIG: [event["pending"] for event in queued(old, new, config)]
        self.assertEqual(pending(apple(status="draft"), apple()), both)
        self.assertEqual(pending(google(), google(rollout=50)), both)
        self.assertEqual(pending(None, apple(status="draft")), internal)
        self.assertEqual(pending(apple(), apple(status="rejected")), internal)
        self.assertEqual(pending(google(track="beta"), google(track="beta", rollout=50)), internal)
        legacy = google(app_id="com.example.legacy")
        self.assertEqual(pending(legacy, {**legacy, "rollout": 50}), internal)
        discord_only = {"discord": {"enabled": True}}
        self.assertEqual(pending(None, apple(status="draft"), discord_only), [["discord"]])
        with self.assertRaisesRegex(RuntimeError, "No destination enabled"):
            enabled(SHIPPED)  # Neither destination is on until the owner chooses.


class BatchingTest(unittest.TestCase):
    def run_change(self, state, old, new, when, hold):
        scope = f"{new['store']}/{new['app_id']}"
        state["scopes"][scope] = {"store": new["store"], "app_id": new["app_id"], "app_name": new["app_name"],
                                  "releases": {"r": old} if old else {}}
        snapshot = {scope: {**state["scopes"][scope], "releases": {"r": new}}}
        return reconcile(state, snapshot, {}, set(), observed_at=when, destinations=ROUTER(CONFIG), hold_seconds=hold)

    def test_steps_seen_in_one_run_become_one_message(self):
        event, = queued(google(status="in review", phase=None, rollout=None), google())
        self.assertEqual(event["kinds"], ["published", "rollout_started"])
        self.assertEqual(event["pending"], ["telegram", "discord"])
        self.assertIn("— <b>Moved to production → Rollout at 20%</b>", telegram_html(event, TZ))
        # The milestone channel shows only the steps its filter allows.
        self.assertTrue(discord_payload(event, TZ)["embeds"][0]["title"].endswith("— Moved to production"))
        # Events queued before batching have no steps and still render.
        old = {"kind": "published", "title": "Moved to production", "store": "google", "app_name": "Game"}
        self.assertIn("— <b>Moved to production</b>", telegram_html(old, TZ))

    @patch("store_monitor.delivery.time.sleep")
    def test_in_between_step_waits_for_the_next_one(self, _):
        build = {**apple(), "kind": "build", "status": "PROCESSING", "notes_source": "TestFlight What to Test"}
        state = self.run_change(empty_state() | {"initialized": ["apple"]}, None, build, WHEN, 600)
        self.assertIn("hold_until", state["outbox"][0])
        sender = Mock(interval=0)
        drain(state, Mock(), {"telegram": sender}, now=WHEN + timedelta(minutes=4))
        sender.send.assert_not_called()  # Still inside the batch window.
        state = self.run_change(state, build, {**build, "status": "VALID"}, WHEN + timedelta(minutes=5), 600)
        event, = state["outbox"]
        self.assertEqual(event["title"], "Build uploaded — processing → Build completed processing")
        self.assertNotIn("hold_until", event)  # A settled state goes out at once.
        self.assertEqual([entry["kinds"] for entry in state["history"]], [["build_processing"], ["build_processed"]])

    @patch("store_monitor.delivery.time.sleep")
    def test_held_step_goes_out_alone_when_the_window_ends(self, _):
        build = {**apple(), "kind": "build", "status": "PROCESSING", "notes_source": "TestFlight What to Test"}
        state = self.run_change(empty_state() | {"initialized": ["apple"]}, None, build, WHEN, 600)
        sender = Mock(interval=0)
        drain(state, Mock(), {"telegram": sender}, now=WHEN + timedelta(minutes=11))
        self.assertEqual([call.args[0]["kinds"] for call in sender.send.call_args_list], [["build_processing"]])
        later = self.run_change(state, build, {**build, "status": "VALID"}, WHEN + timedelta(minutes=12), 600)
        self.assertEqual([event["kinds"] for event in later["outbox"]], [["build_processed"]])
        unheld = self.run_change(empty_state() | {"initialized": ["apple"]}, None, build, WHEN, 0)
        self.assertNotIn("hold_until", unheld["outbox"][0])  # batch_window_seconds = 0 turns holding off.


class DeliveryTest(unittest.TestCase):
    @patch("store_monitor.delivery.time.sleep")
    def test_destinations_fail_independently_and_old_events_migrate(self, _):
        with tempfile.TemporaryDirectory() as directory:
            backend = LocalState(Path(directory) / "state.json")
            state = empty_state()
            # The first event predates per-destination delivery, so it is Telegram-only.
            state["outbox"] = [{"id": 1, "text": "old"}, {"id": 2, "text": "new", "pending": ["telegram", "discord"]}]
            backend.save(state)
            state = prepare(backend.load(), {"telegram", "discord"})
            telegram, broken = Mock(interval=0), Mock(interval=0)
            broken.send.side_effect = ApiError("Discord", 404)
            self.assertEqual(drain(state, backend, {"telegram": telegram, "discord": broken}), {"discord": "Discord: HTTP 404"})
            self.assertEqual([call.args[0]["id"] for call in telegram.send.call_args_list], [1, 2])
            self.assertEqual(backend.load()["outbox"], [{"id": 2, "text": "new", "pending": ["discord"]}])
            telegram, fixed = Mock(interval=0), Mock(interval=0)
            drain(backend.load(), backend, {"telegram": telegram, "discord": fixed})
            telegram.send.assert_not_called()  # Never repeated on the other destination.
            self.assertEqual(fixed.send.call_count, 1)
            self.assertEqual(backend.load()["outbox"], [])


class FormatTest(unittest.TestCase):
    def test_telegram_html_is_escaped_and_fits(self):
        hostile = apple(app_name="<b>Tap & Go</b>", notes="&<>" * 3000)
        message = telegram_html(queued({**hostile, "status": "draft"}, hostile)[0], "UTC")
        self.assertTrue(message.startswith("🍎 <b>&lt;b&gt;Tap &amp; Go&lt;/b&gt;</b> — <b>Waiting for review</b>"))
        self.assertIn("Observed 2026-10-17 12:05 UTC", message)
        self.assertLessEqual(length(message), TELEGRAM_LIMIT)
        self.assertIsNone(telegram_html({"id": 1, "text": "old"}, "UTC"))  # Old events go out as plain text.

    def test_discord_embed_is_escaped_and_never_pings(self):
        hostile = apple(app_name="**Big** _game_", notes="# Heading <@123> " + "*" * 3000)
        payload = discord_payload(queued({**hostile, "status": "draft"}, hostile)[0], "UTC")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], r"\*\*Big\*\* \_game\_ — Waiting for review")
        self.assertTrue(embed["fields"][-1]["value"].startswith(r"\# Heading \<@123>"))
        self.assertLessEqual(length(embed["fields"][-1]["value"]), 1024)


class DiscordSenderTest(unittest.TestCase):
    URL = "https://discord.com/api/webhooks/123/SECRET-token"

    @patch("store_monitor.discord.time.sleep")
    def test_rate_limit_is_honoured_and_token_never_logged(self, sleep):
        limited = Mock(status_code=429, headers={})
        limited.json.return_value = {"retry_after": 1.5}
        session = Mock()
        session.request.side_effect = [limited, Mock(status_code=200, headers={})]
        discord.Discord(self.URL, session=session).post({"embeds": []})
        sleep.assert_called_once_with(1.5)
        session.request.side_effect = requests.ConnectionError(self.URL)
        with self.assertRaises(ApiError) as error:
            discord.Discord(self.URL, session=session).post({"embeds": []})
        self.assertNotIn("SECRET", str(error.exception))

    def test_webhook_url_is_optional_and_validated(self):
        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "none"}):
            self.assertIsNone(discord.webhook_url())
        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://discord.com.evil.io/api/webhooks/1/x"}):
            with self.assertRaises(RuntimeError):
                discord.webhook_url()


if __name__ == "__main__":
    unittest.main()
