# Example notifications

These are illustrative messages with invented version numbers, features, and timestamps. They are not reports of actual releases.

Every notification starts with the store icon (🍎 App Store, 🤖 Google Play), the game name and the event in bold, then the version, build, track and rollout. Change notes follow when the event has them, and the **Observed** time is shown in `notification_timezone` (UTC unless you change it).

## Telegram (every event by default)

Sent with `parse_mode: HTML`. Store-supplied text is escaped, and release notes appear in an expandable quote.

### Several steps in one message

Changes to the same release or build that arrive together, or within `batch_window_seconds` of an in-between state, are sent as one message. The latest step leads, the values below it are the ones that apply now, and earlier steps move to a quieter *Earlier* line:

> 🤖 **Sample Runner** — **Rollout at 20%**\
> Version **1.0.21** · Build **210**\
> Google Play · Track **production** · Rollout **20%**\
> *Earlier: Moved to production*
>
> **Google Play release notes (en-US)**
> > New maps
>
> *Observed 2026-09-08 17:30 UTC*

> 🍎 **Sample Miner** — **Build completed processing**\
> Version **1.0.2** · Build **42**\
> App Store · **iOS**\
> *Earlier: Build uploaded — processing*
>
> **TestFlight What to Test (en-US)**
> > Added fishing boats
>
> *Observed 2026-09-08 17:35 UTC*

### Version entered review

> 🍎 **Sample Miner** — **Waiting for review**\
> Version **1.0.2** · Build **42**\
> App Store · **iOS**
>
> **App Store release notes (en-US)**
> > Added fishing boats\
> > Added cloud saves
>
> *Observed 2026-09-08 17:30 UTC*

<details>
<summary>The HTML Telegram receives</summary>

```html
🍎 <b>Sample Miner</b> — <b>Waiting for review</b>
Version <b>1.0.2</b> · Build <b>42</b>
App Store · <b>iOS</b>

<b>App Store release notes (en-US)</b>
<blockquote expandable>Added fishing boats
Added cloud saves</blockquote>
<i>Observed 2026-09-08 17:30 UTC</i>
```

</details>

### Release moved to production

> 🤖 **Sample Runner** — **Moved to production**\
> Version **1.0.21** · Build **210**\
> Google Play · Track **production** · Rollout **20%**
>
> **Google Play release notes (en-US)**
> > New maps
>
> *Observed 2026-09-08 17:30 UTC*

### Google Play rollout increased

> 🤖 **Sample Miner** — **Rollout increased to 50%**\
> Version **1.0.2** · Build **42**\
> Google Play · Track **production** · Rollout **50%**\
> *Observed 2026-09-08 17:30 UTC*

### Rollout configured while publication confirmation is delayed

> 🤖 **Sample Runner** — **Rollout set to 100% — publication confirmation pending**\
> Version **1.0.15** · Build **210**\
> Google Play · Track **production** · Rollout **100%**\
> *Change notes: not provided*\
> *Observed 2026-09-08 18:12 UTC*

### Apple build processing completed

> 🍎 **Sample Miner** — **Build completed processing**\
> Version **1.0.2** · Build **42**\
> App Store · **iOS**
>
> **TestFlight What to Test (en-US)**
> > Added fishing boats\
> > Added cloud saves
>
> *Observed 2026-09-08 17:30 UTC*

### Android build detected

> 🤖 **Sample Miner** — **New Android build detected**\
> Version **1.0.2** · Build **42**\
> Google Play · **Android**
>
> **Google Play release notes (en-US)**
> > Added fishing boats
>
> *Observed 2026-09-08 17:30 UTC*

### Monitor health

> ⚠️ **Monitor needs attention**\
> delivery/discord\
> Discord: HTTP 404. Checks have failed 3 times; previous release state is preserved.

Messages queued before this format existed are sent once as the plain text they were queued with.

## Discord (shipped filter: review and release milestones)

One embed per event, with a blue bar for the App Store and a green bar for Google Play. Mentions are disabled and store-supplied Markdown is escaped. With the filter that ships in `config.json`, Discord receives only:

| Store | Events |
| --- | --- |
| App Store | Waiting for review, In review, Moved to production (live) |
| Google Play, production track | Moved to production (confirmed by the release-status API), rollout percentage changes, rollout paused, resumed, and completed |

Games listed in `config.json` → `discord.exclude_apps` never appear. Change `discord.events` to send more; see the README.

A combined message shows only the steps Discord's filter allows, so the first example above appears there as a single-step message, **Sample Runner — Moved to production**.

### Version entered review

> 🍎 App Store\
> **Sample Miner — Waiting for review**\
> **Version** 1.0.2 · **Build** 42 · **Platform** iOS\
> **App Store release notes (en-US)**\
> Added fishing boats\
> Added cloud saves\
> <sub>Observed 2026-09-08 17:30 UTC</sub>

### Google Play rollout increased

> 🤖 Google Play\
> **Sample Miner — Rollout increased to 50%**\
> **Version** 1.0.2 · **Build** 42 · **Track** production · **Rollout** 50%\
> <sub>Observed 2026-09-08 17:30 UTC</sub>

<details>
<summary>The JSON Discord receives</summary>

```json
{
  "embeds": [{
    "author": {"name": "🤖 Google Play"},
    "color": 3450963,
    "title": "Sample Miner — Rollout increased to 50%",
    "fields": [
      {"name": "Version", "value": "1.0.2", "inline": true},
      {"name": "Build", "value": "42", "inline": true},
      {"name": "Track", "value": "production", "inline": true},
      {"name": "Rollout", "value": "50%", "inline": true}
    ],
    "footer": {"text": "Observed 2026-09-08 17:30 UTC"}
  }],
  "allowed_mentions": {"parse": []}
}
```

</details>
