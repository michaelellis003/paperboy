#!/usr/bin/env bash
# Deploy paperboy to Cloud Run — hardened and free-tier friendly.
#
# What this sets up, and why:
#   - A dedicated GCP project (blast-radius isolation from your other work)
#   - Secrets in Secret Manager (never baked into the image)
#   - A minimal service account whose ONLY permission is reading those
#     secrets (the default compute SA is far too broad)
#   - --max-instances=1 and scale-to-zero: a request flood cannot spin
#     up fleet-sized costs, and an idle server costs nothing
#   - us-central1: an "always free" tier-1 region (2M requests/month,
#     180k vCPU-seconds — a personal MCP server stays at ~$0)
#   - Public ingress + app-level bearer auth: claude.ai connectors
#     cannot send Google IAM tokens, so the gate is MCP_AUTH_TOKEN —
#     requests without it are rejected in microseconds (401)
#
# Usage:
#   ./deploy/deploy.sh PROJECT_ID [BILLING_ACCOUNT_ID]
#
# Prereqs: `gcloud auth login` done; a .env produced by `paperboy setup`
# in the repo root. Re-running the script updates secrets + redeploys.

set -euo pipefail

PROJECT_ID="${1:?usage: deploy.sh PROJECT_ID [BILLING_ACCOUNT_ID]}"
BILLING_ACCOUNT="${2:-}"
REGION="us-central1"
SERVICE="paperboy"
SA_NAME="paperboy-run"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

# Env vars that belong in Secret Manager; everything else configured
# here rides along as a plain env var.
SECRET_VARS=(MCP_AUTH_TOKEN SMTP_PASSWORD ZOTERO_API_KEY
  DROPBOX_APP_SECRET DROPBOX_REFRESH_TOKEN)
PLAIN_VARS=(DELIVERY_METHOD DEVICE_EMAIL FROM_EMAIL SMTP_HOST SMTP_PORT
  SMTP_USER DROPBOX_APP_KEY DROPBOX_FOLDER ZOTERO_LIBRARY_ID
  ZOTERO_LIBRARY_TYPE READING_QUEUE_COLLECTION SENT_TAG CONTACT_EMAIL)

command -v gcloud >/dev/null || {
  echo "gcloud is not installed — https://cloud.google.com/sdk/docs/install" >&2
  exit 1
}
ACTIVE_ACCOUNT=$(gcloud config get-value account 2>/dev/null || true)
[[ -n "$ACTIVE_ACCOUNT" ]] || {
  echo "gcloud is not authenticated — run: gcloud auth login" >&2
  exit 1
}
# Catch stale tokens early, before half-creating resources.
gcloud projects list --limit=1 >/dev/null 2>&1 || {
  echo "gcloud credentials are stale — run: gcloud auth login" >&2
  exit 1
}

[[ -f "$ENV_FILE" ]] || {
  echo "No .env at ${ENV_FILE} — run 'uv run paperboy setup' first." >&2
  exit 1
}
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

[[ -n "${MCP_AUTH_TOKEN:-}" ]] || {
  echo "MCP_AUTH_TOKEN is empty — the server refuses to run HTTP" >&2
  echo "without it. Re-run 'uv run paperboy setup' (remote option)." >&2
  exit 1
}

echo "==> Project: ${PROJECT_ID}"
if ! gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  if ! gcloud projects create "$PROJECT_ID" 2>/tmp/paperboy-create.err; then
    if grep -q "already in use" /tmp/paperboy-create.err; then
      echo "Project id '${PROJECT_ID}' is taken GLOBALLY (all of GCP," >&2
      echo "not just your account) — try a more specific id, e.g." >&2
      echo "'${PROJECT_ID}-$(whoami)' or '${PROJECT_ID}-mcp'." >&2
    else
      cat /tmp/paperboy-create.err >&2
    fi
    exit 1
  fi
fi

if [[ -z "$BILLING_ACCOUNT" ]]; then
  BILLING_ACCOUNT=$(gcloud billing accounts list --filter=open=true \
    --format="value(name)" | head -1)
fi
[[ -n "$BILLING_ACCOUNT" ]] || {
  echo "No open billing account found; pass one as the 2nd arg." >&2
  exit 1
}
gcloud billing projects link "$PROJECT_ID" \
  --billing-account="$BILLING_ACCOUNT" >/dev/null

echo "==> Enabling APIs (first run takes a minute)"
gcloud services enable run.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com \
  billingbudgets.googleapis.com --project "$PROJECT_ID"

# Two fresh-project gaps that otherwise fail the first deploy (both
# hit during this script's development):
#   1. The source-deploy Artifact Registry repo is not auto-created
#      reliably on brand-new projects.
#   2. Cloud Build runs as the default compute SA, which lacks its
#      builder role (source-bucket read + registry write) until
#      something grants it.
echo "==> Fresh-project build prerequisites"
if ! gcloud artifacts repositories describe cloud-run-source-deploy \
  --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create cloud-run-source-deploy \
    --repository-format=docker --location "$REGION" \
    --project "$PROJECT_ID" >/dev/null
fi
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" \
  --format="value(projectNumber)")
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role roles/cloudbuild.builds.builder --condition=None >/dev/null
# IAM grants propagate asynchronously; a fresh grant needs a moment.
sleep 15

echo "==> Service account with least privilege"
if ! gcloud iam service-accounts describe "$SA_EMAIL" \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" \
    --project "$PROJECT_ID" --display-name "paperboy Cloud Run runtime"
fi

echo "==> Secrets"
SET_SECRETS=()
for var in "${SECRET_VARS[@]}"; do
  value="${!var:-}"
  [[ -n "$value" ]] || continue
  if gcloud secrets describe "$var" --project "$PROJECT_ID" \
    >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$var" \
      --project "$PROJECT_ID" --data-file=- >/dev/null
  else
    printf '%s' "$value" | gcloud secrets create "$var" \
      --project "$PROJECT_ID" --replication-policy=automatic \
      --data-file=- >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$var" --project "$PROJECT_ID" \
    --member "serviceAccount:${SA_EMAIL}" \
    --role roles/secretmanager.secretAccessor >/dev/null
  SET_SECRETS+=("${var}=${var}:latest")
done

SET_ENV=()
for var in "${PLAIN_VARS[@]}"; do
  value="${!var:-}"
  [[ -n "$value" ]] && SET_ENV+=("${var}=${value}")
done

join() { local IFS=","; echo "$*"; }

echo "==> Deploying to Cloud Run (${REGION})"
gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --source "$REPO_ROOT" \
  --service-account "$SA_EMAIL" \
  --allow-unauthenticated \
  --max-instances 1 \
  --min-instances 0 \
  --memory 512Mi \
  --cpu 1 \
  --timeout 300 \
  --set-secrets "$(join "${SET_SECRETS[@]}")" \
  --set-env-vars "$(join "${SET_ENV[@]}")"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" \
  --region "$REGION" --format="value(status.url)")

echo "==> Budget alert (\$1/month, scoped to this project)"
BUDGET_NOTE="Budget alert created — you'll be emailed if costs appear."
if ! gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" \
  --format="value(displayName)" 2>/dev/null \
  | grep -q "^${SERVICE}-${PROJECT_ID}-guardrail$"; then
  if ! gcloud billing budgets create \
    --billing-account="$BILLING_ACCOUNT" \
    --display-name="${SERVICE}-${PROJECT_ID}-guardrail" \
    --budget-amount=1USD \
    --filter-projects="projects/${PROJECT_ID}" >/dev/null 2>&1; then
    BUDGET_NOTE="Could not create the budget (needs Billing Account
  Costs Manager). Create it manually:
  https://console.cloud.google.com/billing/budgets
  (\$1/month scoped to project ${PROJECT_ID})"
  fi
fi

cat <<DONE

==> Deployed.

  MCP endpoint:  ${URL}/mcp
  Auth:          Bearer token from MCP_AUTH_TOKEN in your .env

claude.ai -> Settings -> Connectors -> Add custom connector:
  URL: ${URL}/mcp
  and supply the bearer token.

Cost guardrails in place: max 1 instance, scales to zero when idle,
free-tier region. ${BUDGET_NOTE}
DONE
