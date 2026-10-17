# SASI

Claude, ChatGPT and Gemini working as one team. You ask once, in one box, and attach any files (images, video, audio, PDFs, CSV, JSON, Parquet, Excel, Word, ZIP, code and more). All three models analyse it on their own at the same time, read and check each other's analyses, then SASI plans the work, splits it across the models, has each piece checked by a different model, and delivers one combined result that shows where the models agreed and disagreed.

This release builds the design in the *Multi-Model Collaboration App* architecture document. SASI picks the field (software engineering, research and analysis, or general) from the request itself, and the planner confirms or corrects it.

## How a request moves through SASI

| Stage | What happens | Gate before moving on |
|---|---|---|
| 0a. Independent analysis | Every connected model studies the request and attached files on its own, in parallel. Text appears live as each model writes | At least one analysis succeeded |
| 0b. Cross-check | Each model reads the others' analyses and lists agreements, disagreements and mistakes | Runs when two or more analyses exist |
| 1. Understand + plan | One model turns the request into a goal, assumptions, success criteria and a task graph | Plan is readable; falls back to a single task if not |
| 2. Plan review | A *different* model reviews the plan; the planner revises once if asked | Reviewer accepts, or one revision is done |
| 3. Your approval | You see the plan and approve it (optional switch) | You click Approve |
| 4. Parallel work | Tasks with no unmet dependency run at the same time on different models, each with only its own context | Required inputs were accepted |
| 5. Cross-review | Each output is reviewed by another model: pass, revise or block. One revision, then one reassignment to another model | Pass, else the task is marked "needs your review" and dependants are blocked |
| 6. Integrate + evaluate | One model merges the accepted work; a different model scores it against the success criteria | Done |
| 7. Deliver | Result, who did and checked what, gaps, limitations and token usage | |

Rules that are enforced by code, not by model opinion:

- Provider keys stay on the server (Secret Manager on Cloud Run). The browser never sees them.
- A model never reviews its own work while another provider is available. If it has to, the result is labelled *self-reviewed*.
- Every accepted step records its assurance level. This release only does model review, so results are *evidence-reviewed*, never "tested" or "verified".
- Budget guards per run: model calls, total tokens, output tokens per call, tasks per plan, revisions per task, and runs at once. Hitting a limit stops the run and keeps accepted work.
- A provider that fails is retried once for temporary errors, then skipped for the rest of that run, and its work goes to another model.
- Pasted reference material is passed to models as untrusted data, never as instructions.
- Model output is sanitized before it is shown in the browser.

## Files and the project record

Any file type can be attached from the composer (button or drag and drop), up to 10 per run and 25 MB each.

- Images and PDFs go to every model. Video and audio go to Gemini, and the other models work from Gemini's analysis of them.
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
- Eris runs allow up to 1,000,000 tokens and 12,000 output tokens per call, because they carry the guidebook and whole training scripts.

SASI never executes code, so nothing is ever run end to end by the app itself.

## API keys

Keys are pasted on the app's **Connections** page. The backend checks each key against the provider, stores it in Google Secret Manager (or memory when running locally), and starts using it immediately. Keys are never returned to the browser or logged; only the last 4 characters are shown. Runs need at least one key. With one key, that model also reviews its own work and results are labelled *self-reviewed*; with all three, every step is checked by a different model.

Simulated agents exist only for the automated tests (`ALLOW_SIMULATED=true`).

## Code map

```
backend/app/
  main.py                 API, access-code sign-in, live updates (server-sent events), serves the web app
  config.py               settings and budget limits (all from environment)
  models.py               run, plan, task, review, usage records
  gateway/                model access: one adapter per provider + simulated agents + model registry
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
- **Models:** the official Anthropic, OpenAI and Google Gen AI SDKs. Claude defaults to `claude-opus-5-5`; ChatGPT and Gemini pick the newest general model from the account's live model list. Pin any of them with `CLAUDE_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL`.
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
