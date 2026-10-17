#!/usr/bin/env bash
# One command to get a live SASI link. Run it in Google Cloud Shell from the repo folder:
#   bash deploy/deploy-now.sh
# It runs the one-time setup (safe to repeat; skip it with SKIP_SETUP=1), builds the app
# on Google Cloud Build (no large upload from Cloud Shell), deploys it to Cloud Run and
# prints the link. Later deploys can also come from GitHub Actions.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT_ID="${PROJECT_ID:-sasi-511906}"
REGION="${REGION:-us-central1}"
SERVICE=sasi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/sasi/${SERVICE}:build-$(date +%Y%m%d%H%M%S)"

retry() {  # retry a command up to 3 times; Cloud Shell's network can drop connections
  local n=1
  until "$@"; do
    if [ "$n" -ge 3 ]; then echo "Failed after 3 tries: $*"; return 1; fi
    echo "Retrying in $((n * 10))s..."; sleep $((n * 10)); n=$((n + 1))
  done
}

if [ "${SKIP_SETUP:-0}" != "1" ]; then
  bash deploy/setup-gcp.sh
fi

echo "== Building the app on Cloud Build (about 5 minutes)"
retry gcloud builds submit . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --config deploy/cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE}" \
  --service-account "projects/${PROJECT_ID}/serviceAccounts/sasi-builder@${PROJECT_ID}.iam.gserviceaccount.com" \
  --default-buckets-behavior regional-user-owned-bucket

echo "== Deploying to Cloud Run"
SECRETS="APP_ACCESS_CODE=APP_ACCESS_CODE:latest,SESSION_SECRET=SESSION_SECRET:latest"
for name in ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY; do
  if gcloud secrets describe "$name" --project "$PROJECT_ID" >/dev/null 2>&1; then
    SECRETS="${SECRETS},${name}=${name}:latest"
    echo "  using $name"
  fi
done
retry gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$IMAGE" \
  --service-account "sasi-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 1 \
  --cpu 1 \
  --memory 512Mi \
  --concurrency 80 \
  --timeout 3600 \
  --set-env-vars "STORAGE_BACKEND=bigquery,GCP_PROJECT=${PROJECT_ID},BIGQUERY_DATASET=sasi_data" \
  --set-secrets "$SECRETS" \
  --quiet

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')
CODE=$(gcloud secrets versions access latest --secret APP_ACCESS_CODE --project "$PROJECT_ID")
echo
echo "=============================================="
echo " SASI is live: $URL"
echo " Access code:  $CODE"
echo "=============================================="
