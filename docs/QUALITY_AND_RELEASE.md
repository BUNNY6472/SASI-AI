# SASI quality and workspace upgrade

SASI means **Sasidhar’s Artificial Super Intelligence**. This release improves the existing application’s orchestration, evidence handling and usability. It does not train a new foundation model or establish frontier-level benchmark performance.

## Implemented

1. **Replace mandatory comparison with effort modes.** Quick starts with one analyst; Adaptive increases breadth for complex/research work; Deep uses broader comparison. Observed call durations update the reserve for synthesis and review. Claude effort varies by stage and remaining time. A bounded additional investigation can address final-review gaps.
2. **Make review gates trustworthy.** Invalid plans/reviews cannot silently pass. String booleans and incomplete responses are rejected. Reassignments retain the previous draft and reviewer feedback. Original criteria, relevant inputs and source evidence reach reviewers.
3. **Bind assessments to exact output.** Each final version has a SHA-256. Old reviews cannot validate a changed answer. The latest review round is authoritative for identical bytes; old rounds remain history. Current deterministic failures override positive peer opinions.
4. **Add public research and explicit provenance.** Supplied HTTPS URLs are fetched with public-address validation, DNS pinning, redirect validation and strict size/time bounds. Gemini can discover public evidence using Google Search grounding. Provider summaries, fetched page excerpts, attachment excerpts and failed retrievals are distinguished. Eris does not fetch external evidence.
5. **Check what the app can actually check.** The exact downloadable Python artifact is parsed for syntax without execution. Citation coverage checks known source links. Execution remains unknown, and source coverage does not establish claim support. Known failures feed the repair loop.
6. **Improve persistence and capacity.** Local SQLite checkpoints retain runs, files and outputs on persistent disk. Cache eviction does not delete durable history. Cloud checkpoint failures are explicit. Run admission is serialized, shutdown preserves interrupted work, and extraction is bounded and moved off the event loop.
7. **Replace the visual shell.** The active app has a responsive workspace, readable composer, effort/time selection, attachments, history, progress, plan approval, sources/checks and connection management. Simulation is clearly labelled. The unused 3D scene and Three.js dependencies are removed.
8. **Add external outcome measurement.** `evals/` compares Quick/Adaptive/Deep under a common time ceiling, records output/usage/provenance, uses independent exact-answer oracles, and leaves research for human review. It does not use peer scores as ground truth.
9. **Gate automatic deployment.** Only a successful CI run for the current main commit triggers deployment of that exact revision. Failed or superseded runs do not deploy.

## Requested resource limits

The user requested a threefold increase over the initial upgrade defaults:

| Limit | Initial upgrade | Current default |
|---|---:|---:|
| Model calls per run | 100 | **300** |
| Tokens per normal run | 800,000 | **2,400,000** |
| Tokens per Eris run | 1,200,000 | **3,600,000** |

These are maximums, not targets. The selected 5–60 minute ceiling still applies. Existing explicit environment overrides take precedence, so check `/api/limits` after deployment. Model generations reserve estimated tokens before parallel dispatch. Grounded-search fallback attempts and available usage from rejected research responses are counted. Provider-side retries, uncertain usage after cancellation and separate search charges prevent these guards from being an exact monetary cap.

## Release boundary

Local validation on 2026-10-17: **205 backend tests passed**, including trial-policy restrictions and owner authentication; the production frontend build passed. Desktop and 390-pixel mobile browser checks covered navigation, uploads, plan approval, simulated completion, evidence/checks and settings. SQLite history and the uploaded attachment survived a backend restart. The six-case evaluation suite passed dry validation (18 planned mode comparisons); no paid model evaluation or live quality score was produced.

The upgrade is prepared in branch `codex/sasi-quality-workspace`. A production release requires successful CI, verified billing constraints, configured GitHub federation and a checked Cloud Run revision. Local tests and browser checks alone do not establish a successful deployment.

The checked-in Cloud Run configuration permits scale-to-zero, caps instances at one and gives an existing instance CPU outside requests. Usage can consume credits. A scale-down or restart can interrupt execution; the user resumes from the last durable checkpoint. This is not a durable queue, an automatic retry scheduler, or a supported multi-instance architecture.

The requested deployment uses `TRIAL_CREDITS_ONLY=true`: only Vertex Gemini is allowed, with partner models and all API-key fallbacks disabled before any provider probes. All six open-model integrations remain configured for a later funded configuration. Qwen, Kimi and MiniMax retire on October 21, 2026 UTC and are excluded automatically on/after that date. Production also sets `REQUIRE_ACCESS_TOKEN=true`, requiring the private owner token and a non-default session secret. The deployment preserves unrelated environment variables and Secret Manager bindings.

Trial-only routing is not a billing cap. Before rollout, verify the linked account's actual Free Trial status, remaining credit, credit scope and expiry in Cloud Billing. Set alerts against credit consumption without allowing promotional credits to hide spend. Alerts and service spend caps cannot promise zero out-of-pocket costs on an already-paid account. No account upgrade is authorized by this release.

The manual deployment script remains an operator path. Run the regression suite and production frontend build before using it. Check the deployed revision, health, sign-in, Connections, `/api/limits`, a bounded real run, interrupted-run recovery and research provenance after rollout. Use a prior Cloud Run revision for rollback if needed.

## Still necessary before claiming high real-world scores

- **Measure a baseline.** Compare the workflow with a strong single-model baseline on representative tasks, with fixed provider versions, held-out cases, actual costs and blind human review for research. The included six-case suite is a smoke evaluation, not a public leaderboard or a held-out benchmark.
- **Add isolated execution.** Functional software/ML claims require a separate sandbox with resource limits, controlled network access, test artifacts and recorded exit/results. Never execute arbitrary generated code inside the API process.
- **Use durable workers for production scale.** A transactional shared state store, queue, leases, idempotent tool calls, cancellation and restart recovery are the next infrastructure step. BigQuery checkpoints are a record, not a job scheduler.
- **Improve evidence coverage.** Current discovery is an initial bounded grounding call; gap investigations use the collected evidence. This is not an unrestricted search/replanning loop. Claim-level support, contradictory evidence and document coverage still need evaluation.
- **Calibrate routing.** Current routing history measures peer acceptance, not independent correctness. Do not optimize it for larger peer-review numbers. Update routing using measured task outcomes only after collecting a trustworthy dataset.

Provider documentation consulted: [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search), [Claude effort controls](https://platform.claude.com/docs/en/build-with-claude/effort), [Cloud Run CPU and billing](https://docs.cloud.google.com/run/docs/configuring/billing-settings), [model lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/deprecations/open-models).
