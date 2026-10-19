"""Deliver the outbox to each destination independently.

Every queued event lists the destinations that still owe it (`pending`). A
destination removes itself after a confirmed send and the state is saved at
once, so a failing destination keeps its place in the queue without blocking
or repeating the others. Delivery stays at least once per destination.
"""
import re
import time
from datetime import datetime, timezone

from .events import held
from .providers import safe_error

CHANNEL_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
WEBHOOK_SECRET = re.compile(r"DISCORD_WEBHOOK_URL(?:_[A-Z0-9]+)*")
LEGACY_DISCORD = "discord"  # The destination name used before Discord had channels.


def app_ids(values):
    # A list of IDs, or an object whose keys are IDs and whose values name the game for readers.
    return {str(item).strip().lower() for item in values or [] if str(item).strip()}


def app_keys(event):
    return {str(event.get(key) or "").lower() for key in ("app_id", "bundle_id")} - {""}


def excluded(event, settings):
    return bool(app_keys(event) & app_ids(settings.get("exclude_apps")))


def included(event, settings):
    """`games` is "all" (the default) or the only games this destination receives."""
    games = settings.get("games", "all")
    return games == "all" or bool(app_keys(event) & app_ids(games))


def wants(event, settings):
    """Whether a destination's settings accept this event.

    `events` maps "apple", "google" and "monitor" (health messages) to the kinds
    to send; "*" means every kind, and a missing `events` sends everything.
    `google_tracks` limits Google release events to those tracks. `games` and
    `exclude_apps` choose games; health messages belong to no game.
    """
    if not settings.get("enabled", False):
        return False
    if (events := settings.get("events")) is not None:
        kinds = events.get(event.get("store") or "monitor", [])
        if "*" not in kinds and event.get("kind") not in kinds:
            return False
    if not event.get("store"):
        return True
    tracks = settings.get("google_tracks")
    if (tracks is not None and event["store"] == "google" and not event["kind"].startswith("build_")
            and event.get("track") not in tracks):
        return False
    return included(event, settings) and not excluded(event, settings)


def discord_channels(config, include_disabled=False):
    """Discord channels with their destination names; a section without `channels` is one channel.

    Each channel posts to the webhook in the environment variable (and secret)
    named by `webhook_secret`, so webhook URLs never enter config.json.
    """
    section = config.get("discord", {})
    if not (section.get("enabled", False) or include_disabled):
        return []
    if "channels" not in section:
        legacy = {key: value for key, value in section.items() if key != "enabled"}
        return [{**legacy, "id": LEGACY_DISCORD, "name": "Discord", "webhook_secret": "DISCORD_WEBHOOK_URL",
                 "enabled": True, "destination": LEGACY_DISCORD}]
    channels, seen = [], set()
    for channel in section["channels"]:
        cid, secret = str(channel.get("id", "")), str(channel.get("webhook_secret", ""))
        if not CHANNEL_ID.fullmatch(cid) or cid in seen:
            raise RuntimeError(f"Discord channel id {cid!r} must be unique: lowercase letters, digits and dashes")
        if not WEBHOOK_SECRET.fullmatch(secret):
            raise RuntimeError(f"Discord channel {cid}: webhook_secret must be DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL_<NAME>")
        seen.add(cid)
        if channel.get("enabled", True) or include_disabled:
            channels.append({**channel, "enabled": True, "destination": f"discord/{cid}"})
    return channels

def enabled(config):
    names = ({"telegram"} if config.get("telegram", {}).get("enabled", False) else set())
    names |= {channel["destination"] for channel in discord_channels(config)}
    if not names:
        raise RuntimeError("No destination enabled: set telegram.enabled or discord.enabled in config.json")
    return names


def routes(config):
    """Each enabled destination with the filter from its config.json settings."""
    result = {}
    if config.get("telegram", {}).get("enabled", False):
        result["telegram"] = lambda event, settings=config["telegram"]: wants(event, settings)
    for channel in discord_channels(config):
        result[channel["destination"]] = lambda event, channel=channel: wants(event, channel)
    return result


def prepare(state, active, aliases=None):
    """Upgrade events queued by older versions and drop destinations that were turned off.

    `aliases` renames destinations, so Discord events queued before channels
    existed go to the channel that took over their webhook.
    """
    aliases = aliases or {}
    for event in state["outbox"]:
        # Older events were queued for Telegram only and have no kind to filter on.
        names = [aliases.get(name, name) for name in event.get("pending", ["telegram"])]
        event["pending"] = [name for name in dict.fromkeys(names) if name in active]
        if visible := event.get("visible"):
            event["visible"] = {aliases.get(name, name): kinds for name, kinds in visible.items()}
    state["outbox"] = [event for event in state["outbox"] if event["pending"]]
    return state


def deliver(state, backend, name, sender, now=None):
    """Send this destination's events in order; stop at its first failure and return the error."""
    now = now or datetime.now(timezone.utc)
    for event in list(state["outbox"]):
        # A held event waits for its next step until its batch window ends.
        if name not in event.get("pending", ["telegram"]) or held(event, now):
            continue
        try:
            sender.send(event)
        except Exception as error:  # A rendering bug or outage here must not stop other destinations.
            return safe_error(error)
        event["pending"] = [item for item in event.get("pending", ["telegram"]) if item != name]
        if not event["pending"]:
            state["outbox"].remove(event)
        backend.save(state)  # A storage failure still stops the run: unsaved sends would repeat.
        time.sleep(sender.interval)
    return None


def drain(state, backend, senders, now=None):
    """Returns {destination: error} for destinations that could not finish."""
    failures = {}
    for name, sender in senders.items():
        if error := deliver(state, backend, name, sender, now):
            failures[name] = error
    return failures
