"""Deliver the outbox to each destination independently.

Every queued event lists the destinations that still owe it (`pending`). A
destination removes itself after a confirmed send and the state is saved at
once, so a failing destination keeps its place in the queue without blocking
or repeating the others. Delivery stays at least once per destination.
"""
import time

from .providers import safe_error

DESTINATIONS = ("telegram", "discord")


def excluded(event, settings):
    # A list of IDs, or an object whose keys are IDs and whose values name the game for readers.
    denied = {str(item).strip().lower() for item in settings.get("exclude_apps", []) if str(item).strip()}
    return bool({str(event.get(key) or "").lower() for key in ("app_id", "bundle_id")} & denied)


def wants(event, settings):
    """Whether a destination's config.json section accepts this event.

    `events` maps "apple", "google" and "monitor" (health messages) to the kinds
    to send; "*" means every kind, and a missing `events` sends everything.
    `google_tracks` limits Google release events to those tracks.
    """
    if not settings.get("enabled", False):
        return False
    if (events := settings.get("events")) is not None:
        kinds = events.get(event.get("store") or "monitor", [])
        if "*" not in kinds and event.get("kind") not in kinds:
            return False
    tracks = settings.get("google_tracks")
    if (tracks is not None and event.get("store") == "google" and not event["kind"].startswith("build_")
            and event.get("track") not in tracks):
        return False
    return not excluded(event, settings)


def enabled(config):
    names = {name for name in DESTINATIONS if config.get(name, {}).get("enabled", False)}
    if not names:
        raise RuntimeError("No destination enabled: set telegram.enabled or discord.enabled in config.json")
    return names


def routes(config):
    return {name: (lambda event, settings=config.get(name, {}): wants(event, settings)) for name in DESTINATIONS}


def prepare(state, active):
    """Upgrade events queued before per-destination delivery and drop destinations that were turned off."""
    for event in state["outbox"]:
        # Events without `pending` predate per-destination delivery and were Telegram-only.
        event["pending"] = [name for name in event.get("pending", ["telegram"]) if name in active]
    state["outbox"] = [event for event in state["outbox"] if event["pending"]]
    return state


def deliver(state, backend, name, sender):
    """Send this destination's events in order; stop at its first failure and return the error."""
    for event in list(state["outbox"]):
        if name not in event.get("pending", ["telegram"]):
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


def drain(state, backend, senders):
    """Returns {destination: error} for destinations that could not finish."""
    failures = {}
    for name, sender in senders.items():
        if error := deliver(state, backend, name, sender):
            failures[name] = error
    return failures
