# SASI regression evaluation

This small, public suite checks whether application changes improve independently measured outcomes. It is **not a frontier benchmark**, is not held out from development, and cannot establish superiority over commercial assistants. Peer model scores are not used as labels.

Run from the repository root using the backend environment. The default command only reads and validates local fixtures:

```powershell
.\.venv\Scripts\python.exe -m evals.run_eval --validate --max-cases 6
```

To use an already running SASI API, set `SASI_ACCESS_CODE` through your normal secure environment configuration if the app requires login. The runner never prints the access code and does not accept it as a command-line argument. No provider credentials are needed by this client; the server's configured routes are used.

The following command creates potentially billable runs. It is an example, not a step performed during implementation:

```powershell
.\.venv\Scripts\python.exe -m evals.run_eval --execute --base-url https://YOUR-SASI-HOST --max-cases 3 --minutes 20
```

The three modes are Quick, Adaptive (Auto in the UI) and Deep. With the example settings, three cases create nine sequential bridge runs. New questions have no session timer: `--minutes 20` is the evaluation client's watchdog, not an application research target. Each run finishes when its answer and bounded checks finish. Server-level call/token limits and finite provider request timeouts still apply and are captured in the experiment metadata. Compare actual wall time and tokens alongside outcomes. Provider usage may contain estimates and is not a monetary bill.

For question-driven runs, the harness requests cancellation if the client watchdog expires while a run is still active. Historical graceful and strict runs retain their older watchdog allowances for compatibility. The run's timing policy and watchdog allowance are recorded with every result; the harness does not silently add a session timer to the application.

Results go to a new `evals/results/<UTC-time>.jsonl` file. Existing files are never overwritten. Each result contains the actual prompt, output, run ID, mode, timing, token usage, observed model IDs, checks, source evidence, output hash, and external outcome. The fixture hash, provider snapshot and server limits are recorded once. Runs remain in the SASI app until you delete them normally. A timeout or Ctrl-C requests cancellation of a known active run; if cancellation or run creation has an unknown outcome, inspect the app before retrying.

For a meaningful comparison:

1. Use the same fixture version, application revision, provider/model pins, server token/call caps, request timeout and client watchdog for all modes. Record the application commit separately if your API does not expose it. The runner rotates mode order across cases, but this does not remove provider load or model randomness.
2. Inspect each accepted oracle before running. Objective cases use exact text or strict JSON equality. JSON key order is ignored; extra prose, Markdown fences, duplicate keys and incorrect types fail. A correct number embedded in an incorrectly formatted essay still fails a strict-format case. The oracle never executes generated code.
3. Count incomplete, interrupted, simulated and failed runs separately; do not hide them or count them as successes. Distinguish completed objective failures from cases that were not scored. Report the total case count, success count, failures, unscored count, median latency and tokens. Repeat the experiment across seeds/runs or independent sessions before drawing conclusions.
4. The research case stays **unscored** until a human reviews it. Hide mode/model identity, have reviewers inspect the actual cited documents and evidence dates, and apply every rubric item. Record claim-level support and material omissions, not just style preferences. A citation URL appearing in fetched evidence does not establish that the page supports a claim.
5. Add representative private tasks and a held-out test split before using results to choose routing policies. These six toy cases exercise basic arithmetic, data aggregation, dependencies, instruction following, conflicting records and research structure; they do not cover complex software engineering or novel scientific discovery.

You can select modes with `--modes quick,adaptive`, choose an alternate fixture JSON with `--cases`, or provide a new output path with `--output`. `--max-cases` defaults to three and is capped at twenty; `--execute` is always required for API calls. Remote endpoints require HTTPS; HTTP is allowed only for a local loopback server.
