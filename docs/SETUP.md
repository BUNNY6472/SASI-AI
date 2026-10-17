# Deploying SASI to Google Cloud

Project: `sasi-511906` · Region: `us-central1` · Service: `sasi` on Cloud Run.

You do steps 1 to 3 once. After that, every merge to `main` deploys automatically.

## Fastest: one command for a live link

In Cloud Shell (the `>_` icon at the top of the Google Cloud console):

```bash
git clone -b claude/sasi-multi-ai-app-c549ms https://github.com/BUNNY6472/SASI-AI.git && cd SASI-AI && bash deploy/deploy-now.sh
```

It sets everything up, builds the app on Cloud Build (inside the free tier), deploys it and prints the link and access code. Do step 1 below too, and step 3 if you want merges to deploy automatically.

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

The app reaches Claude, ChatGPT and Gemini through Google Cloud Vertex AI, so it works without API keys once the models are enabled (next step).

## 5. Enable the models in Vertex AI Model Garden (one time)

Gemini works straight away. Claude and OpenAI's models are partner models: open [Vertex AI Model Garden](https://console.cloud.google.com/vertex-ai/model-garden) in project `sasi-511906`, open **Claude Opus 5.5** and press **Enable**, then open **gpt-oss-120b** and press **Enable**. Then press **Check again** on the app's Connections page.

Google's free trial can't use partner models such as Claude, and its credit doesn't pay for them; gpt-oss may be treated the same way. Until the billing account is upgraded to a paid one, those show "not working" on Connections and SASI uses their API keys instead, if you saved any. Gemini on Vertex AI is paid from the trial credit.

## 6. API keys (optional)

Open the app, go to **Connections**, and paste each key (Claude, ChatGPT, Gemini). SASI checks each key, stores it in Secret Manager and starts using it straight away. No redeploy is needed. Keys are never shown again; only their last 4 characters are.

A key is the other route for that model. Claude and Gemini use Vertex AI first and the key as the backup; ChatGPT uses its key first, because the OpenAI API has the newest GPT model while Vertex AI only has OpenAI's open-weight gpt-oss.

Where to get keys: Claude at console.anthropic.com, ChatGPT at platform.openai.com, Gemini at aistudio.google.com. Set a monthly spend limit in each provider's console too; those charges are billed by the providers, not by Google Cloud.

## What this costs on Google Cloud

| Service | Use | Free tier |
|---|---|---|
| Cloud Run | 1 instance max, scales to zero | 2M requests and 180k vCPU-seconds a month |
| Artifact Registry | keeps the 2 newest images | 0.5 GB |
| Secret Manager | 5 secrets | 6 active versions |
| BigQuery | runs, every model's output, attached files | 10 GB storage, 1 TB queries a month |
| Cloud Build | builds the image when you use deploy-now.sh | 2,500 build-minutes a month |
| Vertex AI | Claude, Gemini and gpt-oss calls | none: billed per token to the project (Gemini from the trial credit; partner models need a paid account) |

SASI writes to BigQuery with load jobs, which are free. Files count toward the 10 GB of free storage, so delete finished runs you no longer need. Model usage is the main cost. Through Vertex AI it is billed to this Google Cloud project, so set a budget alert under Billing > Budgets & alerts. With API keys it is billed by Anthropic, OpenAI and Google AI Studio.

## Good to know

- Keep a run's page open while it works. Cloud Run only gives the app CPU while a request is open, so closing the page pauses the run. If the instance shuts down, the run shows **Interrupted** and **Resume from checkpoint** continues it without redoing accepted work.
- To change the access code: `printf 'new-code' | gcloud secrets versions add APP_ACCESS_CODE --data-file=-`, then redeploy.
