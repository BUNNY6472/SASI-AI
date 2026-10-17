#!/usr/bin/env bash
# One-time Google Cloud setup for SASI. Run it in Google Cloud Shell:
#   bash deploy/setup-gcp.sh
#
# What it creates (all free-tier or free-to-exist):
#   - turns on the needed APIs
#   - Artifact Registry repo "sasi" that keeps only the 2 newest images (stays under the 0.5 GB free tier)
#   - service account sasi-runtime   (what the app runs as: BigQuery + reading secrets)
#   - service account sasi-deployer  (what GitHub Actions uses to deploy)
#   - keyless GitHub login (Workload Identity Federation) limited to the BUNNY6472/SASI-AI repo
#   - secrets APP_ACCESS_CODE and SESSION_SECRET (random values)
# It does NOT add model API keys; use deploy/add-api-key.sh for those.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-sasi-511906}"
REGION="${REGION:-us-central1}"
GITHUB_REPO_ID="${GITHUB_REPO_ID:-1057223359}"     # BUNNY6472/SASI-AI
GITHUB_OWNER_ID="${GITHUB_OWNER_ID:-142133765}"    # BUNNY6472
POOL="github"
PROVIDER="sasi-ai"

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
RUNTIME_SA="sasi-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOY_SA="sasi-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

echo "== Enabling APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  iamcredentials.googleapis.com iam.googleapis.com sts.googleapis.com bigquery.googleapis.com

echo "== Artifact Registry"
if ! gcloud artifacts repositories describe sasi --location "$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create sasi --repository-format docker --location "$REGION" --description "SASI images"
fi
cat > /tmp/sasi-cleanup.json <<'JSON'
[
  {"name": "keep-newest-2", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
  {"name": "delete-older", "action": {"type": "Delete"}, "condition": {"tagState": "ANY"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies sasi --location "$REGION" --policy /tmp/sasi-cleanup.json --no-dry-run

echo "== Service accounts"
gcloud iam service-accounts describe "$RUNTIME_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create sasi-runtime --display-name "SASI app runtime"
gcloud iam service-accounts describe "$DEPLOY_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create sasi-deployer --display-name "SASI GitHub deployer"

bind() { gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$1" --role "$2" --condition None >/dev/null; echo "  $2 -> $1"; }
bind "$RUNTIME_SA" roles/bigquery.dataEditor
bind "$RUNTIME_SA" roles/bigquery.jobUser
bind "$RUNTIME_SA" roles/secretmanager.secretAccessor
bind "$DEPLOY_SA" roles/run.admin
bind "$DEPLOY_SA" roles/artifactregistry.writer
bind "$DEPLOY_SA" roles/secretmanager.viewer
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member "serviceAccount:$DEPLOY_SA" --role roles/iam.serviceAccountUser >/dev/null

echo "== Keyless GitHub login (Workload Identity Federation)"
gcloud iam workload-identity-pools describe "$POOL" --location global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$POOL" --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers describe "$PROVIDER" --location global --workload-identity-pool "$POOL" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --location global --workload-identity-pool "$POOL" \
    --display-name "SASI-AI repo" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref" \
    --attribute-condition "assertion.repository_owner_id == '${GITHUB_OWNER_ID}' && assertion.repository_id == '${GITHUB_REPO_ID}'"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository_id/${GITHUB_REPO_ID}" >/dev/null

echo "== App secrets"
new_secret() {
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    echo "  $1 already exists (kept)"
  else
    printf '%s' "$2" | gcloud secrets create "$1" --replication-policy automatic --data-file=- >/dev/null
    echo "  created $1"
  fi
}
ACCESS_CODE="sasi-$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"
new_secret APP_ACCESS_CODE "$ACCESS_CODE"
new_secret SESSION_SECRET "$(head -c 32 /dev/urandom | base64 | tr -d '\n')"

echo
echo "================ DONE ================"
echo "Add these in GitHub: repo Settings > Secrets and variables > Actions > Variables tab"
echo "  GCP_WORKLOAD_IDENTITY_PROVIDER = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
echo "  GCP_DEPLOY_SERVICE_ACCOUNT     = ${DEPLOY_SA}"
echo "  GCP_PROJECT_ID                 = ${PROJECT_ID}"
echo "  GCP_REGION                     = ${REGION}"
echo
echo "App access code (what you type to open the app):"
echo "  gcloud secrets versions access latest --secret APP_ACCESS_CODE"
