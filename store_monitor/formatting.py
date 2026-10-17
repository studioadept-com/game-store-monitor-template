"""Render queued events for Telegram (HTML) and Discord (embeds).

Store-supplied text (app names, versions, release notes) is always escaped, and
messages are sized to each destination's limits. Events queued before structured
fields existed have only `text`; Telegram sends that as plain text.
"""
import html
import re

from .events import local_time, percent

STORES = {"apple": ("🍎", "App Store", 0x0D96F6), "google": ("🤖", "Google Play", 0x34A853)}
PLATFORMS = {"IOS": "iOS", "MAC_OS": "macOS", "TV_OS": "tvOS", "VISION_OS": "visionOS"}
MONITOR_ICONS = {"monitor_connected": "🔗", "monitor_recovered": "✅", "monitor_attention": "⚠️"}
NOTES_PREVIEW = 1500  # Full notes stay in state history; keep messages compact.
TELEGRAM_LIMIT = 4096
DISCORD_FIELD_LIMIT = 1024
DISCORD_TITLE_LIMIT = 256

# Inline markers anywhere, block markers (heading, quote, list) only at a line start.
DISCORD_INLINE = re.compile(r"([\\*_~`|<\[\]])")
DISCORD_BLOCK = re.compile(r"^(\s*)([#>+-])", re.M)


def length(text):
    """Telegram and Discord count UTF-16 code units, so an emoji can count twice."""
    return len(text.encode("utf-16-le")) // 2


def preview(text, size):
    return text if len(text) <= size else text[:max(0, size - 1)].rstrip() + "…"


def fit(build, text, limit):
    """Shorten text before escaping until build(text) fits, so no entity or escape is cut in half."""
    size = NOTES_PREVIEW
    while True:
        rendered = build(preview(text, size))
        if length(rendered) <= limit or size <= 1:
            return rendered if length(rendered) <= limit else None
        # Escaping can expand characters unevenly; shrink in proportion and retry.
        size = max(1, min(size - 1, size * limit // length(rendered)))


def discord_escape(text):
    return DISCORD_BLOCK.sub(r"\1\\\2", DISCORD_INLINE.sub(r"\\\1", text))


def track_text(event):
    track = event.get("track")
    return PLATFORMS.get(track, track) if track else None


def track_label(event):
    # Builds carry a pseudo-track (platform), so only Google release events name a track.
    return "Track" if event["store"] == "google" and not event["kind"].startswith("build_") else "Platform"


def steps_title(event, destination):
    """The steps this destination may see, joined; events queued before batching have one step."""
    steps = event.get("steps") or [{"kind": event["kind"], "title": event["title"]}]
    visible = event.get("visible", {}).get(destination)
    return " → ".join(step["title"] for step in steps if visible is None or step["kind"] in visible)


def notes_heading(event):
    locale = f" ({event['notes_locale']})" if event.get("notes_locale") else ""
    return event.get("notes_source", "Release notes") + locale


def telegram_html(event, display_timezone):
    """HTML message, or None when only the plain-text fallback can be sent."""
    if "kind" not in event:
        return None
    escape = html.escape
    if not event.get("store"):
        icon = MONITOR_ICONS.get(event["kind"], "ℹ️")
        message = f"{icon} <b>{escape(event['title'])}</b>\n{escape(event.get('detail', ''))}"
        return message if length(message) <= TELEGRAM_LIMIT else None
    icon, store, _ = STORES[event["store"]]
    lines = [f"{icon} <b>{escape(event['app_name'])}</b> — <b>{escape(steps_title(event, 'telegram'))}</b>"]
    facts = [f"Version <b>{escape(event['version'])}</b>"] if event.get("version") else []
    if event.get("build"):
        facts.append(f"Build <b>{escape(event['build'])}</b>")
    if facts:
        lines.append(" · ".join(facts))
    place = [store]
    if track_text(event):
        label = "Track " if track_label(event) == "Track" else ""
        place.append(f"{label}<b>{escape(track_text(event))}</b>")
    if event.get("rollout") is not None:
        place.append(f"Rollout <b>{percent(event['rollout'])}</b>")
    lines.append(" · ".join(place))
    footer = f"<i>Observed {escape(local_time(event['observed_at'], display_timezone))}</i>" if event.get("observed_at") else ""

    def build(notes):
        body = list(lines)
        if "notes" in event:
            if notes:
                body += ["", f"<b>{escape(notes_heading(event))}</b>",
                         f"<blockquote expandable>{escape(notes)}</blockquote>"]
            else:
                body.append("<i>Change notes: not provided</i>")
        return "\n".join(body + ([footer] if footer else []))

    return fit(build, event.get("notes", ""), TELEGRAM_LIMIT)


def discord_payload(event, display_timezone):
    """One embed coloured by store; mentions never ping anyone."""
    icon, store, colour = STORES[event["store"]]
    title = preview(f"{event['app_name']} — {steps_title(event, 'discord')}", DISCORD_TITLE_LIMIT)
    embed = {"author": {"name": f"{icon} {store}"}, "color": colour,
             "title": fit(discord_escape, title, DISCORD_TITLE_LIMIT) or steps_title(event, "discord")}
    fields = []
    for name, value in (("Version", event.get("version")), ("Build", event.get("build")),
                        (track_label(event), track_text(event)),
                        ("Rollout", percent(event["rollout"]) if event.get("rollout") is not None else None)):
        if value:
            fields.append({"name": name, "value": fit(discord_escape, str(value), DISCORD_FIELD_LIMIT),
                           "inline": True})
    if "notes" in event:
        value = fit(discord_escape, event["notes"], DISCORD_FIELD_LIMIT) if event["notes"] else "*Not provided*"
        fields.append({"name": preview(notes_heading(event), DISCORD_TITLE_LIMIT), "value": value, "inline": False})
    if fields:
        embed["fields"] = fields
    if event.get("observed_at"):
        embed["footer"] = {"text": "Observed " + local_time(event["observed_at"], display_timezone)}
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}
