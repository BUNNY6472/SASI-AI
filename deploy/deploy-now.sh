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

retry() {  # retry with growing waits; covers flaky networks and new permissions still spreading
  local n=1
  until "$@"; do
    if [ "$n" -ge 4 ]; then echo "Failed after 4 tries: $*"; return 1; fi
    echo "Retrying in $((n * 30))s..."; sleep $((n * 30)); n=$((n + 1))
  done
}

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
if ! retry build_on_cloud_build; then
  echo "== Cloud Build did not work; building inside Cloud Shell instead"
  build_in_cloud_shell
fi

echo "== Deploying to Cloud Run"
SECRETS="APP_ACCESS_CODE=APP_ACCESS_CODE:latest,SESSION_SECRET=SESSION_SECRET:latest"
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
  --set-env-vars "STORAGE_BACKEND=bigquery,KEY_STORE=secretmanager,GCP_PROJECT=${PROJECT_ID},BIGQUERY_DATASET=sasi_data" \
  --set-secrets "$SECRETS" \
  --quiet

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')
CODE=$(gcloud secrets versions access latest --secret APP_ACCESS_CODE --project "$PROJECT_ID")
echo
echo "=============================================="
echo " SASI is live: $URL"
echo " Access code:  $CODE"
echo "=============================================="
