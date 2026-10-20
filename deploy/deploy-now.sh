#!/usr/bin/env bash
# One command to get a live SASI link. Run it in Google Cloud Shell from the repo folder:
#   bash deploy/deploy-now.sh
# It runs the one-time setup (safe to repeat; skip it with SKIP_SETUP=1), builds the app
# on Google Cloud Build (no large upload from Cloud Shell), deploys it to Cloud Run and
# prints the link. Later deploys can also come from GitHub Actions.
# Optional model settings, for example a different Claude model on Vertex AI:
#   EXTRA_ENV="VERTEX_CLAUDE_MODEL=claude-fable-5-1" bash deploy/deploy-now.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT_ID="${PROJECT_ID:-sasi-511906}"
REGION="${REGION:-us-central1}"
SERVICE=sasi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/sasi/${SERVICE}:build-$(date +%Y%m%d%H%M%S)"

retry() {  # retry with growing waits; covers flaky networks and new permissions still spreading
  local n=1
  until "$@"; do
    if [ "$n" -ge 4 ]; then echo "Failed after 4 tries: $*"; return 1; fi
    echo "Retrying in $((n * 30))s..."; sleep $((n * 30)); n=$((n + 1))
  done
}

ensure_login() {  # Cloud Shell logins can expire during long sessions; sign in again if needed
  if ! gcloud auth print-access-token >/dev/null 2>&1; then
    echo "== Your Google Cloud login has expired. Follow the link below to sign in again."
    gcloud auth login --update-adc
    gcloud config set project "$PROJECT_ID" >/dev/null
  fi
}
ensure_login

if [ "${SKIP_SETUP:-0}" != "1" ]; then
  bash deploy/setup-gcp.sh
fi

build_on_cloud_build() {
  gcloud builds submit . \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --config deploy/cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE}" \
    --service-account "projects/${PROJECT_ID}/serviceAccounts/sasi-builder@${PROJECT_ID}.iam.gserviceaccount.com" \
    --gcs-source-staging-dir "gs://${PROJECT_ID}-sasi-build/source"
}

build_in_cloud_shell() {
  gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
  docker build -t "$IMAGE" . && retry docker push "$IMAGE"
}

echo "== Building the app on Cloud Build (about 5 minutes)"
ensure_login
if ! retry build_on_cloud_build; then
  echo "== Cloud Build did not work; building inside Cloud Shell instead"
  build_in_cloud_shell
fi

echo "== Deploying to Cloud Run"
SECRETS="APP_ACCESS_CODE=APP_ACCESS_CODE:latest,SESSION_SECRET=SESSION_SECRET:latest"
deploy_service() {
  ensure_login
  gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$IMAGE" \
  --service-account "sasi-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --allow-unauthenticated \
  --min-instances 0 \
  --no-cpu-throttling \
  --max-instances 1 \
  --cpu 1 \
  --memory 1Gi \
  --concurrency 80 \
  --timeout 3600 \
  --update-env-vars "STORAGE_BACKEND=bigquery,KEY_STORE=secretmanager,GCP_PROJECT=${PROJECT_ID},BIGQUERY_DATASET=sasi_data,UPLOAD_BUCKET=${PROJECT_ID}-uploads,MAX_OUTPUT_TOKENS_PER_CALL=64000,ERIS_MAX_OUTPUT_TOKENS_PER_CALL=64000,VERTEX_AI=true,VERTEX_CLAUDE_ENABLED=false,ALLOW_CLAUDE_API_KEY=true,TRIAL_CREDITS_ONLY=true,TRIAL_CREDIT_EXPIRES_AT=2026-11-18T00:00:00Z,REQUIRE_ACCESS_TOKEN=true,ALLOW_SIMULATED=false,OPEN_MODELS=glm+deepseek+qwen+kimi+minimax+gptoss,MAX_CONCURRENT_RUNS=1,MAX_MODEL_CALLS_PER_RUN=300,MAX_TOKENS_PER_RUN=5000000,ERIS_MAX_TOKENS_PER_RUN=5000000,PROVIDER_REQUEST_TIMEOUT_SECONDS=900,RUN_TIME_LIMIT_MINUTES=120,MAX_TOTAL_RUN_MINUTES=120,MODEL_PHASE_MIN_MINUTES=20,MODEL_PHASE_GRACE_MINUTES=5${EXTRA_ENV:+,$EXTRA_ENV}" \
  --update-secrets "$SECRETS" \
  --quiet
}
retry deploy_service

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')
echo
echo "=============================================="
echo " SASI is live: $URL"
echo " Sign in with your existing SASI access code."
echo "=============================================="
