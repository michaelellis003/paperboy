#!/usr/bin/env bash
# Deploy paperboy to Cloud Run — hardened and free-tier friendly.
#
# What this sets up, and why:
#   - A dedicated GCP project (blast-radius isolation from your other work)
#   - Secrets in Secret Manager (never baked into the image or argv)
#   - A minimal service account whose ONLY permission is reading those
#     secrets (the default compute SA is far too broad)
#   - --max-instances=1 and scale-to-zero: a request flood cannot spin
#     up fleet-sized costs, and an idle server costs nothing
#   - us-central1: an "always free" tier-1 region. Note the free tier
#     (2M requests/month, 180k vCPU-seconds) is shared across your
#     whole BILLING ACCOUNT, not per project
#   - A $1/month budget with 50% and 100% alert thresholds (the
#     gcloud budgets API adds NO default thresholds — without explicit
#     rules a budget never notifies anyone)
#   - Public ingress + app-level bearer auth: claude.ai connectors
#     cannot send Google IAM tokens, so the gate is MCP_AUTH_TOKEN —
#     requests without it are rejected in microseconds (401)
#
# Usage:
#   ./deploy/deploy.sh PROJECT_ID [BILLING_ACCOUNT_ID]
#
# Prereqs: `gcloud auth login` done; a .env produced by `paperboy setup`
# in the repo root. Re-running is a clean sync: unchanged secrets add
# no new versions, and the budget is not duplicated.

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

# Parse .env with python-dotenv — the EXACT parser the local server
# uses — so cloud and local can never disagree about what a value is.
# (Never `source` it: shell evaluation corrupts passwords containing
# $ or backticks; a hand-rolled parser diverges on dotenv-isms like
# inline comments, 'export ' prefixes, or spaces around '='.) Only
# the allowlisted variables are exported, so a stray PATH= line in
# .env cannot hijack this script.
command -v uv >/dev/null || {
  echo "uv is required (same tool as 'uv run paperboy setup'):" >&2
  echo "  https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
}
DOTENV_EXPORTS="$(uv run --directory "$REPO_ROOT" python - "$ENV_FILE" \
  "${SECRET_VARS[@]}" "${PLAIN_VARS[@]}" KINDLE_EMAIL <<'PY'
import shlex
import sys

from dotenv import dotenv_values

env_file, *allowed = sys.argv[1:]
values = dotenv_values(env_file)
for key in allowed:
    value = values.get(key)
    if value:
        print(f"export {key}={shlex.quote(value)}")
PY
)" || {
  echo "Failed to parse ${ENV_FILE} with python-dotenv." >&2
  exit 1
}
eval "$DOTENV_EXPORTS"

# The app accepts KINDLE_EMAIL as an alias for DEVICE_EMAIL; honor it
# here too, or alias users deploy a cloud instance with no device.
DEVICE_EMAIL="${DEVICE_EMAIL:-${KINDLE_EMAIL:-}}"

TOKEN="${MCP_AUTH_TOKEN:-}"
[[ ${#TOKEN} -ge 32 ]] || {
  echo "MCP_AUTH_TOKEN must be at least 32 chars (the server refuses" >&2
  echo "to start otherwise, and Cloud Run's error for that is" >&2
  echo "opaque). Re-run 'uv run paperboy setup' (remote option), or:" >&2
  echo "  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'" >&2
  exit 1
}

echo "==> Project: ${PROJECT_ID}"
if ! gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  CREATE_ERR="$(mktemp)"
  if ! gcloud projects create "$PROJECT_ID" 2>"$CREATE_ERR"; then
    if grep -q "already in use" "$CREATE_ERR"; then
      echo "Project id '${PROJECT_ID}' is taken GLOBALLY (all of GCP," >&2
      echo "not just your account) — try a more specific id, e.g." >&2
      echo "'${PROJECT_ID}-$(whoami)' or '${PROJECT_ID}-mcp'." >&2
    else
      cat "$CREATE_ERR" >&2
    fi
    rm -f "$CREATE_ERR"
    exit 1
  fi
  rm -f "$CREATE_ERR"
fi

if [[ -z "$BILLING_ACCOUNT" ]]; then
  ACCOUNT_COUNT=$(gcloud billing accounts list --filter=open=true \
    --format="value(name)" | wc -l | tr -d ' ')
  if [[ "$ACCOUNT_COUNT" -gt 1 ]]; then
    echo "You have ${ACCOUNT_COUNT} open billing accounts — pass the" >&2
    echo "one to charge as the 2nd argument (never guessing which," >&2
    echo "it could be an employer's):" >&2
    gcloud billing accounts list --filter=open=true \
      --format="table(name,displayName)" >&2
    exit 1
  fi
  BILLING_ACCOUNT=$(gcloud billing accounts list --filter=open=true \
    --format="value(name)" | head -1)
fi
[[ -n "$BILLING_ACCOUNT" ]] || {
  echo "No open billing account found; pass one as the 2nd arg." >&2
  exit 1
}
echo "==> Billing account: ${BILLING_ACCOUNT}"
gcloud billing projects link "$PROJECT_ID" \
  --billing-account="$BILLING_ACCOUNT" >/dev/null

echo "==> Enabling APIs (first run takes a minute)"
gcloud services enable run.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com \
  billingbudgets.googleapis.com --project "$PROJECT_ID"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" \
  --format="value(projectNumber)")
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

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
if ! gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten="bindings[].members" \
  --filter="bindings.role:roles/cloudbuild.builds.builder AND bindings.members:serviceAccount:${COMPUTE_SA}" \
  --format="value(bindings.role)" 2>/dev/null | grep -q .; then
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${COMPUTE_SA}" \
    --role roles/cloudbuild.builds.builder --condition=None >/dev/null
  # IAM grants propagate asynchronously; 15s proved marginal in
  # practice (a first deploy raced it), so wait longer.
  sleep 30
fi

# Cleanup policy: keep the 2 newest images, delete the rest after 30
# days — otherwise every deploy accumulates ~90 MB toward the 0.5 GB
# Artifact Registry free tier. Non-fatal if permissions are missing.
CLEANUP_POLICY="$(mktemp)"
cat > "$CLEANUP_POLICY" <<'JSON'
[
  {
    "name": "keep-latest",
    "action": {"type": "Keep"},
    "mostRecentVersions": {"keepCount": 2}
  },
  {
    "name": "delete-old",
    "action": {"type": "Delete"},
    "condition": {"olderThan": "2592000s"}
  }
]
JSON
gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy \
  --location "$REGION" --project "$PROJECT_ID" \
  --policy "$CLEANUP_POLICY" --no-dry-run >/dev/null 2>&1 \
  || echo "    (could not set image cleanup policy — prune manually)"
rm -f "$CLEANUP_POLICY"

echo "==> Service account with least privilege"
if ! gcloud iam service-accounts describe "$SA_EMAIL" \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" \
    --project "$PROJECT_ID" --display-name "paperboy Cloud Run runtime"
fi

echo "==> Secrets (unchanged values add no new versions)"
SET_SECRETS=()
for var in "${SECRET_VARS[@]}"; do
  value="${!var:-}"
  [[ -n "$value" ]] || continue
  if gcloud secrets describe "$var" --project "$PROJECT_ID" \
    >/dev/null 2>&1; then
    current=$(gcloud secrets versions access latest --secret "$var" \
      --project "$PROJECT_ID" 2>/dev/null || true)
    if [[ "$current" != "$value" ]]; then
      printf '%s' "$value" | gcloud secrets versions add "$var" \
        --project "$PROJECT_ID" --data-file=- >/dev/null
    fi
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
  # '|' is the deploy-flag list delimiter below; a value containing it
  # would silently corrupt every env var. Fail loudly instead.
  if [[ "$value" == *"|"* ]]; then
    echo "ERROR: ${var} contains a '|' character, which the deploy" >&2
    echo "flags cannot carry. Please remove it from .env." >&2
    exit 1
  fi
  [[ -n "$value" ]] && SET_ENV+=("${var}=${value}")
done

# '|' as the list delimiter (^|^ syntax): commas can legitimately
# appear inside values, pipes cannot.
join_flags() { local IFS="|"; echo "^|^$*"; }

DEPLOY_FLAGS=(--set-secrets "$(join_flags "${SET_SECRETS[@]}")")
if [[ ${#SET_ENV[@]} -gt 0 ]]; then
  DEPLOY_FLAGS+=(--set-env-vars "$(join_flags "${SET_ENV[@]}")")
fi

# Budget BEFORE the deploy: a first run that dies mid-build must not
# leave a billing-linked project with no guardrail.
echo "==> Budget alert (\$1/month, 50% and 100% thresholds)"
BUDGET_NOTE="Budget alert active: Billing Account Admins/Users are
emailed at 50% and 100% of \$1/month."
# Idempotency keys off the budget's project filter (the API stores
# project NUMBERS), never the display name, which can drift. The
# anchored match prevents a longer project number false-positiving.
# If LISTING fails (create-but-not-list permissions), skip auto-create
# rather than risk stacking duplicate budgets on every re-run.
if ! BUDGET_LIST=$(gcloud billing budgets list \
  --billing-account="$BILLING_ACCOUNT" \
  --format="value(budgetFilter.projects)" 2>/dev/null); then
  BUDGET_NOTE="Could not LIST budgets (missing Billing Account
Viewer?) — auto-create skipped to avoid duplicates. Verify one
exists: https://console.cloud.google.com/billing/budgets"
elif ! grep -Eq "projects/${PROJECT_NUMBER}(\$|[^0-9])" \
  <<<"$BUDGET_LIST"; then
  if ! gcloud billing budgets create \
    --billing-account="$BILLING_ACCOUNT" \
    --display-name="${SERVICE}-${PROJECT_ID}-guardrail" \
    --budget-amount=1USD \
    --threshold-rule=percent=0.5 \
    --threshold-rule=percent=1.0 \
    --filter-projects="projects/${PROJECT_ID}" >/dev/null 2>&1; then
    BUDGET_NOTE="Could not create the budget (common causes: you lack
Billing Account Costs Manager, or your billing account's currency is
not USD — the script requests 1USD). Create it manually — WITH alert
thresholds, they are not added by default:
  https://console.cloud.google.com/billing/budgets
  (~\$1/month equivalent scoped to ${PROJECT_ID}, thresholds 50%/100%)"
  fi
fi

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
  "${DEPLOY_FLAGS[@]}"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" \
  --region "$REGION" --format="value(status.url)")

# Catch the org-policy failure class (Domain Restricted Sharing makes
# --allow-unauthenticated a silent no-op: deploy "succeeds", endpoint
# 403s for everyone). An unauthenticated probe must get the app's 401.
echo "==> Post-deploy check (expecting 401 from the auth gate)"
PROBE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${URL}/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  || echo "unreachable")
if [[ "$PROBE" == "401" ]]; then
  echo "    OK — endpoint up, bearer auth enforced."
elif [[ "$PROBE" == "403" ]]; then
  echo "    WARNING: got 403, not 401 — your organization likely" >&2
  echo "    enforces Domain Restricted Sharing, which blocks" >&2
  echo "    --allow-unauthenticated. Claude clients cannot reach" >&2
  echo "    this endpoint until that org policy is relaxed for" >&2
  echo "    project ${PROJECT_ID}." >&2
else
  echo "    WARNING: expected 401, got '${PROBE}' — check" >&2
  echo "    'gcloud run services logs read ${SERVICE}'." >&2
fi

cat <<DONE

==> Deployed.

  MCP endpoint:  ${URL}/mcp
  Auth:          Bearer token from MCP_AUTH_TOKEN in your .env

Connect a client:
  Claude Code:
    claude mcp add --transport http paperboy ${URL}/mcp \\
      --header "Authorization: Bearer <your MCP_AUTH_TOKEN>"
  Claude API: pass the token as authorization_token on the MCP
    connector.
  claude.ai / mobile: check your Add-connector dialog. If it has a
    beta "Request headers" section (rolling out slowly), paste the
    bearer header there; if it shows only URL + OAuth fields, you
    need the OAuth path (FastMCP providers — roadmapped). See the
    README's "Connecting clients" table.

Cost guardrails in place: max 1 instance, scales to zero when idle,
free-tier region (the free tier is shared across your billing
account). Note: the budget EMAILS you at 50%/100% — it does not cap
billing. ${BUDGET_NOTE}
DONE
