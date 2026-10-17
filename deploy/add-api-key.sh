#!/usr/bin/env bash
# Store one model API key in Secret Manager. Run in Google Cloud Shell:
#   bash deploy/add-api-key.sh claude     (or gemini)
# Paste the key when asked; it is not shown or saved in shell history.
# Then re-run the "Deploy to Cloud Run" workflow in GitHub Actions so the app picks it up.
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-sasi-511906}"
case "${1:-}" in
  claude) NAME=ANTHROPIC_API_KEY ;;
  gemini) NAME=GEMINI_API_KEY ;;
  *) echo "usage: $0 claude|gemini  (open models run through Google Cloud and need no key)"; exit 1 ;;
esac
read -r -s -p "Paste the $1 API key: " KEY; echo
[ -n "$KEY" ] || { echo "No key entered"; exit 1; }
if gcloud secrets describe "$NAME" --project "$PROJECT_ID" >/dev/null 2>&1; then
  printf '%s' "$KEY" | gcloud secrets versions add "$NAME" --project "$PROJECT_ID" --data-file=- >/dev/null
  # Destroy older versions so only one is kept (Secret Manager free tier covers 6 active versions).
  for v in $(gcloud secrets versions list "$NAME" --project "$PROJECT_ID" --filter='state!=DESTROYED' --format='value(name)' --sort-by=~createTime | tail -n +2); do
    gcloud secrets versions destroy "$v" --secret "$NAME" --project "$PROJECT_ID" --quiet >/dev/null
  done
else
  printf '%s' "$KEY" | gcloud secrets create "$NAME" --project "$PROJECT_ID" --replication-policy automatic --data-file=- >/dev/null
fi
echo "Saved $NAME. Now redeploy: GitHub > Actions > Deploy to Cloud Run > Run workflow."
