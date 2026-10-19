# Game store monitor

A small bot that watches your App Store Connect and Google Play releases and posts changes to Telegram, Discord, or both:

> 🤖 **Sample Miner** — **Rollout increased to 50%**\
> Version **1.0.2** · Build **42**\
> Google Play · Track **production** · Rollout **50%**\
> *Observed 2026-09-08 17:30 UTC*

It reports builds, drafts, review and approval decisions, publication, rollout changes, and release-note edits. Each message includes the version, build, track, change notes and the time it was observed. See [EXAMPLES.md](EXAMPLES.md).

**Setting it up?** Follow [AGENTS.md](AGENTS.md). An AI agent can run it end to end, and it marks the few steps that need a person.

## How it runs

A small **Cloud Run service** (a quarter vCPU, billed only while a check runs) checks both stores each time **Cloud Scheduler** calls it, every five minutes. That fits in Cloud Run's free allowance; see the cost table in [AGENTS.md](AGENTS.md#cost). State (known releases, history, and queued notifications) is one `state.json` in a private Cloud Storage bucket. Pushing to `main` runs `deploy.yml`, which tests, builds and redeploys the service. In a copy that isn't set up yet (and in this template), the deploy is skipped until the repository variables exist. GitHub signs in to Google Cloud keylessly, and Google Play access uses the service's own service account, so the only stored keys are the App Store key and the Telegram and Discord credentials, all in Secret Manager.

## Configuration (`config.json`)

| Setting | Meaning |
| --- | --- |
| `apple.enabled`, `google.enabled` | Which stores to check |
| `telegram.enabled`, `discord.enabled` | Where to send notifications; at least one must be on |
| `discord.channels` | One entry per Discord channel, each with its own webhook, games and events (below). Different games can go to different channels, for example one per studio |
| `<channel>.id`, `.name`, `.enabled` | `id` (lowercase letters, digits, dashes) names the channel in the queue and in health alerts (`delivery/discord/<id>`). `enabled: false` pauses one channel |
| `<channel>.webhook_secret` | The secret holding that channel's webhook URL: `DISCORD_WEBHOOK_URL` or `DISCORD_WEBHOOK_URL_<NAME>`. URLs never go into `config.json` |
| `<channel>.games` | `"all"` (new games appear automatically), or only these games: a list of IDs, or an object of ID → name |
| `telegram.events`, `<channel>.events` | Which event kinds to send, per `apple`, `google` and `monitor` (health alerts). `"*"` sends every kind. Leave `events` out to send everything. |
| `telegram.google_tracks`, `<channel>.google_tracks` | Limit Google release events to these tracks, for example `["production"]` |
| `telegram.exclude_apps`, `<channel>.exclude_apps` | Games to leave out, by iOS bundle ID, Android package name, or App Store numeric ID. A list, or an object of ID → name. |
| `notification_timezone` | Time zone for the *Observed* time, for example `Europe/London` |
| `batch_window_seconds` | How long a change into an in-between state (such as a build still processing) waits for its next step, so both go out as one message. Default 600; `0` sends every change at once |
| `google.apps`, `google.extra_tracks` | Packages or tracks that discovery misses |
| `google.lifecycle_*` | Budget for Google's separately rate-limited release-status endpoint; the defaults are safe |

As shipped, both destinations are off. Telegram gets everything once enabled. The one Discord channel, `main`, is set to review and release milestones, which suits a shared channel. An older `discord` section without `channels` still works as a single channel on `DISCORD_WEBHOOK_URL`.

### Settings page

Instead of editing `config.json` by hand, run a local page that lists your games from the monitor's state:

```sh
scripts/settings.sh
```

It creates `.venv` and installs the requirements on first use, then opens on `127.0.0.1` (port 8740, or the next free one). It reads `GCP_PROJECT`, `GITHUB_REPO` and `STATE_BUCKET` from `setup.env`; it needs a signed-in `gcloud` and `gh`. Add, pause or remove Discord channels; tick each one's games, events and Google tracks; paste a channel's webhook URL to store it in Secret Manager and GitHub; and send a test message. The page also shows the check schedule with a monthly cost estimate per interval (from the measured check time), and can change, pause or resume the Scheduler job directly; that needs `GCP_REGION` and no deploy. **Save** writes only `config.json`: commit and push it to deploy. The deploy maps every `webhook_secret` named in `config.json` that exists, and manual workflow runs load every `DISCORD_WEBHOOK_URL*` repository secret. Before the first check there is no state yet, so the page lists only games already named in `config.json`.

### Event kinds

| Store | Kinds |
| --- | --- |
| both | `draft`, `ready_for_review`, `waiting_for_review`, `in_review`, `approved`, `rejected`, `review_withdrawn`, `published`, `notes_updated`, `release_renamed`, `release_build_changed`, `status_changed` |
| both | `rollout_started` (first percentage, sent with the publication), `rollout_changed`, `rollout_paused`, `rollout_resumed`, `rollout_completed` |
| Google | `not_sent_for_review`, `publication_unconfirmed`, `rollout_pending_confirmation` (track configured while Google's status endpoint hasn't confirmed it) |
| builds | `build_processing`, `build_processed`, `build_failed`, `build_invalid`, `build_detected` (Android), `build_status`, `build_notes_updated` |
| `monitor` | `monitor_connected`, `monitor_attention`, `monitor_recovered` |

## Behaviour worth knowing

- **Batching.** All changes to one release or build found in the same check are sent as one message. The latest step is the headline (for example **Rollout at 20%**), with the current version, build, track and rollout below it, and earlier steps on a quieter *Earlier: Moved to production* line. A change into an in-between state waits up to `batch_window_seconds` (default 600, about two checks) for the next step. These states are an App Store build still processing, ready for review, accepted, awaiting Apple's release or processing for distribution, and on Google Play, approved but not yet published, or a track configured but not yet confirmed. So a build's *processing* and *completed* steps arrive as one message. A settled state goes out at once together with anything held for that release; if no next step comes, the held message goes out when the window ends. `0` turns holding off. Held messages wait in the outbox in `state.json`, so a restart neither loses nor repeats them. Each Discord channel shows only the steps its filter allows.
- **Quiet first run:** existing releases are recorded without being announced, followed by one **Monitoring connected** message per store.
- **At-least-once delivery:** each destination acknowledges every message separately, and state is saved after each send. A failing destination keeps its queue without blocking or repeating the other one. A crash between a send and the save can repeat that one message.
- **Failures:** a store, track or destination that fails three runs in a row raises one **Monitor needs attention** alert, and another when it recovers. A check with errors answers HTTP 500, so it shows as failed in Cloud Scheduler and the service logs.
- **Google Play honesty:** a release is only called live after Google's release-status endpoint confirms it. That endpoint has its own small quota, so the monitor spends a budget of six requests per run on tracks still awaiting a decision, and backs off for an hour if Google refuses.
- **Apple phased releases** follow Apple's 1, 2, 5, 10, 20, 50, 100% schedule. These percentages are eligible automatic updates, not installs.
- **Limits:** about 35 App Store requests and 95 Google requests per run, far below both stores' limits. Telegram and Discord get at most one message per second.

## Local development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

With store credentials in the environment, `.venv/bin/python -m store_monitor --local-state state.json --dry-run` prints what would be sent to each destination and saves nothing.

## References

[Play release lifecycle](https://developers.google.com/android-publisher/api-ref/rest/v3/applications.tracks.releases) · [Play edits](https://developers.google.com/android-publisher/edits) · [App Store Connect API](https://developer.apple.com/help/app-store-connect/get-started/app-store-connect-api) · [Telegram Bot API](https://core.telegram.org/bots/api) · [Discord webhooks](https://discord.com/developers/docs/resources/webhook#execute-webhook) · [Cloud Run services on a schedule](https://cloud.google.com/run/docs/triggering/using-scheduler) · [Keyless GitHub sign-in](https://github.com/google-github-actions/auth)
