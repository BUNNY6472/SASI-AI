#!/usr/bin/env bash
# One command to get a live SASI link. Run it in Google Cloud Shell from the repo folder:
#   bash deploy/deploy-now.sh
# It runs the one-time setup (safe to repeat), builds the app image inside Cloud Shell,
# deploys it to Cloud Run and prints the link. Later deploys can come from GitHub Actions.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT_ID="${PROJECT_ID:-sasi-511906}"
REGION="${REGION:-us-central1}"
SERVICE=sasi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/sasi/${SERVICE}:cloudshell-$(date +%Y%m%d%H%M%S)"

bash deploy/setup-gcp.sh

echo "== Building the app image (takes a few minutes)"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build -t "$IMAGE" .
docker push "$IMAGE"

echo "== Deploying to Cloud Run"
SECRETS="APP_ACCESS_CODE=APP_ACCESS_CODE:latest,SESSION_SECRET=SESSION_SECRET:latest"
for name in ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY; do
  if gcloud secrets describe "$name" --project "$PROJECT_ID" >/dev/null 2>&1; then
    SECRETS="${SECRETS},${name}=${name}:latest"
  fi
done
gcloud run deploy "$SERVICE" \
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
