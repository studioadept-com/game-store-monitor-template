"""Optional Discord webhook destination.

Which events reach Discord is set in config.json (see delivery.wants).
"""
import os
import re
import time

import requests

from .formatting import discord_payload
from .http import ApiError

WEBHOOK = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+")
MAX_RETRY_WAIT = 30  # Longer rate-limit waits leave the event queued for the next run.


def webhook_url():
    """The configured webhook, or None. `none` lets the secret exist before the URL arrives."""
    value = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if value.lower() in ("", "none"):
        return None
    if not WEBHOOK.fullmatch(value):
        # Never echo the value: it contains the webhook token.
        raise RuntimeError("DISCORD_WEBHOOK_URL is not a Discord webhook URL")
    return value


def retry_after(response):
    try:
        return float(response.json()["retry_after"])
    except (ValueError, KeyError, TypeError):
        header = response.headers.get("Retry-After", "")
        try:
            return float(header)
        except ValueError:
            return None


class Discord:
    interval = 1.0  # Webhooks allow about 30 messages a minute per channel.

    def __init__(self, url, display_timezone="UTC", session=None):
        self.url = url
        self.display_timezone = display_timezone
        self.session = session or requests.Session()

    def send(self, event):
        self.post(discord_payload(event, self.display_timezone))

    def post(self, payload):
        for attempt in range(3):
            try:
                # wait=true makes Discord confirm the message was stored before answering.
                response = self.session.request("POST", self.url, params={"wait": "true"}, json=payload,
                                                timeout=(10, 40), allow_redirects=False)
            except requests.RequestException:
                # The exception text includes the URL, and the URL is the secret.
                raise ApiError("Discord") from None
            if response.status_code == 429:
                wait = retry_after(response)
                if wait is None or wait > MAX_RETRY_WAIT or attempt == 2:
                    raise ApiError("Discord", 429, "rate limited")
                time.sleep(wait)  # A 429 means nothing was posted, so retrying cannot duplicate.
                continue
            if not 200 <= response.status_code < 300:
                raise ApiError("Discord", response.status_code)
            if response.headers.get("X-RateLimit-Remaining") == "0":
                try:
                    time.sleep(min(float(response.headers.get("X-RateLimit-Reset-After", 0)), MAX_RETRY_WAIT))
                except ValueError:
                    pass
            return


def test_payload():
    return {"allowed_mentions": {"parse": []}, "embeds": [{
        "title": "Release notifications connected", "color": 0x5865F2,
        "description": "This channel will receive the store events selected in config.json.",
    }]}
