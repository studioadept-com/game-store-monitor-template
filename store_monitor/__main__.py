import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import discord, telegram
from .delivery import LEGACY_DISCORD, discord_channels, drain, enabled, prepare, routes
from .events import reconcile
from .providers import AppleProvider, GoogleProvider, safe_error
from .state import GCSState, GitHubState, LocalState
from .telegram import Telegram


def legacy_aliases(config):
    """Discord events queued before channels existed go to the channel that uses their webhook."""
    for channel in discord_channels(config):
        if channel["webhook_secret"] == "DISCORD_WEBHOOK_URL":
            return {LEGACY_DISCORD: channel["destination"]}
    return {}


def senders(config, display_timezone, errors):
    """Senders for the enabled destinations whose credentials are present."""
    result = {}
    if "telegram" in enabled(config):
        if telegram.configured():
            result["telegram"] = Telegram(display_timezone)
        else:
            print("Telegram is enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not set; Telegram notifications stay queued.")
    for channel in discord_channels(config):
        name = channel["destination"]
        try:
            url = discord.webhook_url(channel["webhook_secret"])
        except RuntimeError as error:
            errors["delivery/" + name] = str(error)
            continue
        if url:
            result[name] = discord.Discord(url, display_timezone, destination=name)
        else:
            print(f"Discord channel {channel['id']} has no {channel['webhook_secret']}; its notifications stay queued.")
    return result


def test_discord(config, only=None):
    """Post a connection test to every listed channel whose webhook is set, enabled or not."""
    sent = 0
    for channel in discord_channels(config, include_disabled=True):
        if only and channel["id"] != only:
            continue
        url = discord.webhook_url(channel["webhook_secret"])
        if not url:
            print(f"Skipped {channel['id']}: {channel['webhook_secret']} is not set.")
            continue
        discord.Discord(url).post(discord.test_payload(channel))
        print(f"Discord test delivered to {channel['id']}.")
        sent += 1
    if not sent:
        raise RuntimeError("No Discord webhook is set for the channels in config.json")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--local-state", help="Use this state file instead of GitHub")
    parser.add_argument("--dry-run", action="store_true", help="Read stores; do not save state or send messages")
    parser.add_argument("--test-telegram", action="store_true")
    parser.add_argument("--test-discord", action="store_true", help="Send a connection test to the Discord channels only")
    parser.add_argument("--channel", help="With --test-discord: test only this channel id")
    args = parser.parse_args(argv)
    if args.test_telegram:
        Telegram().post("Game store monitor: Telegram connection verified. Store monitoring will start after account setup is complete.")
        print("Telegram test delivered.")
        return 0
    config = json.load(open(args.config))
    if args.test_discord:
        test_discord(config, args.channel)
        return 0
    display_timezone = config.get("notification_timezone", "UTC")
    if args.local_state:
        backend = LocalState(args.local_state)
    elif os.getenv("STATE_BUCKET"):
        backend = GCSState(os.environ["STATE_BUCKET"])
    else:
        backend = GitHubState()
    # A destination switched off in config.json drops what it still had queued.
    state = prepare(backend.load(), enabled(config), legacy_aliases(config))
    delivery_errors = {}
    outputs = {} if args.dry_run else senders(config, display_timezone, delivery_errors)
    # Finish previous deliveries before collecting; each destination fails on its own.
    for name, error in drain(state, backend, outputs).items():
        delivery_errors["delivery/" + name] = error
    stores = [(store, factory) for store, factory in (("apple", AppleProvider), ("google", GoogleProvider))
              if config.get(store, {}).get("enabled", True)]
    if not stores:
        raise RuntimeError("Both store connections are disabled")

    def collect(store, factory):
        # Each store gets its own result containers so the threads never share a dict.
        snapshots, errors, successes = {}, {}, set()
        started = time.monotonic()
        try:
            factory().collect(config.get(store, {}), state["scopes"], snapshots, errors, successes)
        except Exception as error:
            errors[store + "/discovery"] = safe_error(error)
        print(f"{store}: {len(snapshots)} scopes in {time.monotonic() - started:.0f}s")
        return snapshots, errors, successes

    snapshots, errors, successes = {}, dict(delivery_errors), set()
    # Delivery health feeds the usual three-strikes alert, so a broken webhook is reported on Telegram.
    successes |= {"delivery/" + name for name in outputs if "delivery/" + name not in errors}
    # The stores are independent and I/O bound; checking them side by side halves the run time.
    with ThreadPoolExecutor(len(stores)) as pool:
        for store_snapshots, store_errors, store_successes in pool.map(lambda item: collect(*item), stores):
            snapshots.update(store_snapshots)
            errors.update(store_errors)
            successes |= store_successes
    new_state = reconcile(state, snapshots, errors, successes, config.get("failure_threshold", 3),
                          display_timezone=display_timezone, hold_seconds=config.get("batch_window_seconds", 600),
                          destinations=routes(config))
    summary = f"Checked {len(snapshots)} app/track scopes; {len(errors)} errors; {len(new_state['outbox'])} queued notifications."
    print(summary)
    for scope, error in errors.items():
        print(f"Check failed: {scope}: {error}")
    if args.dry_run:
        for event in new_state["outbox"]:
            hold = f" (held until {event['hold_until']})" if "hold_until" in event else ""
            print(f"[{', '.join(event['pending'])}]{hold} {event['text']}")
    else:
        if new_state != state:
            backend.save(new_state)  # Persist new observations AND events before sending.
        for name, error in drain(new_state, backend, outputs).items():
            errors["delivery/" + name] = error
            print(f"Delivery failed: {name}: {error}")
    if path := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as output:
            output.write(summary + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("Monitor failed: " + safe_error(error), file=sys.stderr)
        sys.exit(1)
