import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from store_monitor import discord, settings
from store_monitor.__main__ import legacy_aliases, senders
from store_monitor.delivery import discord_channels, drain, enabled, prepare, routes
from store_monitor.events import detect, empty_state, reconcile
from store_monitor.formatting import TELEGRAM_LIMIT, discord_payload, length, telegram_html
from store_monitor.http import ApiError
from store_monitor.state import LocalState

WHEN = datetime(2026, 10, 17, 12, 5, tzinfo=timezone.utc)
TZ = "UTC"
ROUTER = routes
SHIPPED = json.loads((Path(__file__).parent.parent / "config.json").read_text())
# Telegram gets everything; Discord uses the shipped milestone filter and leaves one game out.
MAIN = "discord/main"
CONFIG = {"telegram": {"enabled": True},
          "discord": {"enabled": True, "channels": [{**SHIPPED["discord"]["channels"][0], "exclude_apps": ["com.example.legacy"]}]}}


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
        both, internal = [["telegram", MAIN]], [["telegram"]]
        pending = lambda old, new, config=CONFIG: [event["pending"] for event in queued(old, new, config)]
        self.assertEqual(pending(apple(status="draft"), apple()), both)
        self.assertEqual(pending(google(), google(rollout=50)), both)
        self.assertEqual(pending(None, apple(status="draft")), internal)
        self.assertEqual(pending(apple(), apple(status="rejected")), internal)
        self.assertEqual(pending(google(track="beta"), google(track="beta", rollout=50)), internal)
        legacy = google(app_id="com.example.legacy")
        self.assertEqual(pending(legacy, {**legacy, "rollout": 50}), internal)
        discord_only = {"discord": {"enabled": True}}  # The layout before channels: one channel, old name.
        self.assertEqual(pending(None, apple(status="draft"), discord_only), [["discord"]])
        with self.assertRaisesRegex(RuntimeError, "No destination enabled"):
            enabled(SHIPPED)  # Neither destination is on until the owner chooses.


class ChannelsTest(unittest.TestCase):
    STUDIO = {"id": "studio-a", "name": "Studio A", "webhook_secret": "DISCORD_WEBHOOK_URL_STUDIO_A",
              "games": {"com.example.miner": "Sample Miner"}, "events": {"apple": ["*"], "google": ["*"]}}

    def test_each_channel_gets_its_own_games_and_events(self):
        config = {"telegram": {"enabled": True}, "discord": {"enabled": True, "channels": [*CONFIG["discord"]["channels"], self.STUDIO]}}
        miner = google(app_id="com.example.miner", app_name="Sample Miner")
        pending = lambda old, new: [event["pending"] for event in queued(old, new, config)]
        self.assertEqual(pending(miner, {**miner, "rollout": 50}), [["telegram", MAIN, "discord/studio-a"]])
        self.assertEqual(pending(google(), google(rollout=50)), [["telegram", MAIN]])  # The runner is not Studio A's.
        self.assertEqual(pending({**miner, "track": "beta"}, {**miner, "track": "beta", "rollout": 50}),
                         [["telegram", "discord/studio-a"]])  # Studio A takes every track.
        off = {"telegram": {"enabled": True}, "discord": {"enabled": True, "channels": [{**self.STUDIO, "enabled": False}]}}
        self.assertEqual([e["pending"] for e in queued(miner, {**miner, "rollout": 50}, off)], [["telegram"]])
        self.assertEqual([c["destination"] for c in discord_channels(off, include_disabled=True)], ["discord/studio-a"])
        legacy = {"discord": {"enabled": True, "exclude_apps": []}}  # Before channels: one channel, old name.
        self.assertEqual([c["destination"] for c in discord_channels(legacy)], ["discord"])
        for bad in ({**self.STUDIO, "id": "Studio A"}, {**self.STUDIO, "webhook_secret": "TELEGRAM_BOT_TOKEN"}):
            with self.assertRaises(RuntimeError):
                discord_channels({"discord": {"enabled": True, "channels": [bad]}})

    def test_queued_events_move_to_the_channel_on_the_old_webhook(self):
        state = empty_state()
        state["outbox"] = [{"id": 1, "text": "x", "pending": ["telegram", "discord"], "visible": {"discord": ["published"]}}]
        state = prepare(state, enabled(CONFIG), legacy_aliases(CONFIG))
        self.assertEqual(state["outbox"][0]["pending"], ["telegram", MAIN])
        self.assertEqual(state["outbox"][0]["visible"], {MAIN: ["published"]})
        config = {"telegram": {"enabled": True}, "discord": {"enabled": True, "channels": [*CONFIG["discord"]["channels"], self.STUDIO]}}
        errors = {}
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
                                       "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/a",
                                       "DISCORD_WEBHOOK_URL_STUDIO_A": "https://evil.example/hook"}):
            outputs = senders(config, TZ, errors)
        self.assertEqual(set(outputs), {"telegram", MAIN})
        self.assertEqual(errors, {"delivery/discord/studio-a": "DISCORD_WEBHOOK_URL_STUDIO_A is not a Discord webhook URL"})

    def test_settings_page_lists_games_once_and_keeps_config_style(self):
        state = {"scopes": {
            "apple/1": {"store": "apple", "app_id": "1", "bundle_id": "com.example.miner", "app_name": "Sample Miner"},
            "apple/1/builds": {"store": "apple", "app_id": "1", "kind": "builds"},
            "google/com.example.miner": {"store": "google", "app_id": "com.example.miner", "app_name": "Miner"},
            "google/com.example.miner/production": {"store": "google", "app_id": "com.example.miner",
                                                        "releases": {"r": {"track": "qa", "app_name": "Miner"}}}}}
        found = settings.games(state, CONFIG)
        miner = next(game for game in found if game["key"] == "com.example.miner")
        self.assertEqual(miner["names"], {"apple": "Sample Miner", "google": "Miner"})
        self.assertEqual(miner["ids"], ["1", "com.example.miner"])
        self.assertTrue(next(game for game in found if game["key"] == "com.example.legacy")["unseen"])
        self.assertIn("qa", settings.google_tracks(state, SHIPPED))
        self.assertEqual(json.loads(settings.dumps(SHIPPED)), SHIPPED)
        # Five-minute checks of 80 s fit the free allowance; two-minute ones don't.
        self.assertEqual((settings.estimate(5, 80)["checks"], settings.estimate(5, 80)["cost"]), (8640, 0))
        self.assertEqual(settings.estimate(5, 80)["cost_without_allowance"], 4.58)
        self.assertEqual(settings.estimate(2, 80)["cost"], 6.23)
        self.assertEqual([settings.interval(settings.cron(m)) for m in (5, 60)], [5, 60])
        self.assertIsNone(settings.interval("0 9 * * 1"))
        with tempfile.TemporaryDirectory() as directory:
            page = settings.Settings(Path(directory) / "config.json", state, None, {"GCP_PROJECT": "p", "GCP_REGION": "r"})
            with self.assertRaises(ValueError):
                page.set_schedule(minutes=1)  # Only the listed intervals.
            with self.assertRaises(RuntimeError):
                page.save({"discord": {"enabled": True, "channels": [{**self.STUDIO, "webhook_secret": "ASC_PRIVATE_KEY"}]}})
            with self.assertRaises(ValueError):
                page.store_webhook("DISCORD_WEBHOOK_URL", "https://example.com/not-discord")


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
        self.assertEqual(event["pending"], ["telegram", MAIN])
        message = telegram_html(event, TZ)
        self.assertIn("— <b>Rollout at 20%</b>\n", message)  # The latest step leads.
        self.assertIn("Rollout <b>20%</b>\n<i>Earlier: Moved to production</i>", message)
        # The milestone channel shows only the steps its filter allows, so no "Earlier" line either.
        embed = discord_payload(event, TZ, MAIN)["embeds"][0]
        self.assertTrue(embed["title"].endswith("— Moved to production"))
        self.assertNotIn("description", embed)
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
        self.assertEqual(event["title"], "Build completed processing")
        self.assertIn("— Build completed processing\nApp Store · IOS\nEarlier: Build uploaded — processing", event["text"])
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
