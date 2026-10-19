#!/usr/bin/env bash
# Sets up Google Cloud and GitHub for the store monitor. Every step skips or
# updates what already exists, so rerunning is safe. AGENTS.md gives the order.
#
# Settings come from the environment, or from ./setup.env (KEY=value lines; git ignores it):
#   GCP_PROJECT   Google Cloud project ID, with billing enabled          (required)
#   GCP_REGION    Region for Cloud Run, Scheduler, Storage and images    (required)
#   GITHUB_REPO   owner/name of your copy of this repository             (required)
#   STATE_BUCKET  Bucket for state.json      (default: GCP_PROJECT-store-monitor)
#   SCHEDULE      Cron schedule in UTC       (default: */5 * * * *; 'schedule' prints its cost)
#
# Commands:
#   check                 Show tool, sign-in and billing status
#   infra                 APIs, service accounts, bucket, image registry, IAM, keyless GitHub sign-in
#   variables             GitHub repository variables the workflows read
#   secret NAME [FILE]    Store a secret in Secret Manager and GitHub; prompts unless FILE is given
#                         (or use the settings page: python3 -m store_monitor.settings)
#   schedule              After the first deploy: call the service on SCHEDULE, and estimate the cost
#   commands              Print run, test, dry-run, log and pause/resume commands
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f setup.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./setup.env
  set +a
fi

SECRETS="TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID DISCORD_WEBHOOK_URL ASC_ISSUER_ID ASC_KEY_ID ASC_PRIVATE_KEY"
SERVICE=store-monitor
SCHEDULER_JOB=store-monitor-every-5-min
POOL=game-store-monitor
PROVIDER=github
AR_REPO=store-monitor

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
quiet() { "$@" >/dev/null 2>&1; }

settings() {
  : "${GCP_PROJECT:?Set GCP_PROJECT (see the top of this script)}"
  : "${GCP_REGION:?Set GCP_REGION (see the top of this script)}"
  : "${GITHUB_REPO:?Set GITHUB_REPO to owner/name (see the top of this script)}"
  PROJECT=$GCP_PROJECT
  REGION=$GCP_REGION
  REPO=$GITHUB_REPO
  BUCKET=${STATE_BUCKET:-$PROJECT-store-monitor}
  SCHEDULE=${SCHEDULE:-*/5 * * * *}
  IMAGE=$REGION-docker.pkg.dev/$PROJECT/$AR_REPO/monitor
  MONITOR_SA=store-monitor@$PROJECT.iam.gserviceaccount.com
  DEPLOYER_SA=store-monitor-deployer@$PROJECT.iam.gserviceaccount.com
  SCHEDULER_SA=store-monitor-scheduler@$PROJECT.iam.gserviceaccount.com
}

project_number() { gcloud projects describe "$PROJECT" --format='value(projectNumber)'; }

cmd_check() {
  settings
  command -v gcloud >/dev/null || die "Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install"
  command -v gh >/dev/null || die "Install the GitHub CLI: https://cli.github.com"
  printf 'gcloud account: %s\n' "$(gcloud config get-value account 2>/dev/null)"
  gh auth status >/dev/null 2>&1 || die "Sign in to GitHub: gh auth login"
  gh repo view "$REPO" --json name >/dev/null || die "Cannot see GitHub repository $REPO"
  gcloud projects describe "$PROJECT" --format='value(projectId)' >/dev/null || die "Cannot see project $PROJECT"
  billing=$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null || true)
  if [ "$billing" = True ]; then
    echo "Billing: enabled"
  else
    echo "Billing: not confirmed. A human must link a billing account: https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT"
  fi
}

cmd_infra() {
  settings
  log "Enabling APIs"
  gcloud services enable --project "$PROJECT" \
    run.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com \
    artifactregistry.googleapis.com storage.googleapis.com iam.googleapis.com \
    iamcredentials.googleapis.com sts.googleapis.com cloudresourcemanager.googleapis.com \
    androidpublisher.googleapis.com playdeveloperreporting.googleapis.com

  log "Service accounts"
  for entry in "store-monitor:Store monitor (Play Console read-only)" \
               "store-monitor-deployer:Store monitor deployer" \
               "store-monitor-scheduler:Store monitor scheduler"; do
    name=${entry%%:*}
    if ! quiet gcloud iam service-accounts describe "$name@$PROJECT.iam.gserviceaccount.com" --project "$PROJECT"; then
      gcloud iam service-accounts create "$name" --project "$PROJECT" --display-name "${entry#*:}"
    fi
  done

  log "State bucket gs://$BUCKET"
  if ! quiet gcloud storage buckets describe "gs://$BUCKET"; then
    gcloud storage buckets create "gs://$BUCKET" --project "$PROJECT" --location "$REGION" \
      --uniform-bucket-level-access --public-access-prevention --soft-delete-duration 7d
  fi
  gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
    --member "serviceAccount:$MONITOR_SA" --role roles/storage.objectUser >/dev/null

  log "Image registry $AR_REPO (keeps the 5 newest images)"
  if ! quiet gcloud artifacts repositories describe "$AR_REPO" --project "$PROJECT" --location "$REGION"; then
    gcloud artifacts repositories create "$AR_REPO" --project "$PROJECT" --location "$REGION" --repository-format docker
  fi
  policy=$(mktemp)
  cat >"$policy" <<'JSON'
[
  {"name": "keep-recent", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 5}},
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "7d"}}
]
JSON
  gcloud artifacts repositories set-cleanup-policies "$AR_REPO" --project "$PROJECT" --location "$REGION" \
    --policy "$policy" --no-dry-run >/dev/null
  rm -f "$policy"
  gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --project "$PROJECT" --location "$REGION" \
    --member "serviceAccount:$DEPLOYER_SA" --role roles/artifactregistry.writer >/dev/null

  log "Deployer permissions"
  gcloud projects add-iam-policy-binding "$PROJECT" --condition None \
    --member "serviceAccount:$DEPLOYER_SA" --role roles/run.developer >/dev/null
  # Secret metadata only (never values), so deploy.yml maps just the secrets that exist.
  gcloud projects add-iam-policy-binding "$PROJECT" --condition None \
    --member "serviceAccount:$DEPLOYER_SA" --role roles/secretmanager.viewer >/dev/null
  gcloud iam service-accounts add-iam-policy-binding "$MONITOR_SA" --project "$PROJECT" \
    --member "serviceAccount:$DEPLOYER_SA" --role roles/iam.serviceAccountUser >/dev/null

  log "Keyless GitHub sign-in, limited to $REPO on main"
  read -r repo_id owner_id < <(gh api "repos/$REPO" --jq '"\(.id) \(.owner.id)"')
  number=$(project_number)
  if ! quiet gcloud iam workload-identity-pools describe "$POOL" --project "$PROJECT" --location global; then
    gcloud iam workload-identity-pools create "$POOL" --project "$PROJECT" --location global \
      --display-name "Game store monitor"
  fi
  condition="assertion.repository_owner_id == '$owner_id' && assertion.repository_id == '$repo_id' && assertion.ref == 'refs/heads/main' && assertion.workflow_ref in ['$REPO/.github/workflows/store-monitor.yml@refs/heads/main', '$REPO/.github/workflows/deploy.yml@refs/heads/main']"
  mapping="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.workflow=assertion.workflow_ref.extract('/.github/workflows/{file}@')"
  if quiet gcloud iam workload-identity-pools providers describe "$PROVIDER" --project "$PROJECT" --location global --workload-identity-pool "$POOL"; then
    gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --project "$PROJECT" --location global \
      --workload-identity-pool "$POOL" --attribute-mapping "$mapping" --attribute-condition "$condition" >/dev/null
  else
    gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project "$PROJECT" --location global \
      --workload-identity-pool "$POOL" --issuer-uri https://token.actions.githubusercontent.com \
      --attribute-mapping "$mapping" --attribute-condition "$condition"
  fi
  principal="principalSet://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools/$POOL/attribute.workflow"
  # Only the manual-check workflow may act as the monitor, and only the deploy workflow as the deployer.
  gcloud iam service-accounts add-iam-policy-binding "$MONITOR_SA" --project "$PROJECT" \
    --member "$principal/store-monitor.yml" --role roles/iam.workloadIdentityUser >/dev/null
  gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" --project "$PROJECT" \
    --member "$principal/deploy.yml" --role roles/iam.workloadIdentityUser >/dev/null

  cmd_variables
  log "Done. Invite this account in Play Console (human step): $MONITOR_SA"
}

cmd_variables() {
  settings
  log "GitHub repository variables for $REPO"
  number=$(project_number)
  provider="projects/$number/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
  for pair in "GCP_PROJECT=$PROJECT" "GCP_REGION=$REGION" "STATE_BUCKET=$BUCKET" "IMAGE=$IMAGE" \
              "GCP_WORKLOAD_IDENTITY_PROVIDER=$provider" "GCP_SERVICE_ACCOUNT=$MONITOR_SA" \
              "GCP_DEPLOYER_SERVICE_ACCOUNT=$DEPLOYER_SA"; do
    gh variable set "${pair%%=*}" --repo "$REPO" --body "${pair#*=}"
  done
}

cmd_secret() {
  settings
  name=${1:-}
  file=${2:-}
  case " $SECRETS " in
    *" $name "*) ;;
    *) [[ "$name" =~ ^DISCORD_WEBHOOK_URL(_[A-Z0-9]+)+$ ]] ||  # Another Discord channel's webhook.
         die "Usage: $0 secret NAME [FILE], where NAME is one of: $SECRETS, or DISCORD_WEBHOOK_URL_<CHANNEL>" ;;
  esac
  if [ -n "$file" ]; then
    [ -f "$file" ] || die "No such file: $file"
  else
    read -rsp "Value for $name (input hidden): " value
    echo
    [ -n "$value" ] || die "Empty value; nothing stored"
    file=$(mktemp)
    trap 'rm -f "$file"' EXIT
    printf %s "$value" >"$file"
    unset value
  fi
  log "Secret Manager: $name"
  if quiet gcloud secrets describe "$name" --project "$PROJECT"; then
    gcloud secrets versions add "$name" --project "$PROJECT" --data-file "$file" >/dev/null
  else
    gcloud secrets create "$name" --project "$PROJECT" --replication-policy automatic --data-file "$file" >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$name" --project "$PROJECT" \
    --member "serviceAccount:$MONITOR_SA" --role roles/secretmanager.secretAccessor >/dev/null
  log "GitHub secret (manual workflow runs): $name"
  gh secret set "$name" --repo "$REPO" <"$file"
  echo "Stored. The scheduled job picks up a new value on its next run; a newly created secret needs a deploy."
}

service_url() {
  gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)' 2>/dev/null
}

estimate() {
  # Runs take about 90 seconds at a quarter vCPU; Cloud Run's free allowance is
  # 180,000 vCPU-seconds a month per billing account, then $0.000024 each.
  case "$SCHEDULE" in
    "*/"[0-9]*" * * * *") minutes=${SCHEDULE#*/}; minutes=${minutes%% *} ;;
    *) echo "Cost estimate skipped: SCHEDULE is not of the form '*/N * * * *'"; return ;;
  esac
  awk -v runs="$((43200 / minutes))" 'BEGIN {
    seconds = runs * 90 * 0.25; extra = seconds - 180000
    printf "About %d checks a month, %d vCPU-seconds: ", runs, seconds
    if (extra <= 0) print "inside the free allowance (about $0 a month)."
    else printf "about $%.2f a month beyond the free allowance.\n", extra * 0.000024
  }'
}

cmd_schedule() {
  settings
  url=$(service_url)
  [ -n "$url" ] || die "Deploy first: gh workflow run deploy.yml --repo $REPO, then wait for it to finish"
  log "Scheduler may call the service"
  gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:$SCHEDULER_SA" --role roles/run.invoker >/dev/null
  log "Schedule '$SCHEDULE' (UTC)"
  verb=create
  if quiet gcloud scheduler jobs describe "$SCHEDULER_JOB" --project "$PROJECT" --location "$REGION"; then
    verb=update
  fi
  # The scheduler signs each call with its own identity; the service rejects everyone else.
  gcloud scheduler jobs "$verb" http "$SCHEDULER_JOB" --project "$PROJECT" --location "$REGION" \
    --schedule "$SCHEDULE" --time-zone Etc/UTC --uri "$url/run" --http-method POST \
    --oidc-service-account-email "$SCHEDULER_SA" --oidc-token-audience "$url" \
    --attempt-deadline 280s >/dev/null
  echo "Scheduled."
  estimate
}

cmd_commands() {
  settings
  url=$(service_url)
  url=${url:-SERVICE_URL}
  call="curl -X POST -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" \"$url/run"
  cat <<EOF
# Deploy (also runs on every push to main that changes the code or config.json)
gh workflow run deploy.yml --repo $REPO

# Connection tests: these send a message and nothing else
$call?mode=test-telegram"
$call?mode=test-discord"

# Dry run: read the stores, log what would be sent, save nothing
$call?mode=dry-run"

# One real check now (the first one records a quiet baseline)
$call"

# Logs
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="$SERVICE"' --project $PROJECT --limit 50 --format 'value(textPayload)'

# Pause or resume the schedule
gcloud scheduler jobs pause $SCHEDULER_JOB --project $PROJECT --location $REGION
gcloud scheduler jobs resume $SCHEDULER_JOB --project $PROJECT --location $REGION
EOF
}

command=${1:-}
[ $# -gt 0 ] && shift
case "$command" in
  check | infra | variables | schedule | commands) "cmd_$command" ;;
  secret) cmd_secret "$@" ;;
  *) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
