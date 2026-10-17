# Deploying SASI to Google Cloud

Project: `sasi-511906` · Region: `us-central1` · Service: `sasi` on Cloud Run.

You do steps 1 to 3 once. After that, every merge to `main` deploys automatically.

## Fastest: one command for a live link

In Cloud Shell (the `>_` icon at the top of the Google Cloud console):

```bash
git clone -b claude/sasi-multi-ai-app-c549ms https://github.com/BUNNY6472/SASI-AI.git && cd SASI-AI && bash deploy/deploy-now.sh
```

The repository is private, so Cloud Shell must sign in to GitHub once before `git clone` or `git pull` works. Run `gh auth login` and choose **GitHub.com**, **HTTPS**, **Yes** (authenticate Git) and **Login with a web browser**, then open the link it shows and enter the code. Cloud Shell remembers it.

To redeploy after changes: `cd ~/SASI-AI && git pull && bash deploy/deploy-now.sh`

It sets everything up, builds the app on Cloud Build (inside the free tier), deploys it and prints the link and access code. Do step 1 below too, and step 3 if you want merges to deploy automatically.

## 1. Protect the budget (2 minutes)

Google Cloud console → **Billing** → **Budgets & alerts** → **Create budget**

- Scope: project `sasi-511906`
- Amount: ₹20,000
- Alerts at 50%, 90% and 100%

Alerts only email you; they do not stop spending. The app's own guards also limit each run (40 model calls, 200,000 tokens) and Cloud Run is capped at one instance that scales to zero when idle.

## 2. Run the setup script in Cloud Shell (5 minutes)

Open Cloud Shell (the `>_` icon at the top of the console), sign in to GitHub once with `gh auth login` (see above), then:

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

The app reaches Claude, Gemini and the open models (GLM, DeepSeek, Qwen, Kimi) through Google Cloud Vertex AI, so it works without API keys once the models are enabled (next step).

## 5. Enable the models in Vertex AI Model Garden (one time)

Gemini works straight away. For the others, open [Vertex AI Model Garden](https://console.cloud.google.com/vertex-ai/model-garden) in project `sasi-511906` and search for each model below. Open the card whose name ends in **API Service**, not the one with a **Deploy** button: Deploy runs the model on your own GPU server, billed by the hour. If the API Service card shows **Enable**, press it and accept the terms. GLM 5.2 and DeepSeek V4.1 Flash show no Enable button and are ready to use. To check from PowerShell that a model answers, run `deploy/check-open-models.ps1`.

- **Claude Opus 5.5**
- **GLM 5.2** (Z.ai)
- **DeepSeek V4.1 Flash**
- **Qwen3 Coder 480B**
- **Kimi K2 Thinking**

Then press **Check again** on the app's Connections page. Each run uses up to four of them: Claude and Gemini first, then the open models that suit the task. A model you don't enable is skipped and the next one takes its seat.

Google's free trial can't use partner models such as Claude, and its credit doesn't pay for them; the open models may be treated the same way. Until the billing account is upgraded to a paid one, those may show "not working" on Connections. Claude then uses its API key instead, if you saved one. Gemini on Vertex AI is paid from the trial credit.

## 6. API keys (optional)

Open the app, go to **Connections**, and paste a Claude or Gemini key. SASI checks each key, stores it in Secret Manager and starts using it straight away. No redeploy is needed. Keys are never shown again; only their last 4 characters are. The open models need no key.

A key is the backup route for that model: Claude and Gemini use Vertex AI first and the key if Vertex fails.

Where to get keys: Claude at console.anthropic.com, Gemini at aistudio.google.com. A Gemini key on AI Studio's free tier costs nothing (it has rate limits, and Google may use free-tier prompts to improve its products). Set a monthly spend limit in each provider's console too; those charges are billed by the providers, not by Google Cloud.

## What this costs on Google Cloud

| Service | Use | Free tier |
|---|---|---|
| Cloud Run | 1 instance max, scales to zero | 2M requests and 180k vCPU-seconds a month |
| Artifact Registry | keeps the 2 newest images | 0.5 GB |
| Secret Manager | 4 secrets | 6 active versions |
| BigQuery | runs, every model's output, attached files | 10 GB storage, 1 TB queries a month |
| Cloud Build | builds the image when you use deploy-now.sh | 2,500 build-minutes a month |
| Vertex AI | Claude, Gemini and open-model calls | none: billed per token to the project (Gemini from the trial credit; partner models need a paid account) |

SASI writes to BigQuery with load jobs, which are free. Files count toward the 10 GB of free storage, so delete finished runs you no longer need. Model usage is the main cost. Through Vertex AI it is billed to this Google Cloud project, so set a budget alert under Billing > Budgets & alerts. With API keys it is billed by Anthropic and Google AI Studio.

## Good to know

- Each run has a one-hour limit. In the last 5 minutes SASI stops new work and delivers the complete result from what is done. The clock pauses while a plan waits for your approval. Change it with `EXTRA_ENV="RUN_TIME_LIMIT_MINUTES=90,WRAP_UP_MINUTES=8" bash deploy/deploy-now.sh`.
- Keep a run's page open while it works. Cloud Run only gives the app CPU while a request is open, so closing the page pauses the run (its clock keeps going). If the instance shuts down, the run shows **Interrupted** and **Resume from checkpoint** continues it with the time it has left, without redoing accepted work.
- To change the access code: `printf 'new-code' | gcloud secrets versions add APP_ACCESS_CODE --data-file=-`, then redeploy.
