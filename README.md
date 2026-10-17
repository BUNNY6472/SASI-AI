# SASI

**Sasidhar’s Artificial Super Intelligence** is a research and problem-solving workspace that coordinates existing models, supplied files, public evidence, and independent review. It is an orchestration application, not a newly trained frontier model. Its quality must be measured on real tasks; adding more models does not guarantee a better answer.

## What this version changes

- **Adaptive effort:** Quick, Adaptive and Deep modes select initial analysis breadth and task limits. Measured call durations update the time reserved for synthesis and review. Complex runs can add one bounded investigation when reviews expose unresolved gaps. Claude reasoning effort also varies by task, mode and remaining time.
- **Evidence before synthesis:** explicit HTTPS links are fetched with size/time limits and SSRF protection. Research/current-information requests can use the existing Gemini connection with Google Search grounding. Sources, retrieval times, search queries and unavailable evidence are visible.
- **Honest quality labels:** invalid review JSON cannot pass; `"false"` is not a boolean verdict; incomplete model responses are rejected. Each review is bound to an answer version and SHA-256. Editing an answer invalidates its old score.
- **Deterministic checks:** Python syntax is parsed without executing it. Citation coverage checks whether links occur in fetched evidence or provider search metadata. These checks can trigger a repair; they do not prove functional correctness or claim truth.
- **A focused interface:** responsive navigation, a readable composer, selectable effort and time, upload progress, live work, final result, sources/checks, and retained run history. The decorative 3D background is removed from the active interface.
- **Operational controls:** durable SQLite storage for local use, explicit cloud checkpoint failures, bounded extraction, serialized run admission, graceful interruption, and deployment only after successful CI.

## How work proceeds

```text
request + files + selected mode/time
  -> retrieve supplied evidence; search public sources when needed
  -> select analysis breadth -> independent analyses/cross-checks when useful
  -> validated plan -> independent plan review -> optional user approval
  -> dependency-aware tasks -> independent reviews -> bounded repair/reassignment
  -> synthesize -> deterministic checks + independent final reviews
  -> investigate a remaining gap if time/budget allow -> repair -> recheck exact result
  -> deliver answer + sources + current checks + limitations + usage
```

The selected time is a research planning target. At the target, no new research work is admitted, while active workflow model calls retain at least a 20-minute working allowance and five minutes of finishing grace. Actual response times can increase their allowance; completed replies proceed immediately. The total active runtime is capped at **120 minutes, including final assembly**. New work stops earlier when necessary to reserve finishing time before that cap. One bounded final synthesis may extend beyond a shorter research target, within the total cap. Approval pauses the working clock, and resume retains spent call allowances. Interrupted text is saved separately as incomplete, unreviewed work. Explicit cancellation and token/call/trial guards still apply. Source-fetch and search-tool timeouts remain separate, shorter bounds.

| Mode | Starting policy | Appropriate use |
|---|---|---|
| Quick | One analyst, no preliminary cross-comparison, up to two plan tasks, one final reviewer when available | Focused questions and inexpensive first passes |
| Adaptive | One analyst for a simple request; two or three for complex/research work; bounded gap investigation | Default everyday work |
| Deep | Available team analyses and cross-checks; larger reserve for review and repair | Difficult research and multi-part problems |

The initial complexity policy is a transparent heuristic, not a trained optimal router. Actual call times update its estimates during a run. `compare_all=true/false` remains a legacy API override; the UI leaves it unset. More effort may cost more without improving quality: use the evaluation runner to compare.

## Understand the result

**Model review score** is a peer model’s opinion, not a correctness probability or benchmark score. Only reviews for the current answer contribute to it. Past reviews remain in the audit history.

| Status/check | Meaning |
|---|---|
| Reviewed | Current independent reviews passed and applicable recorded checks have no failure or unknown result |
| Partial | A check is unknown, a review is incomplete, or required work remains unfinished |
| Failed checks | A current check or review found a failure; inspect the details |
| Unverified | Insufficient current independent review |
| Python syntax passed | The exact downloadable Python file parses; dependencies, behavior, runtime and safety are untested |
| Citation coverage passed | Links match supplied retrieval or provider search metadata; claim support, completeness and source quality still require review |

SASI **does not execute generated code**. It does not provide an isolated execution service, automatically train ML models, or prove research conclusions. A source with `source_kind=google_search` is a provider-grounded summary, not a page quotation. Empty excerpts are discovered links only. Search failures stay visible; model prose is never substituted for retrieved evidence.

Project Eris mode retains its guidebook and downloadable `solution.py`, and never performs public search or fetches request links. Its output still needs external execution and evaluation under the challenge rules.

## Local development

Use Python 3.13 and Node.js 22 or newer compatible with the pinned Next.js release.

```powershell
# From the repository root (Windows)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
Copy-Item backend/.env.example backend/.env
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080

# In a second terminal, from the repository root
cd frontend
npm.cmd ci
npm.cmd run dev
```

Open http://localhost:3000. Configure existing Claude/Gemini keys on Connections, or configure Vertex credentials and `VERTEX_AI=true`. Secrets remain on the server. The example local configuration uses SQLite in `backend/data/sasi.db`; keep that file backed up. Do not use local SQLite on Cloud Run’s ephemeral filesystem.

For UI development without paid calls, set `ALLOW_SIMULATED=true`, leave provider keys empty and `VERTEX_AI=false`. Simulated results are explicitly labelled and are not quality evidence.

```powershell
# Tests from repository root
.\.venv\Scripts\python.exe -m pytest backend/tests -q
cd frontend
npm.cmd run typecheck
npm.cmd run build
```

The backend tests include fake-provider regression tests, not frontier-model benchmarks. See [evals](evals/README.md) for controlled comparisons of modes and human review of research. Live evaluations are opt-in and incur provider usage.

## Models and settings

Claude and Gemini are the frontier seats. All six supported open-model seats are configured: GLM, DeepSeek, Qwen, Kimi, MiniMax and gpt-oss. Outside trial-only mode, Claude uses its API key by default; Gemini and open models use Vertex AI. Claude on Vertex requires an explicit opt-in. Account access, lifecycle and task selection determine which models run. Qwen, Kimi and MiniMax routes are retired automatically on October 21, 2026 UTC; their cards retain an explanation.

The deployment enforces **`TRIAL_CREDITS_ONLY=true`** for Google Cloud routes: Vertex Gemini is allowed, while partner models and the Gemini API-key fallback are blocked before probes or inference. The owner has separately enabled **`ALLOW_CLAUDE_API_KEY=true`** for a dedicated, owner-funded Anthropic key entered through Connections; those charges are outside Google Cloud credits. Legacy Claude keys are not activated by this exception. Google welcome credits exclude managed partner models and AI Studio API costs. This policy does not measure your credit balance or cap billing. Verify the account is still an unupgraded Free Trial, the remaining credit and its expiry before deployment; do not upgrade billing to make models available. [Google trial coverage](https://docs.cloud.google.com/free/docs/free-cloud-features)

| Setting | Default |
|---|---|
| `RUN_TIME_LIMIT_MINUTES` | 120; each new request can select a 20–120 minute research target within the server ceiling |
| `MAX_TOTAL_RUN_MINUTES` | 120; hard total active-runtime cap including final assembly, retained across resume |
| `MODEL_PHASE_MIN_MINUTES` | 20; minimum working allowance for an admitted workflow model call (completed replies proceed immediately) |
| `MODEL_PHASE_GRACE_MINUTES` | 5; finishing grace after a call's allowance; active work and final synthesis stay inside the total cap |
| `MAX_MODEL_CALLS_PER_RUN` | 300 |
| `MAX_TOKENS_PER_RUN` | 2,400,000 |
| `ERIS_MAX_TOKENS_PER_RUN` | 3,600,000 |
| `MAX_CONCURRENT_RUNS` | 3 in one process |
| `MAX_TASKS_PER_PLAN` | 6; Quick restricts this further |
| `MAX_GAP_TASKS_PER_RUN`, `MAX_REPLANS_PER_RUN` | 1 each |
| `ENABLE_WEB_RESEARCH` | true; requires a working Gemini grounding route |
| `RESEARCH_TIMEOUT_SECONDS` | 45 |
| `STORAGE_BACKEND` | memory in tests; use sqlite locally or bigquery in cloud |
| `OPEN_MODELS` | glm,deepseek,qwen,kimi,minimax,gptoss; blocked while trial-only mode is on |
| `TRIAL_CREDITS_ONLY` | false locally; true in deployment |
| `VERTEX_CLAUDE_ENABLED` | false; Claude uses its separate API key outside trial-only mode |
| `REQUIRE_ACCESS_TOKEN` | false locally; true in deployment, fails closed when owner authentication is missing |
| `TEAM_SIZE` | 4 maximum initial seats |

Zero disables optional token/call caps. Token reservations are estimates and final usage follows provider reporting; these are not exact monetary spending limits. Google Search can add tool charges. Set billing alerts separately.

Uploads: at most ten files, 25 MiB each and 60 MiB total per run. Documents/tables/archives have additional expanded-size, row, column and cell limits. Large tables are explicitly labelled sampled previews; SASI no longer claims a whole-file statistical profile for partial extraction. Images, PDFs, audio and video are sent only to providers able to read them.

## Deployment and limits

[Setup](docs/SETUP.md) covers the existing Google Cloud project. [Release notes](docs/QUALITY_AND_RELEASE.md) describe what was changed and what still needs production work.

The automatic workflow deploys the exact successful CI commit on `main`, and skips superseded commits. Cloud configuration allows scale-to-zero, at most one instance, and CPU outside HTTP requests while an instance exists. Instance CPU/storage/build usage can consume credits. Scale-down or a restart can interrupt a background run; resume from its checkpoint. The manual deployment script is for an operator who has already run the checks; it is not a CI bypass recommendation.

Production requires a private owner access token (`APP_ACCESS_CODE`) and a separate session secret in Secret Manager. The login exchanges the token for a secure HTTP-only session cookie; API endpoints and downloads require authentication. Rotate both secrets when replacing owner access so old sessions stop working. Anyone holding the token can sign in: keep it private and never commit it or put it in a URL.

The current workflow engine still runs in one process. Checkpoints preserve accepted work, but a process/platform restart interrupts the run; use Resume. It is not a durable job queue, and multiple API workers or instances are unsupported. BigQuery remains the cloud checkpoint store; it is not a transactional work scheduler. The shared access code is for one trusted owner/team, not separate tenant accounts.

## Code map

- `backend/app/workflow/`: controller, effort policy, role prompts and routing history.
- `backend/app/gateway/`: provider adapters, grounding metadata, context fitting and model routes.
- `backend/app/tools/`: bounded source fetching and attachment evidence.
- `backend/app/verification.py`: exact artifact checks without code execution.
- `backend/app/store/`: memory, SQLite and BigQuery state.
- `backend/app/extract.py`: bounded document/data extraction.
- `frontend/`: Next.js static-export research workspace.
- `evals/`: opt-in outcome evaluation; `backend/tests/`: regression suite.

Provider references: [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search), [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort), [Cloud Run background CPU and billing](https://docs.cloud.google.com/run/docs/configuring/billing-settings), [open-model lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/deprecations/open-models).
