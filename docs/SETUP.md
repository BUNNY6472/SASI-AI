# Deploying SASI to Google Cloud

Project: `sasi-511906` · Region: `us-central1` · Service: `sasi` on Cloud Run.

You do steps 1 to 3 once. After that, every merge to `main` deploys automatically.

## 1. Protect the budget (2 minutes)

Google Cloud console → **Billing** → **Budgets & alerts** → **Create budget**

- Scope: project `sasi-511906`
- Amount: ₹20,000
- Alerts at 50%, 90% and 100%

Alerts only email you; they do not stop spending. The app's own guards also limit each run (40 model calls, 200,000 tokens) and Cloud Run is capped at one instance that scales to zero when idle.

## 2. Run the setup script in Cloud Shell (5 minutes)

Open Cloud Shell (the `>_` icon at the top of the console), then:

```bash
git clone https://github.com/BUNNY6472/SASI-AI.git
cd SASI-AI
git checkout claude/sasi-multi-ai-app-c549ms   # until the PR is merged
bash deploy/setup-gcp.sh
```

It turns on the APIs, creates the image registry, two service accounts, the keyless GitHub login, and the app's access code. Everything it creates is free or inside the free tier. At the end it prints four values.

## 3. Add the four values in GitHub (2 minutes)

GitHub → **BUNNY6472/SASI-AI** → **Settings** → **Secrets and variables** → **Actions** → **Variables** tab → **New repository variable**, once for each:

| Name | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | printed by the script (`projects/…/providers/sasi-ai`) |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `sasi-deployer@sasi-511906.iam.gserviceaccount.com` |
| `GCP_PROJECT_ID` | `sasi-511906` |
| `GCP_REGION` | `us-central1` |

These are not secret; no key file is stored in GitHub.

## 4. Deploy

Merge the PR, or run **Actions → Deploy to Cloud Run → Run workflow**. The run summary shows the app's URL.

Get the access code to open the app:

```bash
gcloud secrets versions access latest --secret APP_ACCESS_CODE
```

The app starts in demo mode.

## 5. Add the API keys (when you have them)

In Cloud Shell, once per provider:

```bash
bash deploy/add-api-key.sh claude
bash deploy/add-api-key.sh chatgpt
bash deploy/add-api-key.sh gemini
```

Then **Actions → Deploy to Cloud Run → Run workflow**. Open **Connections** in the app to see each model connected and which model version it uses.

Where to get keys: Claude at console.anthropic.com, ChatGPT at platform.openai.com, Gemini at aistudio.google.com. Set a monthly spend limit in each provider's console too; those charges are billed by the providers, not by Google Cloud.

## What this costs on Google Cloud

| Service | Use | Free tier |
|---|---|---|
| Cloud Run | 1 instance max, scales to zero | 2M requests and 180k vCPU-seconds a month |
| Artifact Registry | keeps the 2 newest images | 0.5 GB |
| Secret Manager | 5 secrets | 6 active versions |
| BigQuery | run checkpoints | 10 GB storage, 1 TB queries a month |

Streaming rows into BigQuery costs about $0.01 per 200 MB, which is far beyond what this app writes. Model API usage is the main cost, and it is billed by Anthropic, OpenAI and Google AI Studio.

## Good to know

- Keep a run's page open while it works. Cloud Run only gives the app CPU while a request is open, so closing the page pauses the run. If the instance shuts down, the run shows **Interrupted** and **Resume from checkpoint** continues it without redoing accepted work.
- To change the access code: `printf 'new-code' | gcloud secrets versions add APP_ACCESS_CODE --data-file=-`, then redeploy.
