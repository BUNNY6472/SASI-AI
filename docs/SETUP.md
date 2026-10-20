# Deploying SASI to Google Cloud

Project: `sasi-511906` · Region: `us-central1` · Service: `sasi` on Cloud Run.

You do steps 1 to 3 once. After that, a successful CI run for the current `main` commit triggers deployment of that exact tested commit. Failed CI does not deploy.

The current deployment configuration scales to zero, caps instances at one and keeps CPU available outside requests while an instance exists. Usage can consume credits. It remains a single-process workflow service: scale-down or a restart interrupts work, and **Resume** continues from the last checkpoint. Trial-only deployment permits Vertex Gemini only and requires the private owner access token. See [the current release notes](QUALITY_AND_RELEASE.md) before deploying.

## Fastest: one command for a live link

In Cloud Shell (the `>_` icon at the top of the Google Cloud console):

```bash
git clone -b main https://github.com/BUNNY6472/SASI-AI.git && cd SASI-AI && bash deploy/deploy-now.sh
```

For authenticated repository access, sign Cloud Shell in to GitHub. Run `gh auth login` and choose **GitHub.com**, **HTTPS**, **Yes** (authenticate Git) and **Login with a web browser**, then open the link it shows and enter the code. Cloud Shell remembers it.

To redeploy after changes: `cd ~/SASI-AI && git pull && bash deploy/deploy-now.sh`

It sets up the resources, builds the app on Cloud Build, deploys it and prints the link. Sign in with your existing access code. Cloud Build and the running service may incur charges. Do step 1 below too, and step 3 if you want merges to deploy automatically.

## 1. Protect the budget (2 minutes)

Google Cloud console → **Billing** → **Budgets & alerts** → **Create budget**

- Scope: project `sasi-511906`
- Amount: ₹20,000
- Alerts at 50%, 90% and 100%

Alerts only email you; they do not stop spending. By default the app limits each run to 300 model calls and 5,000,000 reported tokens (50 lakh, including Eris), reserves estimated tokens before parallel calls, and uses question-driven completion for new bridge requests. A finite per-request timeout handles stalled providers; there is no session timer. These are resource guards, not a currency-denominated billing cap. Provider retries, search charges and differences in token accounting can affect the invoice. Cloud Run is capped at one instance with instance-based billing and permits scale-to-zero with the checked-in deployment configuration.

## 2. Run the setup script in Cloud Shell (5 minutes)

The setup script provisions `${PROJECT_ID}-uploads` as a private bucket with uniform access and public access prevention. Only the runtime service account receives object access on that bucket. `deploy/upload-cors.json` permits resumable transfers from SASI's production origin; update the exact origin if you use a different domain. `UPLOAD_BUCKET` enables the 10 GiB combined attachment limit. Staged files expire after three hours; accepted originals remain until their run is deleted. Storage and transfer can consume trial credits. No GPU or managed model endpoint is provisioned for uploads.

Original storage and model inspection are separate: the app uses bounded previews and statistical reports, not whole-dataset execution. Cloud Storage holds complete bytes; authenticated downloads support ranges. A whole-dataset analysis/training sandbox remains future work.

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

Google's welcome credit excludes managed partner models and Gemini API usage through AI Studio. The deployment's `TRIAL_CREDITS_ONLY=true` blocks those Google Cloud partner routes and the Gemini API-key fallback before any checks or generation. The owner-authorized exception `ALLOW_CLAUDE_API_KEY=true` allows direct Claude with a separately funded Anthropic key. Enter it on SASI's authenticated Connections page; never commit it or paste it into deployment commands. The key is saved in `SASI_OWNER_ANTHROPIC_API_KEY`, separate from legacy keys; the setup script creates this empty secret and gives the runtime permission to manage its versions. Keep `VERTEX_CLAUDE_ENABLED=false`. Anthropic charges are outside Google Cloud budgets. The configured trial cutoff still stops all model activity, including Claude, because hosting uses the trial project. Do not upgrade the Google Cloud account under a trial-only spending requirement. Verify actual remaining credit, scope and expiry in Billing; alerts do not impose an exact spending cap.

## 6. API keys (optional)

Open the app, go to **Connections**, and paste a Claude or Gemini key. SASI checks each key, stores it in Secret Manager and starts using it straight away. No redeploy is needed. Keys are never shown again; only their last 4 characters are. The open models need no key.

Outside trial-only mode, Claude uses its API key by default and Gemini uses Vertex first with an optional key fallback. Trial-only mode disables key entry and use; stored keys remain in Secret Manager for a later funded configuration.

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

- New bridge requests have no session time selection or total runtime deadline. One model answers, another supplies a compact independent review, and a bounded correction is made only when needed.
- `PROVIDER_REQUEST_TIMEOUT_SECONDS=900` bounds each provider request. It is not a session timer. Existing saved pipeline runs retain their legacy timing settings for compatibility. Token/call/trial guards and cancellation still apply.
- Cancellation and call/token/trial guards still apply. If a stream ends without completing, available text is saved as an incomplete, unreviewed draft. It does not count as an accepted analysis or task, and it does not receive a passing review score.
- Keep a run's page open while it works. The deployed configuration allows CPU outside requests, but the instance may still scale down or restart. Interrupted work can resume from its last durable checkpoint without redoing accepted tasks.
- To change the access code: `printf 'new-code' | gcloud secrets versions add APP_ACCESS_CODE --data-file=-`, then redeploy.
