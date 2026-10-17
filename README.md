# Game store monitor

A small bot that watches your App Store Connect and Google Play releases and posts changes to Telegram, Discord, or both:

> 🤖 **Sample Miner** — **Rollout increased to 50%**\
> Version **1.0.2** · Build **42**\
> Google Play · Track **production** · Rollout **50%**\
> *Observed 2026-09-08 17:30 UTC*

It reports builds, drafts, review and approval decisions, publication, rollout changes, and release-note edits. Each message includes the version, build, track, change notes and the time it was observed. See [EXAMPLES.md](EXAMPLES.md).

**Setting it up?** Follow [AGENTS.md](AGENTS.md). An AI agent can run it end to end, and it marks the few steps that need a person.

## How it runs

A **Cloud Run job** checks both stores once and exits; **Cloud Scheduler** starts it every five minutes. State (known releases, history, and queued notifications) is one `state.json` in a private Cloud Storage bucket. Pushing to `main` runs `deploy.yml`, which tests, builds and redeploys the job. GitHub signs in to Google Cloud keylessly, and Google Play access uses the job's own service account, so the only stored keys are the App Store key and the Telegram and Discord credentials, all in Secret Manager.

## Configuration (`config.json`)

| Setting | Meaning |
| --- | --- |
| `apple.enabled`, `google.enabled` | Which stores to check |
| `telegram.enabled`, `discord.enabled` | Where to send notifications; at least one must be on |
| `<destination>.events` | Which event kinds to send, per `apple`, `google` and `monitor` (health alerts). `"*"` sends every kind. Leave `events` out to send everything. |
| `<destination>.google_tracks` | Limit Google release events to these tracks, for example `["production"]` |
| `<destination>.exclude_apps` | Games to leave out, by iOS bundle ID, Android package name, or App Store numeric ID. A list, or an object of ID → name. |
| `notification_timezone` | Time zone for the *Observed* time, for example `Europe/London` |
| `google.apps`, `google.extra_tracks` | Packages or tracks that discovery misses |
| `google.lifecycle_*` | Budget for Google's separately rate-limited release-status endpoint; the defaults are safe |

As shipped, both destinations are off. Telegram gets everything once enabled. Discord's filter is set to review and release milestones, which suits a shared channel.

### Event kinds

| Store | Kinds |
| --- | --- |
| both | `draft`, `ready_for_review`, `waiting_for_review`, `in_review`, `approved`, `rejected`, `review_withdrawn`, `published`, `notes_updated`, `release_renamed`, `release_build_changed`, `status_changed` |
| both | `rollout_started` (first percentage, sent with the publication), `rollout_changed`, `rollout_paused`, `rollout_resumed`, `rollout_completed` |
| Google | `not_sent_for_review`, `publication_unconfirmed`, `rollout_pending_confirmation` (track configured while Google's status endpoint hasn't confirmed it) |
| builds | `build_processing`, `build_processed`, `build_failed`, `build_invalid`, `build_detected` (Android), `build_status`, `build_notes_updated` |
| `monitor` | `monitor_connected`, `monitor_attention`, `monitor_recovered` |

## Behaviour worth knowing

- **Quiet first run:** existing releases are recorded without being announced, followed by one **Monitoring connected** message per store.
- **At-least-once delivery:** each destination acknowledges every message separately, and state is saved after each send. A failing destination keeps its queue without blocking or repeating the other one. A crash between a send and the save can repeat that one message.
- **Failures:** a store, track or destination that fails three runs in a row raises one **Monitor needs attention** alert, and another when it recovers. Executions with errors show as failed in Cloud Run.
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

[Play release lifecycle](https://developers.google.com/android-publisher/api-ref/rest/v3/applications.tracks.releases) · [Play edits](https://developers.google.com/android-publisher/edits) · [App Store Connect API](https://developer.apple.com/help/app-store-connect/get-started/app-store-connect-api) · [Telegram Bot API](https://core.telegram.org/bots/api) · [Discord webhooks](https://discord.com/developers/docs/resources/webhook#execute-webhook) · [Cloud Run jobs on a schedule](https://cloud.google.com/run/docs/execute/jobs-on-schedule) · [Keyless GitHub sign-in](https://github.com/google-github-actions/auth)
