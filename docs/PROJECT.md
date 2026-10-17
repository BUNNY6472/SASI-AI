# SASI: the whole project in one page

> Historical deployment snapshot. For the current implementation and its limitations, use [README](../README.md) and [quality and release notes](QUALITY_AND_RELEASE.md). The changes on `codex/sasi-quality-workspace` have not been deployed by this document; model review is not an executed test or proof of correctness.

SASI is a web app where several AI models work on one question together. They each answer on their own,
score each other's approaches, agree on a plan, split the work, review each other, and deliver one reviewed
answer. It runs on Google Cloud in the project **sasi-511906**.

*As of 17 October 2026. Code: branch `claude/sasi-multi-ai-app-c549ms`, latest change `20686af`.*

**Contents**

1. [All the links](#1-all-the-links)
2. [Where it is deployed](#2-where-it-is-deployed)
3. [How a run works](#3-how-a-run-works)
4. [High-level architecture](#4-high-level-architecture)
5. [Low-level architecture](#5-low-level-architecture)
6. [How code gets to the live app](#6-how-code-gets-to-the-live-app)
7. [Settings](#7-settings)
8. [Google Cloud resources](#8-google-cloud-resources)
9. [Costs and the free trial](#9-costs-and-the-free-trial)
10. [What is done](#10-what-is-done)
11. [What is still open](#11-what-is-still-open)
12. [Everyday commands](#12-everyday-commands)

---

## 1. All the links

### The app

| What | Link |
|---|---|
| Live app | https://sasi-498137486060.us-central1.run.app |
| Health check (no login) | https://sasi-498137486060.us-central1.run.app/api/health |

The live link is worked out from the project number (498137486060) in Cloud Run's standard address format.
Cloud Run also gives every service a second, older-style address. Both open the same app. To print the
address Cloud Run itself reports, run this in Cloud Shell:

```bash
gcloud run services describe sasi --region us-central1 --project sasi-511906 --format='value(status.url)'
```

To open the app you need the access code:

```bash
gcloud secrets versions access latest --secret APP_ACCESS_CODE --project sasi-511906
```

### GitHub

| What | Link |
|---|---|
| Repository | https://github.com/BUNNY6472/SASI-AI |
| Pull request #1 (all the work so far, not merged yet) | https://github.com/BUNNY6472/SASI-AI/pull/1 |
| Working branch | https://github.com/BUNNY6472/SASI-AI/tree/claude/sasi-multi-ai-app-c549ms |
| Actions (CI and deploy runs) | https://github.com/BUNNY6472/SASI-AI/actions |
| Deploy variables | https://github.com/BUNNY6472/SASI-AI/settings/variables/actions |
| Make the repo private (Danger Zone, at the bottom) | https://github.com/BUNNY6472/SASI-AI/settings |

### Google Cloud console (project sasi-511906)

| What | Link |
|---|---|
| Cloud Run service `sasi` | https://console.cloud.google.com/run/detail/us-central1/sasi/metrics?project=sasi-511906 |
| Cloud Run logs | https://console.cloud.google.com/run/detail/us-central1/sasi/logs?project=sasi-511906 |
| Cloud Run revisions (each deploy) | https://console.cloud.google.com/run/detail/us-central1/sasi/revisions?project=sasi-511906 |
| Cloud Build history | https://console.cloud.google.com/cloud-build/builds;region=us-central1?project=sasi-511906 |
| Artifact Registry (app images) | https://console.cloud.google.com/artifacts/docker/sasi-511906/us-central1/sasi?project=sasi-511906 |
| BigQuery (open dataset `sasi_data`) | https://console.cloud.google.com/bigquery?project=sasi-511906 |
| Secret Manager | https://console.cloud.google.com/security/secret-manager?project=sasi-511906 |
| Vertex AI Model Garden | https://console.cloud.google.com/vertex-ai/model-garden?project=sasi-511906 |
| GLM 5.2 (API Service card) | https://console.cloud.google.com/vertex-ai/publishers/zai-org/model-garden/glm-5.2-maas?project=sasi-511906 |
| DeepSeek V4.1 Flash (API Service card) | https://console.cloud.google.com/vertex-ai/publishers/deepseek-ai/model-garden/deepseek-v4.1-flash-maas?project=sasi-511906 |
| Quotas (search "Vertex AI") | https://console.cloud.google.com/iam-admin/quotas?project=sasi-511906 |
| IAM (who can do what) | https://console.cloud.google.com/iam-admin/iam?project=sasi-511906 |
| Service accounts | https://console.cloud.google.com/iam-admin/serviceaccounts?project=sasi-511906 |
| Workload Identity pools (GitHub login) | https://console.cloud.google.com/iam-admin/workload-identity-pools?project=sasi-511906 |
| Build source bucket | https://console.cloud.google.com/storage/browser/sasi-511906-sasi-build?project=sasi-511906 |
| Enabled APIs | https://console.cloud.google.com/apis/dashboard?project=sasi-511906 |
| Logs Explorer | https://console.cloud.google.com/logs/query?project=sasi-511906 |
| Billing (credit left, costs) | https://console.cloud.google.com/billing?project=sasi-511906 |
| Cloud Shell | https://shell.cloud.google.com/?show=terminal |

For Claude, open Model Garden and search "Claude". Use the **API Service** cards in Model Garden, not
**Deploy**. Deploy rents a GPU server, which costs money every hour.

### Files in this folder

The project's shared `app-preview` folder holds this document, `SETUP.md` (the setup steps), and screenshots
of the app. `sasi-setup/enable-open-models.ps1` turns on and checks the open models from PowerShell.

---

## 2. Where it is deployed

Everything runs in one Google Cloud project, **sasi-511906** (project number 498137486060), region
**us-central1**.

| Part | Where | Details |
|---|---|---|
| The app (web page and server together) | Cloud Run service `sasi` | 1 container, 1 CPU, 1 GiB memory, 0 to 1 instances (scales to zero when idle), up to 80 requests at once, 60-minute request timeout |
| App image | Artifact Registry repo `sasi` | `us-central1-docker.pkg.dev/sasi-511906/sasi/sasi`. Keeps only the 2 newest images |
| Build | Cloud Build, as `sasi-builder` | Uses `deploy/cloudbuild.yaml`. Uploaded sources go to `gs://sasi-511906-sasi-build` and are deleted after 1 day |
| Run history and files | BigQuery dataset `sasi_data` | Tables `run_checkpoints`, `run_outputs`, `run_files` |
| Secrets | Secret Manager | `APP_ACCESS_CODE`, `SESSION_SECRET`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` |
| AI models | Vertex AI | Claude, Gemini, GLM 5.2, DeepSeek V4.1 Flash (Qwen and Kimi as spares) |
| Code | GitHub `BUNNY6472/SASI-AI` | CI checks every push. Pushing to `main` deploys automatically |

The app runs as the service account `sasi-runtime`, so it reaches BigQuery, Secret Manager and Vertex AI
with no key files.

---

## 3. How a run works

What you do: open the app, enter the access code, type a question (and attach files if you like), press
**Start collaboration**.

```
  Your question + files
          │
          ▼
  1. Pick the team ........ field (software, research, Project Eris, general) and input size decide it:
          │                  Claude + Gemini + the 2 best open models (GLM, DeepSeek; Qwen, Kimi are spares)
          ▼
  2. Independent answers .. every model answers on its own, in parallel; you watch the text arrive live
          │
          ▼
  3. Cross-checks ......... each model reads the others' answers and scores every approach 0 to 10
          │
          ▼
  4. Decide ............... the approach with the best average score from the OTHER models wins
          │
          ▼
  5. Plan ................. goal, assumptions, deliverables, success criteria, up to 6 tasks with dependencies
          │
          ▼
  6. Your approval ........ optional; the clock pauses while the plan waits for you
          │
          ▼
  7. Tasks ................ run in parallel once the tasks they depend on are done
          │                  each one: a model writes it → a DIFFERENT model reviews it
          │                  → revise (up to 2 times) or hand it to another model
          ▼
  8. Integrate ............ one model writes the single final answer, built on the winning approach
          │
          ▼
  9. Test and fix ......... the other models score it and list must-fix problems; it is fixed (up to 2 rounds)
          │
          ▼
 10. Deliver .............. final answer, score, gaps, limitations, who did what, tokens used;
                            a "Download solution.py" button when the answer contains the code
```

Rules that keep a run on track:

- **Time is the only limit.** A run gets 60 minutes. The last 5 minutes are a wrap-up: no new work starts
  and SASI delivers the best complete answer from what is done. Answers get 20% of the time, cross-checks
  10%, and 25% is kept for writing and testing the final answer. No token or call limits.
- **Runs go ahead with whatever works.** If a model fails, its seat goes to a spare, or the run carries on
  with the models that work (one is enough).
- **Nothing is lost.** Accepted work is saved at every step, so a stopped run can be resumed, and you can
  delete a run with everything it stored.
- **Project Eris switch.** Every model follows the Eris Solver Guidebook, only dry runs are allowed, and the
  result is one `solution.py`.

---

## 4. High-level architecture

```
                       You, in a browser (computer or phone)
                                     │
                                     │ HTTPS · access code → signed login cookie (7 days)
                                     ▼
┌──────────────────────── Google Cloud project sasi-511906 (us-central1) ────────────────────────┐
│                                                                                                 │
│   Cloud Run service "sasi"   (one container, runs as sasi-runtime)                              │
│   ┌──────────────────────────────────────────────────────────────────────────────────────┐    │
│   │  Web app: Next.js pages exported as static files, 3D background in three.js           │    │
│   │                                                                                        │    │
│   │  Python server (FastAPI)                                                               │    │
│   │    ├── Login gate and API  (/api/...)        ├── Live updates (Server-Sent Events)    │    │
│   │    ├── Workflow controller (runs a run)      ├── Files: convert anything to text      │    │
│   │    └── Model gateway: team, routing, fitting each request to the model's context       │    │
│   └───────────┬────────────────────────────┬─────────────────────────────┬────────────────┘    │
│               │                            │                             │                     │
│               ▼                            ▼                             ▼                     │
│   ┌──────────────────────┐   ┌──────────────────────────┐   ┌──────────────────────────┐      │
│   │ Vertex AI            │   │ BigQuery  sasi_data      │   │ Secret Manager           │      │
│   │  Claude              │   │  run_checkpoints         │   │  APP_ACCESS_CODE         │      │
│   │  Gemini              │   │  run_outputs             │   │  SESSION_SECRET          │      │
│   │  GLM 5.2             │   │  run_files               │   │  ANTHROPIC_API_KEY       │      │
│   │  DeepSeek V4.1 Flash │   └──────────────────────────┘   │  GEMINI_API_KEY          │      │
│   │  (Qwen, Kimi spares) │                                  └──────────────────────────┘      │
│   └──────────────────────┘                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
                │
                └── second route, only if Vertex AI fails: Anthropic API or Gemini API with a key
                    saved on the Connections page
```

---

## 5. Low-level architecture

### 5.1 Code layout

```
SASI-AI/
├── Dockerfile                  two stages: build the web app, then the Python server that also serves it
├── backend/
│   ├── app/
│   │   ├── main.py             FastAPI app: login gate, all /api routes, live updates, serves the web app
│   │   ├── config.py           every setting, read from environment variables
│   │   ├── models.py           data shapes: Run, Plan, Task, Review, Opinion, FinalResult, ...
│   │   ├── files.py            attached files: size limits, which model reads which kind directly
│   │   ├── extract.py          turns Parquet, Excel, Word, PowerPoint, JSON, ZIP, ... into text
│   │   ├── keys.py             API keys in Secret Manager (or env vars locally); only the last 4 shown
│   │   ├── codefile.py         finds the code in the final answer, for "Download solution.py"
│   │   ├── gateway/            talking to the models
│   │   │   ├── catalog.py      the model seats: names, context sizes, strengths, model IDs, locations
│   │   │   ├── registry.py     builds the team, checks which models work and why not
│   │   │   ├── routes.py       Vertex AI first, the API key second
│   │   │   ├── vertex.py       Claude, Gemini and open models on Vertex AI
│   │   │   ├── anthropic_adapter.py, gemini_adapter.py   the API-key routes
│   │   │   ├── fit.py          trims a request to fit a model's context window
│   │   │   ├── base.py         the shared request/response shapes and errors
│   │   │   └── simulated.py    a fake model for tests
│   │   ├── workflow/           running a run
│   │   │   ├── controller.py   the run itself (the 10 steps above), time limit, saving, resuming
│   │   │   ├── router.py       who does each task and who reviews it, learned from past results
│   │   │   ├── profiles.py     fields: software, research, Project Eris, general
│   │   │   ├── prompts.py      the instructions each step sends
│   │   │   ├── eris.py + eris_guidebook.md   Project Eris rules
│   │   └── store/              where runs are saved: bigquery.py (live app), memory.py (local)
│   └── tests/                  47 automatic tests
├── frontend/
│   ├── app/                    page.tsx (the one page), layout.tsx, globals.css
│   ├── components/             Login, Workspace, RunView, Connections, Scene3D, Markdown, ui
│   └── lib/api.ts              calls to the server and the data types
├── deploy/
│   ├── setup-gcp.sh            one-time Google Cloud setup (safe to repeat)
│   ├── deploy-now.sh           build on Cloud Build and deploy to Cloud Run, prints the link
│   ├── cloudbuild.yaml         the build recipe
│   ├── add-api-key.sh          save a model API key from Cloud Shell
│   └── check-open-models.ps1   check from PowerShell that GLM and DeepSeek answer
├── .github/workflows/
│   ├── ci.yml                  tests, web build, image build + smoke test on every push
│   └── deploy.yml              deploys main to Cloud Run
└── docs/                       SETUP.md, PROJECT.md (this file)
```

### 5.2 The screens

| Screen | Component | What it does |
|---|---|---|
| Login | `Login.tsx` | Asks for the access code |
| Workspace | `Workspace.tsx` | Question box, file attach (drag and drop), Project Eris switch, plan approval and compare options, the team chips (a model that is not working shows "not working" with the reason underneath), past runs |
| Run | `RunView.tsx` | The question as a short header (Show full question), live text from each model, scores, plan, tasks with reviews, final answer, Download solution.py, Copy code, Copy all, approve / cancel / resume / delete |
| Connections | `Connections.tsx` | Which models work and through which route, API keys (add, replace, remove, remove all), Check again |
| Background | `Scene3D.tsx` | The 3D scene behind the pages |

### 5.3 The server API

All `/api` routes except health, session, login and logout need the login cookie.

| Method and path | What it does |
|---|---|
| `GET /api/health` | Is the server up |
| `GET /api/session`, `POST /api/login`, `POST /api/logout` | Login state, log in with the access code, log out |
| `GET /api/providers` | Each model: working or not, route, reason |
| `GET /api/keys`, `PUT /api/keys/{provider}`, `DELETE /api/keys/{provider}`, `DELETE /api/keys` | API keys (only the last 4 characters come back) |
| `GET /api/profiles`, `GET /api/limits`, `GET /api/stats` | Fields, the time limit and other limits, usage totals |
| `POST /api/files`, `DELETE /api/files/{file_id}` | Attach and remove files before a run starts |
| `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{id}`, `DELETE /api/runs/{id}` | Start, list, read and delete runs |
| `POST /api/runs/{id}/approve`, `/cancel`, `/resume` | Approve the plan, stop, carry on |
| `GET /api/runs/{id}/events` | Live updates while a run works (Server-Sent Events) |
| `GET /api/runs/{id}/solution` | Download the final code as `solution.py` |
| `GET /api/runs/{id}/files/{file_id}` | Download a file attached to a run |
| everything else | The web app's pages |

### 5.4 What is stored

A **Run** holds: the question, field, Project Eris on or off, files, the team, each model's independent
answer, the cross-checks and scores, the winning approach, the plan and its tasks (each with attempts and
reviews), the final result (answer, integrator, judges' scores, gaps, limitations, fix rounds), the event
log, token usage per model, and timing.

BigQuery dataset `sasi_data`, all tables split by day (`created_at`) and grouped by `run_id`:

| Table | One row per | Used for |
|---|---|---|
| `run_checkpoints` | saved step of a run | the run's current state (latest row), resuming after a restart |
| `run_outputs` | model output (answer, cross-check, task, review, final) | comparing the models' work |
| `run_files` | chunk of an attached file | every model reads the same files; deleted with the run |

Rows are written with load jobs, which are free and can be deleted right away, so **Delete** removes a run
completely.

### 5.5 Models, team and routing

| Seat | Kind | Context | Reads | Vertex model IDs, in order tried | Locations |
|---|---|---|---|---|---|
| Claude | frontier | 1M | images, PDF, text | set by `CLAUDE_MODEL` / `VERTEX_CLAUDE_MODEL` | global |
| Gemini | frontier | 1M | images, PDF, text, video, audio | set by `VERTEX_GEMINI_MODEL` | global |
| GLM | open | 1M | text | `zai-org/glm-5.2-maas`, `glm-5-maas`, `glm-4.7-maas` | global, us-central1 |
| DeepSeek | open | 1M | images, text | `deepseek-ai/deepseek-v4.1-flash-maas`, `v3.2-maas`, `v3.1-maas` | global, us-central1 |
| Qwen (spare) | open | 262K | text | `qwen/qwen3-coder-480b-a35b-instruct-maas`, `qwen3-235b-...` | global, us-south1 |
| Kimi (spare) | open | 262K | text | `moonshotai/kimi-k2-thinking-maas` | global |

The team is the two frontier models plus the best open models for the field and input size, 4 in all
(`TEAM_SIZE`). A model that fails is replaced by the next one that works.

How one model call travels:

```
 controller ──► ModelCall (instructions, prompt, files, time left)
                    │
                    ▼
               fit.py: measure (≈3 characters per token); if too long for this model,
                       trim the largest file texts first, then the middle of the prompt
                    │
                    ▼
               RoutedAdapter for the seat
                    ├── 1st route: Vertex AI as sasi-runtime (no keys)
                    │      Claude   → Anthropic Vertex client, always streaming
                    │      Gemini   → Google Gen AI SDK in Vertex mode
                    │      GLM, DeepSeek, Qwen, Kimi
                    │               → OpenAI-compatible endpoint
                    │                 https://{location}-aiplatform.googleapis.com/v1/projects/sasi-511906/
                    │                   locations/{location}/endpoints/openapi/chat/completions
                    │                 tries each model ID in each location until one answers;
                    │                 DeepSeek gets images inline, and text only if images are refused
                    │
                    └── 2nd route: the provider's own API key from Secret Manager (Claude and Gemini only)
                    │
                    ▼
               ModelResult (text, tokens) ──► saved and shown live; an empty answer is asked again with more room,
                                and a "too long" error is trimmed to the size the model states and retried
```

A route that fails for a lasting reason (no permission, not turned on, no credit) is skipped until the next
check, and the Workspace and Connections pages show the reason Google gave.

### 5.6 Files

Any file type can be attached. Images, PDFs, video and audio go straight to the models that can open them
(Claude, Gemini, and images for DeepSeek). Data and documents (CSV, JSON, Parquet, Excel, Word, PowerPoint,
ZIP, code, text) are turned into text with a profile of the whole file, so a model understands all of it
even when only the start fits. Other binary files are kept and described.

### 5.7 Security

- **Access code.** Every API call needs a login cookie, which you get by entering the access code. The cookie
  is signed with `SESSION_SECRET`, lasts 7 days, and is HTTP-only and same-site. Cloud Run lets anyone reach
  the address, but nothing works without the code.
- **API keys stay on the server.** Keys are typed on the Connections page once, saved in Secret Manager, and
  never sent back to the browser or written to logs. Only the last 4 characters are shown.
- **No key files anywhere.** The app uses its service account. GitHub deploys with Workload Identity
  Federation, limited to this repository (ID 1057223359, owner ID 142133765).
- **Downloads are sandboxed** (a strict Content-Security-Policy), and the container runs as a non-root user.

---

## 6. How code gets to the live app

```
   Route A: from Cloud Shell (used so far)              Route B: from GitHub (after PR #1 is merged)
   ─────────────────────────────────────────            ─────────────────────────────────────────────
   cd ~/SASI-AI && git pull                             push or merge to main
   bash deploy/deploy-now.sh                                      │
          │                                                       ▼
          ▼                                             GitHub Actions "Deploy to Cloud Run"
   setup-gcp.sh (safe to repeat)                        signs in without keys (Workload Identity)
          │                                             as sasi-deployer
          ▼                                                       │
   Cloud Build as sasi-builder                                    ▼
   builds the Dockerfile                                docker build + push
          │                                                       │
          └──────────────► Artifact Registry ◄────────────────────┘
                 us-central1-docker.pkg.dev/sasi-511906/sasi/sasi
                 (tags: build-<date time> from Cloud Shell, the commit ID from GitHub)
                                   │
                                   ▼
                     gcloud run deploy sasi → new revision gets all traffic
```

The image is built in two stages: Node 22 builds the web pages into static files, then a Python 3.13 image
gets the server plus those files.

**Checks on every push** (`ci.yml`): backend tests (47 passing), web build, and building the image and
starting it to check `/api/health` and the home page answer. All three passed on the latest change.

---

## 7. Settings

Set on Cloud Run by the deploy (`--set-env-vars` and `--set-secrets`):

| Setting | Live value | Meaning |
|---|---|---|
| `STORAGE_BACKEND` | `bigquery` | Save runs in BigQuery (locally: `memory`) |
| `KEY_STORE` | `secretmanager` | Keep API keys in Secret Manager (locally: `env`) |
| `GCP_PROJECT` | `sasi-511906` | The project |
| `BIGQUERY_DATASET` | `sasi_data` | The dataset |
| `VERTEX_AI` | `true` | Reach models through Vertex AI first |
| `APP_ACCESS_CODE` | from Secret Manager | The code you type to open the app |
| `SESSION_SECRET` | from Secret Manager | Signs the login cookie |

Defaults you can change with `EXTRA_ENV="NAME=value" bash deploy/deploy-now.sh`:

| Setting | Default | Meaning |
|---|---|---|
| `RUN_TIME_LIMIT_MINUTES` | 60 | A run's time limit |
| `WRAP_UP_MINUTES` | 5 | The wrap-up at the end |
| `TEAM_SIZE` | 4 | Models on a run |
| `OPEN_MODELS` | `glm,deepseek,qwen,kimi` | Open models allowed, best first |
| `VERTEX_LOCATION` | `global` | Where Claude and Gemini are called |
| `VERTEX_CLAUDE_MODEL`, `VERTEX_GEMINI_MODEL` | the newest models | Pick a different Claude or Gemini |
| `MAX_TASKS_PER_PLAN` | 6 | Tasks in a plan |
| `MAX_REVISIONS_PER_TASK` | 2 | Review-and-revise rounds per task |
| `MAX_FINAL_FIX_ROUNDS` | 2 | Test-and-fix rounds on the final answer |
| `MAX_CONCURRENT_RUNS` | 3 | Runs working at the same time |

---

## 8. Google Cloud resources

| Service account | What it is for | Roles |
|---|---|---|
| `sasi-runtime` | the running app | BigQuery Data Editor, BigQuery Job User, Secret Manager Secret Accessor, Vertex AI User; Secret Version Manager and Viewer on the two API-key secrets only |
| `sasi-deployer` | GitHub Actions deploys | Cloud Run Admin, Artifact Registry Writer, Secret Manager Viewer, may act as `sasi-runtime`, signed in from GitHub through Workload Identity |
| `sasi-builder` | Cloud Build | Artifact Registry Writer, Logs Writer, Storage Object Viewer |

Workload Identity: pool `github`, provider `sasi-ai`, trusting GitHub's token issuer only for this repository.
GitHub repository variables: `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_DEPLOY_SERVICE_ACCOUNT`, `GCP_PROJECT_ID`,
`GCP_REGION`.

APIs turned on: Cloud Run, Artifact Registry, Secret Manager, IAM, IAM Credentials, Security Token Service,
BigQuery, Cloud Build, Cloud Storage, Cloud Logging, Vertex AI.

---

## 9. Costs and the free trial

The project is on the Google Cloud free trial (₹20,000 credit).

| Part | Cost |
|---|---|
| Cloud Run | Scales to zero when nobody uses it, at most 1 instance. Light use stays inside the free tier |
| Artifact Registry | Keeps 2 images, under the 0.5 GB free tier |
| Build bucket | Sources deleted after 1 day |
| BigQuery | Load jobs are free; storage of run records is small |
| Secret Manager | 4 secrets, around the free tier (6 secret versions free) |
| **Vertex AI models** | **The real cost.** Billed per token from the credit. A long run with four models reading big files costs the most |

Things that are likely on a free trial (from Google's rules, not yet confirmed for this project):

- **Claude through Vertex AI** usually needs a paid billing account (upgrade from the trial; the remaining
  credit is kept).
- **Open models** (GLM, DeepSeek) can have a quota of 0 on trial projects until billing is upgraded or a quota
  increase is approved.

---

## 10. What is done

All on 17 October 2026 (times in UTC), all in pull request #1.

| Time | What changed |
|---|---|
| 07:08 | First SASI app: models answering together, live 3D web app, one Cloud Run container |
| 07:12 to 08:18 | One-command deploy from Cloud Shell, building on Cloud Build, retries and re-login when Cloud Shell's login expires |
| 07:52 | Real mode: models are called for real, API keys are added from the app, no demo answers |
| 08:08 | File attachments, live side-by-side answers, BigQuery record, Delete removes a run completely, one box for every kind of question |
| 08:14 | Any file type: Parquet, Excel, Word, PowerPoint, JSON, ZIP and more |
| 08:32 | Keys work right away, and a failed save says exactly why |
| 09:00 | Project Eris switch: guidebook rules for every model, dry runs only |
| 09:09 | One best answer: models score each other's approaches, build one answer, then test and fix it |
| 09:14 | Removing keys always works, including all at once |
| 09:16 | Runs go ahead with whichever models work, even just one |
| 10:12 | All models reached through Vertex AI with no keys; API keys became the second route |
| 11:21 | Team of frontier and open models, the 60-minute limit with a wrap-up, no more empty answers |
| 11:43 | GLM 5.2 reads 1M tokens, DeepSeek V4.1 Flash reads images, PowerShell check for the open models |
| 13:00 | Download the final code as `solution.py`; the question shows as a short header with Show full question |
| 13:27 | Open models try every model ID and location before giving up; the Workspace shows why a model is not working |
| 14:30 | This document |

The app is live and has completed real runs, including a Project Eris run.

---

## 11. What is still open

| Item | What is needed |
|---|---|
| GLM and DeepSeek show errors | The exact message from the Workspace or Connections page (after the latest redeploy) says which it is: quota, billing, or the model card. Most likely the free trial |
| Claude through Vertex AI | Likely needs the billing account upgraded from the free trial. Until then Claude can use an Anthropic API key from the Connections page |
| Make the repository private | GitHub settings, Danger Zone, Change visibility. After that, run `gh auth login` once in Cloud Shell so `git pull` keeps working |
| Pull request #1 | Not merged. Merging it into `main` turns on automatic deploys from GitHub |
| More upgrades | Next list from Sasi |

---

## 12. Everyday commands

Run these in Cloud Shell.

```bash
# Get the newest code and redeploy (about 5 minutes; prints the link and the access code)
cd ~/SASI-AI && git pull && bash deploy/deploy-now.sh

# Skip the setup step on later deploys
SKIP_SETUP=1 bash deploy/deploy-now.sh

# The live address and the access code
gcloud run services describe sasi --region us-central1 --project sasi-511906 --format='value(status.url)'
gcloud secrets versions access latest --secret APP_ACCESS_CODE --project sasi-511906

# The app's recent logs
gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name=sasi' \
  --project sasi-511906 --limit 50 --format='value(timestamp,textPayload)'

# Once the repo is private: sign in to GitHub once (GitHub.com, HTTPS, Yes, log in with a browser)
gh auth login
```

From PowerShell on Windows, check that GLM and DeepSeek answer (costs a fraction of a cent):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\check-open-models.ps1 -Login
```

Step-by-step setup from nothing is in `SETUP.md`.
