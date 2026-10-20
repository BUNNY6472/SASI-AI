# SASI

**Sasidhar’s Artificial Super Intelligence** is a research and problem-solving workspace that coordinates existing models, supplied files, public evidence, and independent review. It is an orchestration application, not a newly trained frontier model. Its quality must be measured on real tasks; adding more models does not guarantee a better answer.

## What this version changes

- **Question-driven bridge:** new requests have no session timer. One model writes the answer and another returns compact, structured findings. A targeted correction and recheck run only when needed. Deep and Eris can add a short approach brief.
- **Evidence before synthesis:** explicit HTTPS links are fetched with size/time limits and SSRF protection. Research/current-information requests can use the existing Gemini connection with Google Search grounding. Sources, retrieval times, search queries and unavailable evidence are visible.
- **Honest quality labels:** invalid review JSON cannot pass; `"false"` is not a boolean verdict; incomplete model responses are rejected. Each review is bound to an answer version and SHA-256. Editing an answer invalidates its old score.
- **Deterministic checks:** Python syntax is parsed without executing it. Citation coverage checks whether links occur in fetched evidence or provider search metadata. These checks can trigger a repair; they do not prove functional correctness or claim truth.
- **A focused interface:** ask a question, optionally attach files and choose effort, then read one complete answer. Compact collaboration activity, actual usage and expandable sources/checks remain available.
- **Operational controls:** durable SQLite storage for local use, explicit cloud checkpoint failures, bounded extraction, serialized run admission, graceful interruption, and deployment only after successful CI.

## How work proceeds

```text
question + optional files
  -> source evidence and CPU passage selection
  -> route to healthy, policy-allowed providers
  -> compact approach brief only when warranted
  -> one complete answer
  -> deterministic checks + compact independent verifier packet
  -> targeted correction and recheck only when needed
  -> one answer + evidence + actual usage + unresolved checks
```

New requests use `workflow=bridge` and `timing_policy=question_driven`. They finish according to completed work and checks, without a session target or a 120-minute session cutoff. Finite provider request timeouts still bound stalled network/model calls. Cancellation, token/call/trial limits and bounded correction attempts remain. Persisted legacy pipeline runs retain their original workflow and timing behavior.

| Mode | Starting policy | Appropriate use |
|---|---|---|
| Quick | Direct answer and compact independent review | Focused questions |
| Adaptive | Question-sized answer allowance, independent review and targeted correction | Default everyday work |
| Deep | Short approach brief before answering and review; more answer room | Difficult research and multi-part problems |

Routing is an auditable heuristic, not a trained optimal router. The bridge is CPU orchestration, not a hosted SLM or a CUDA engine. A connected provider handles the generative roles; credential and billing policies still decide eligibility. No GPT integration was added. See [the research, competition adaptations and security contract](docs/COLLABORATION_BRIDGE.md).

## Understand the result

New bridge answers display findings and verification status rather than a model-generated numerical score. Historical pipeline runs may contain a **model review score**: it is a peer model’s opinion, not a correctness probability or benchmark score. Only reviews for the current answer contribute to that assessment. Past reviews remain in the audit history.

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
| `PROVIDER_REQUEST_TIMEOUT_SECONDS` | 900; transport timeout for a single bridge request, not a session duration |
| `RUN_TIME_LIMIT_MINUTES`, `MAX_TOTAL_RUN_MINUTES` | Legacy pipeline only; ignored by new bridge requests |
| `MODEL_PHASE_MIN_MINUTES`, `MODEL_PHASE_GRACE_MINUTES` | Legacy pipeline only |
| `MAX_MODEL_CALLS_PER_RUN` | 300 |
| `MAX_TOKENS_PER_RUN` | 5,000,000 |
| `ERIS_MAX_TOKENS_PER_RUN` | 5,000,000 |
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

With `UPLOAD_BUCKET` configured, uploads support **10 GiB combined per run**, up to 20 files and 10 GiB per file. The browser uploads bounded chunks directly to private Cloud Storage with retry/resume; Cloud Run never buffers the whole large request. Staged reservations enforce the aggregate limit across browser tabs. Originals are retained separately from bounded previews and are deleted with their run. Legacy BigQuery files remain readable. Without a bucket, local uploads retain a 25 MiB per-file and 60 MiB aggregate limit. Reload an older open page to load the resumable upload client.

The upload allowance is not a model context window: text previews inspect at most the first 2 MiB and tables have further row/column/cell bounds. CSV/JSON datasets become compact statistical reports and examples instead of repeated raw dumps. Stage-specific excerpts preserve source references and explicitly identify omissions. Large binary/media files are stored for download but are not claimed to have been inspected; eligible complete media is bounded to 8 MiB per file and 64 MiB resident per run. Generated ML scripts can use the complete original datasets when executed separately; SASI does not execute arbitrary training code inside its API process.

Bridge messages have bounded plan/review output allowances; the complete user request and answer under review remain intact. Repeated unchanged failed drafts do not start an endless discussion. Claude caches shared Eris rules for one hour and eligible repeated file prefixes for five minutes; Gemini implicit cache usage is recorded. Cache hits depend on exact prefixes and are not guaranteed. Token accounting separates cache reads/writes, uncached input, reported thinking, stage totals and conservative estimates. It does not invent dollar amounts or reinterpret historical totals as uncached billing.

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
