# Setup runbook for AI agents

Follow these steps in order to set up the store monitor for a new owner. Steps marked **HUMAN** need the owner: they involve billing, store consoles or chat apps that an agent can't, or shouldn't, operate. Everything else you run yourself, mostly through `scripts/setup-gcp.sh`.

## Rules

- **Never print, log, commit or paste a secret value**: API keys, `.p8` contents, bot tokens or webhook URLs. Pass them with `scripts/setup-gcp.sh secret NAME [FILE]`, which reads hidden input or a file. `setup.env`, `*.p8` and `state.json` are git-ignored; keep it that way.
- Use the dedicated `store-monitor@…` service account only for monitoring. Every check opens and discards a temporary Play edit, and that would cancel an open edit belonging to an uploader using the same account.
- Grant the store accounts read-only access only (see below). Never grant publishing, financial or admin permissions.
- Don't make the repository public: its state history holds unreleased version information.

## 0. Ask the owner

Collect these answers before you start:

| Question | Used for |
| --- | --- |
| Google Cloud project ID (existing, or one they'll create) | `GCP_PROJECT` |
| Region, for example `us-central1` or `europe-west4` | `GCP_REGION` |
| Their GitHub repository, `owner/name` | `GITHUB_REPO` |
| Which stores: App Store, Google Play, or both | `apple.enabled`, `google.enabled` |
| Where to send notifications: Telegram, Discord, or both | `telegram.enabled`, `discord.enabled` |
| What each destination should get: every event, or only review and release milestones | `events` (see README) |
| Games to leave out, as bundle IDs or package names | `exclude_apps` |
| Time zone for timestamps, for example `Europe/London` | `notification_timezone` |

## 1. HUMAN: repository and billing

1. On GitHub, open this template and choose **Use this template → Create a new repository**. Make it **private**.
2. In Google Cloud, create or pick a project and link a billing account: **Billing → Link a billing account**. The monitor costs about $7 a month (see Cost).
3. Install and sign in to the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) (`gcloud auth login`) and the [GitHub CLI](https://cli.github.com) (`gh auth login`), as an account that owns the project and administers the repository.

## 2. Google Cloud and GitHub wiring

Clone the owner's repository, then create `setup.env` in its root:

```sh
GCP_PROJECT=YOUR_GCP_PROJECT
GCP_REGION=YOUR_REGION
GITHUB_REPO=YOUR_GITHUB_OWNER/YOUR_REPO
# Optional: STATE_BUCKET=YOUR_STATE_BUCKET (default: YOUR_GCP_PROJECT-store-monitor)
```

```sh
scripts/setup-gcp.sh check   # tools, sign-ins, billing
scripts/setup-gcp.sh infra   # APIs, bucket, service accounts, image registry, IAM, keyless sign-in, repo variables
```

`infra` restricts GitHub's keyless sign-in to this repository ID, its owner ID and `main`. Only `store-monitor.yml` can act as the monitor account, and only `deploy.yml` as the deployer. Note the monitor account it prints: `store-monitor@YOUR_GCP_PROJECT.iam.gserviceaccount.com`.

## 3. HUMAN: store access

**Google Play** (skip if not monitored). In [Play Console](https://play.google.com/console) → **Users and permissions → Invite new users**, invite the monitor account. Grant **View app information and download bulk reports (read-only)** for the whole account, with no expiry, and nothing else. Account-wide access covers future apps too.

**App Store** (skip if not monitored). In [App Store Connect](https://appstoreconnect.apple.com/access/integrations/api) → **Users and Access → Integrations → Team Keys**, generate a key named `Store monitor` with the **Developer** role. Download the `.p8` file; Apple allows only one download. Give the agent the **Issuer ID**, the **Key ID** and the path to the `.p8`, never its contents. The monitor makes only read (GET) requests.

Then store them:

```sh
scripts/setup-gcp.sh secret ASC_ISSUER_ID
scripts/setup-gcp.sh secret ASC_KEY_ID
scripts/setup-gcp.sh secret ASC_PRIVATE_KEY /path/to/AuthKey_KEYID.p8
```

## 4. HUMAN: notification destinations

Set up at least one.

**Discord.** **Channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.** Anyone with the URL can post to the channel. Then:

```sh
scripts/setup-gcp.sh secret DISCORD_WEBHOOK_URL
```

**Telegram.** Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy its token. Add the bot to the chat or group and send the chat a message. Then store the token and look up the chat ID without printing the token:

```sh
scripts/setup-gcp.sh secret TELEGRAM_BOT_TOKEN
read -rsp "Bot token: " TOKEN; echo; curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | python3 -c 'import json,sys; print({u[k]["chat"]["id"]: u[k]["chat"].get("title") or u[k]["chat"].get("username") for u in json.load(sys.stdin)["result"] for k in ("message", "channel_post", "my_chat_member") if k in u})'; unset TOKEN
scripts/setup-gcp.sh secret TELEGRAM_CHAT_ID
```

## 5. Configure, deploy and test

1. Edit `config.json` to match step 0:
   - Set `enabled: true` on each chosen destination and `enabled: false` on any unused store.
   - Adjust `events`, `google_tracks` and `exclude_apps`, and set `notification_timezone`.
   - If Discord is the only destination, consider adding `"monitor": ["*"]` to its `events`, so health alerts reach someone.
2. Commit and push to `main`. `deploy.yml` tests, builds and deploys the Cloud Run service `store-monitor`, mapping only the secrets that exist. Watch it with `gh run watch --repo YOUR_GITHUB_OWNER/YOUR_REPO`. A secret added later needs another deploy: `gh workflow run deploy.yml --repo YOUR_GITHUB_OWNER/YOUR_REPO`.
3. Run `scripts/setup-gcp.sh commands`. It prints ready-to-run `curl` calls to the service, signed with your own Google identity (the service rejects unsigned calls). Send the connection tests first (`mode=test-discord`, `mode=test-telegram`). They send one message and change nothing.
4. Run the first real check (the `curl` without a mode), which records a quiet baseline, then check its logs for errors. A Play `403` usually means the Play Console invite is missing or hasn't propagated yet; it can take up to a day.
5. Start the five-minute schedule with `scripts/setup-gcp.sh schedule`. It also prints the estimated monthly cost.
6. Give the owner the output of `scripts/setup-gcp.sh commands`: run now, tests, dry run, logs, pause and resume.

## First run: the quiet baseline

The first successful check of each store records every current release **without** announcing it, then sends one **Monitoring connected** message per store. That message is a `monitor_*` event, so a destination filtered to milestones won't show it. From then on, only changes are reported. A game added later is reported from its first release that the monitor sees. Switching a destination on later never replays old events.

## Secrets and variables

| Name | Kind | Where | Needed when |
| --- | --- | --- | --- |
| `ASC_ISSUER_ID`, `ASC_KEY_ID`, `ASC_PRIVATE_KEY` | secret | Secret Manager + GitHub | App Store is monitored |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | secret | Secret Manager + GitHub | Telegram is enabled |
| `DISCORD_WEBHOOK_URL` | secret | Secret Manager + GitHub | Discord is enabled (`none` = skip for now) |
| `GCP_PROJECT`, `GCP_REGION` | variable | GitHub | always |
| `STATE_BUCKET` | variable | GitHub | always |
| `IMAGE` | variable | GitHub | always (`REGION-docker.pkg.dev/PROJECT/store-monitor/monitor`) |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | variable | GitHub | always |
| `GCP_SERVICE_ACCOUNT`, `GCP_DEPLOYER_SERVICE_ACCOUNT` | variable | GitHub | always |

`scripts/setup-gcp.sh` sets all of them. The service reads secrets from Secret Manager; the GitHub copies are only for manual workflow runs. Google Play needs no key: the service runs as the monitor service account.

## Cost

The monitor runs as a Cloud Run **service** with a quarter vCPU and 256 MiB. It is billed only while a check is running, about 70–90 seconds, most of it waiting on store APIs. In tier-1 regions such as `us-central1` or `europe-west4`, request-billed Cloud Run costs $0.000024 per vCPU-second and $0.0000025 per GiB-second, after a monthly free allowance per billing account of 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests:

| Schedule | Checks a month | Approximate cost |
| --- | --- | --- |
| every 10 minutes | 4,320 | $0 |
| every 5 minutes (default) | 8,640 | $0 to about $0.35 a month |
| every 2 minutes | 21,600 | about $7 a month |

`scripts/setup-gcp.sh schedule` prints the estimate for your `SCHEDULE` (for example `SCHEDULE='*/10 * * * *'`). If other projects on the same billing account already use the free allowance, five-minute checks cost about $5 a month. Secret Manager, Storage, Scheduler and the image registry (5 newest images kept) add a few cents. GitHub Actions minutes are used only for deploys and manual runs.
