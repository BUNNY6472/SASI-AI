# SASI

Claude, Gemini and the best open models working as one team. You ask once, in one box, and attach any files (images, video, audio, PDFs, CSV, JSON, Parquet, Excel, Word, ZIP, code and more). Each run gets a team of up to four models: the frontier models Claude and Gemini first, then the open models on Google Cloud that suit the task best. Every model on the team analyses the request on its own at the same time, they read and check each other's analyses, then SASI plans the work, splits it across the models, has each piece checked by a different model, and delivers one complete result within the run's time limit (one hour by default).

This release builds the design in the *Multi-Model Collaboration App* architecture document. SASI picks the field (software engineering, research and analysis, or general) from the request itself, and the planner confirms or corrects it.

## How a request moves through SASI

| Stage | What happens | Gate before moving on |
|---|---|---|
| 0. Team and clock | SASI picks the team (working frontier models first, then the open models whose context window holds the input and whose strengths fit the task) and starts the run's clock | At least one model works |
| 0a. Independent analysis | Every model on the team studies the request and attached files on its own, in parallel. Text appears live as each model writes | At least one analysis succeeded; after 20% of the time limit SASI stops waiting for slower models |
| 0b. Cross-check and team decision | Each model reads the others' analyses, lists agreements, disagreements, mistakes and edge cases, and scores every approach 0-10. The approach with the best average score from the *other* models becomes the base for one answer | Runs when two or more analyses exist |
| 1. Understand + plan | One model turns the request into a goal, assumptions, success criteria and a task graph, building on the chosen approach | Plan is readable; falls back to a single task if not |
| 2. Plan review | A *different* model reviews the plan; the planner revises once if asked | Reviewer accepts, or one revision is done |
| 3. Your approval | You see the plan and approve it (optional switch) | You click Approve |
| 4. Parallel work | Tasks with no unmet dependency run at the same time on different models, each with only its own context. Task work stops early enough to leave a quarter of the time for the final result | Required inputs were accepted |
| 5. Cross-review | Each output is reviewed by another model: pass, revise or block. One revision, then one reassignment to another model | Pass, else the task is marked "needs your review" and dependants are blocked |
| 6. One result | One model merges the accepted work into a single answer, never a set of alternatives | Done |
| 7. Test, fix and score | Every other model tests that one result against the criteria and edge cases and scores it. If any finds something that must be fixed, the integrator fixes it once and they test again. The final score is their average | The other models' re-test |
| 8. Deliver | One result with its score, who built and tested it, what was fixed, gaps, limitations, time taken and token usage | |
| Wrap-up | In the last 5 minutes of the time limit SASI stops new work. If the final result exists it is delivered as it stands; if not, a model writes the complete result (complete code, no placeholders) from the reviewed work, drafts and analyses | Always ends with a result |

Rules that are enforced by code, not by model opinion:

- Provider keys stay on the server (Secret Manager on Cloud Run). The browser never sees them.
- A model never reviews its own work while another provider is available. If it has to, the result is labelled *self-reviewed*.
- Every accepted step records its assurance level. This release only does model review, so results are *evidence-reviewed*, never "tested" or "verified".
- Time is the limit, not tokens or calls: 60 minutes per run (`RUN_TIME_LIMIT_MINUTES`), with the last 5 (`WRAP_UP_MINUTES`) kept for delivering the complete result. Every model call is bounded by the time left. The clock pauses while a plan waits for your approval. Calls and tokens have no cap by default (`MAX_MODEL_CALLS_PER_RUN`, `MAX_TOKENS_PER_RUN`; 0 means no cap).
- Every request is fitted to the model it goes to. Inputs are measured, and when they are too long for a model's context window the largest file texts are trimmed (keeping their start and end, with a note), then the shared material. If a model still says the input is too long, SASI remembers the limit it states, trims again and retries. Models with large windows always get everything.
- A model never shows an empty answer. A reply with no text (for example when a thinking model spends its whole allowance thinking) is an error: SASI asks once more with more room, and if it fails again the panel shows the error.
- Runs go ahead with whichever models work. A model with nothing connected or a failing route is skipped and the run says so. A model that fails mid-run is retried once for temporary errors, then skipped for the rest of that run; its work goes to another model and the next best working model takes its seat on the team. A model that fails for a lasting reason is skipped in later runs until it passes a check again, which happens before each run.
- Pasted reference material is passed to models as untrusted data, never as instructions.
- Model output is sanitized before it is shown in the browser.

## Files and the project record

Any file type can be attached from the composer (button or drag and drop), up to 10 per run and 25 MB each.

- Images and PDFs go to Claude and Gemini directly. Video and audio go to Gemini. DeepSeek V4.1 Flash also reads images. The other open models read text, so for media they work from the frontier models' analyses.
- Data and documents are converted to text in `backend/app/extract.py`:
  - CSV, TSV, JSON, JSON Lines and Parquet.
  - Excel (`.xlsx`) and Word (`.docx`).
  - PowerPoint (`.pptx`) and Jupyter notebooks.
  - ZIP and gzip, including the files inside them.
  - Code, text, markdown, SVG and HTML.
- Tables come with a profile computed from the whole file: rows, columns, and per column the type, nulls, min, max, mean and distinct values.
- Other binary files are kept with the run, and the models are told they exist.

On Cloud Run everything for a run is kept in BigQuery (dataset `sasi_data`): `run_checkpoints` (run state), `run_outputs` (every model's analysis, cross-check, task work, review and final result, ready to compare with SQL) and `run_files` (the files, in chunks). **Delete** on a finished run removes it from all three tables.

## Project Eris mode

A separate switch in the composer, for Project Eris ML challenges. When it is on:

- Every model, on every step, gets the full Eris Solver Guidebook (`backend/app/workflow/eris_guidebook.md`) as rules. The text is sent as a repeated prefix, so the providers cache it.
- The models are told never to run, or ask for, the full end-to-end training. They deliver one clean `solution.py` with no dry-run flags or switches (the determinism check rejects those), plus separate dry-run steps for a copy of the script.
- The side-by-side comparison adds an "Eris rule check" table: every model's answer against each rule (real training in the script, no hardcoding, one end-to-end script, data, libraries, test-set use, determinism, 1.5 hours on an A10G, a complete submission.csv, domain rules).
- Reviews send back any work that fails a rule, and the final result ends with the rule check, the dry-run steps and the expected runtime.
- Eris answers may be up to 32,000 tokens per call (plus the same again for thinking), because they carry the guidebook and whole training scripts.

SASI never executes code, so nothing is ever run end to end by the app itself.

## Google Cloud Vertex AI (no keys needed)

On Cloud Run, SASI reaches every model through Vertex AI, signed in as the app's own service account (`sasi-runtime`, role `roles/aiplatform.user`). No API keys are needed, and the usage is billed to the Google Cloud project. `deploy/setup-gcp.sh` turns on the Vertex AI API and grants the role; the deploy sets `VERTEX_AI=true`.

| Seat | Kind | Model IDs tried, newest first | Context | Notes |
|---|---|---|---|---|
| Claude | frontier | `claude-opus-5-5`, `claude-opus-5` | 1M | Enable Claude once in Vertex AI Model Garden. A Claude API key is the backup route |
| Gemini | frontier | `gemini-4-argon` | 1M | Files go inline up to 14 MB per request (Vertex has no Files API). A Gemini API key is the backup route |
| GLM | open | `zai-org/glm-5.2-maas`, `glm-5-maas`, `glm-4.7-maas` | 1M | Strong at code and long agentic work |
| DeepSeek | open | `deepseek-ai/deepseek-v4.1-flash-maas`, `v3.2-maas`, `v3.1-maas` | 1M | Takes very large inputs whole, and reads images |
| Qwen | open | `qwen/qwen3-coder-480b-a35b-instruct-maas`, `qwen3-235b-a22b-instruct-2507-maas` | 256K | Coding |
| Kimi | open | `moonshotai/kimi-k2-thinking-maas` | 256K | Reasoning |

The open models are reached through Vertex AI's OpenAI-compatible Chat Completions endpoint. DeepSeek V4.1 Flash gets images inline (if the endpoint refuses them, it gets text from then on); otherwise images, PDFs, video and audio reach the open models through the frontier models' analyses. Each is tried in a few locations, and the first model ID this project can call is used. Use each model's **API Service** card in [Model Garden](https://console.cloud.google.com/vertex-ai/model-garden), not the card with **Deploy** (that one runs the model on your own paid GPU server). Press **Enable** if the API Service card shows it; GLM 5.2 and DeepSeek V4.1 Flash need no enable step. A model this project can't call shows "not working" on Connections and is skipped.

The team for a run is the working frontier models, then open models ranked by whether the input fits their context window and whether their strengths suit the work (code for software and Eris runs, reasoning for research and general runs), up to `TEAM_SIZE` (4). Change the open models with `OPEN_MODELS` (default `glm,deepseek,qwen,kimi`; `minimax` and `gptoss` are also known; in `EXTRA_ENV` separate them with `+`, such as `OPEN_MODELS=glm+kimi`), and the frontier models with `VERTEX_CLAUDE_MODEL`, `VERTEX_GEMINI_MODEL` and `VERTEX_LOCATION`, for example `EXTRA_ENV="TEAM_SIZE=3" bash deploy/deploy-now.sh`.

Claude and Gemini go through Vertex first and use their API key, if one is saved, as the backup. If a route fails before the model has written anything, the other route takes over that call; a route that fails for a lasting reason (no permission, not enabled, no credit) is skipped until the next check.

**What is free.** Nothing on Vertex AI is free: every call is billed to the project, and free-trial credit pays for it only where Google allows (it does not cover partner models such as Claude). The only free option is a Gemini API key from Google AI Studio on its free tier, which has rate limits; Google may use free-tier prompts to improve its products.

## API keys

SASI takes API keys for Claude and Gemini only; the open models run through Google Cloud. Keys are pasted on the app's **Connections** page. The backend checks each key against the provider, stores it in Google Secret Manager (or memory when running locally), and starts using it immediately. Keys are never returned to the browser or logged; only the last 4 characters are shown. To change a key, paste the new one and press **Replace key**; the old one is destroyed. **Remove** (or **Remove all keys**) stops SASI using a key at once and writes a removal marker in Secret Manager, which needs the same permission as saving, then destroys the old versions. Without Vertex AI, runs need at least one key. With one model, that model also reviews its own work and results are labelled *self-reviewed*; with two or more, every step is checked by a different model.

Simulated agents exist only for the automated tests (`ALLOW_SIMULATED=true`).

## Code map

```
backend/app/
  main.py                 API, access-code sign-in, live updates (server-sent events), serves the web app
  config.py               settings: team, time limit, optional caps (all from environment)
  models.py               run, plan, task, review, usage records
  gateway/                model access: the model catalog (catalog.py), one adapter per provider, the Vertex AI
                          adapters (vertex.py), fitting inputs to context windows (fit.py), the two routes per
                          model (routes.py), simulated agents and the model registry
  workflow/controller.py  the workflow controller (stages, gates, retries, checkpoints)
  workflow/router.py      chooses who does and who checks each task, from review history, and says why
  workflow/profiles.py    field profiles: software, research, general, and Project Eris
  workflow/eris.py        Project Eris mode: the guidebook rules, the rule list and the dry-run instructions
  workflow/prompts.py     task-scoped prompts
  store/                  project record: in-memory, or BigQuery checkpoints (dataset sasi_data)
frontend/                 Next.js web app with the three.js 3D scene (exported to static files)
Dockerfile                one container: the web app + API
.github/workflows/        CI on every PR; deploy to Cloud Run on main
deploy/                   one-time Google Cloud setup and the API-key helper
```

### Choices made against the architecture's suggested stack

The document calls its stack "replaceable choices". To start inside the free tier with one small service:

- **Project record:** BigQuery (`sasi_data.run_checkpoints`, created automatically) instead of PostgreSQL. Cloud SQL has no free tier. Each meaningful step appends a checkpoint row; an interrupted run can resume from it without redoing accepted work.
- **Workflow:** a small asyncio controller instead of LangGraph plus a job queue. Same stages and checkpoints, fewer moving parts. It runs inside the web service, which is limited to one instance.
- **Models:** the official Anthropic and Google Gen AI SDKs, through Vertex AI on Cloud Run (see above) or with API keys, and the OpenAI SDK only to call open models through Vertex AI's OpenAI-compatible endpoint. With a key, Claude defaults to `claude-opus-5-5` and Gemini picks the newest general model from the account's live model list. Pin them with `CLAUDE_MODEL` and `GEMINI_MODEL`.
- **Sign-in:** a private access code, so only you can use it. Locking it to your Google account (Identity-Aware Proxy) is an easy later step.

Not in this release: file uploads, sandboxed code execution, web search, benchmark feeds, multiple users. These are later phases in the document.

## Run locally

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --reload --port 8080

# web app (in another terminal)
cd frontend
npm ci
npm run dev        # http://localhost:3000, forwards /api to :8080
```

Copy `backend/.env.example` to `backend/.env` to add keys locally.

## Deploy

See [docs/SETUP.md](docs/SETUP.md).
