"use client";

import { useEffect, useMemo, useState } from "react";
import DOMPurify from "dompurify";
import {
  api,
  fileSize,
  Judgement,
  Opinion,
  providerMeta,
  Run,
  Task,
} from "@/lib/api";
import Markdown from "./Markdown";
import { OriginalRequest, RunQuestion } from "./RunRequest";
import { ActiveCalls, RunClock } from "./RunTiming";
import IncompleteWork from "./IncompleteWork";
import UsageDetails from "./UsageDetails";
import FileCoverage from "./FileCoverage";
import BridgeSession from "./BridgeSession";
import { AgentChip, StatusBadge, fmt } from "./ui";

const LIVE = new Set([
  "pending",
  "researching",
  "analyzing",
  "cross_checking",
  "planning",
  "reviewing_plan",
  "awaiting_approval",
  "running",
  "integrating",
  "verifying",
]);

/** The run's models in team order (frontier first), plus any older run's models. */
function teamOrder(run: Run): string[] {
  return [
    ...new Set([
      ...(run.team || []),
      ...run.analyses.map((a) => a.provider),
      ...run.cross_checks.map((c) => c.provider),
      ...Object.values(run.live || {}).map((l) => l.provider),
    ]),
  ];
}
const FIELD_NAME: Record<string, string> = {
  software: "Software engineering",
  research: "Research and analysis",
  general: "General",
  eris: "Project Eris ML",
};
const TERMINAL = new Set([
  "completed",
  "failed",
  "cancelled",
  "budget_stopped",
  "interrupted",
]);

export default function RunView({
  id,
  onRun,
  back,
}: {
  id: string;
  onRun: (r: Run | null) => void;
  back: () => void;
}) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<
    "compare" | "plan" | "result" | "evidence" | "activity" | "partials"
  >("plan");
  const [followStage, setFollowStage] = useState(true);
  const [openTask, setOpenTask] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stream, setStream] = useState(0);

  useEffect(() => {
    let es: EventSource | null = null;
    let stopped = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const connect = () => {
      es = new EventSource(`/api/runs/${id}/events`);
      es.onmessage = (m) => {
        try {
          const r = JSON.parse(m.data) as Run;
          if (!stopped) {
            setRun(r);
            setError("");
          }
          if (TERMINAL.has(r.status)) es?.close();
        } catch {
          setError(
            "An update could not be read. Reconnecting to the research session…",
          );
        }
      };
      es.onerror = () => {
        es?.close();
        // Reconnect while the run is live (Cloud Run can drop long streams).
        if (!stopped)
          retry = setTimeout(
            () =>
              api<Run>(`/api/runs/${id}`)
                .then((r) => {
                  if (stopped) return;
                  setRun(r);
                  setError("");
                  if (LIVE.has(r.status)) connect();
                })
                .catch((e) => {
                  if (!stopped) {
                    setError(e.message);
                    retry = setTimeout(connect, 4000);
                  }
                }),
            1500,
          );
      };
    };
    api<Run>(`/api/runs/${id}`)
      .then((r) => {
        if (!stopped) setRun(r);
      })
      .catch((e) => {
        if (!stopped) setError(e.message);
      });
    connect();
    return () => {
      stopped = true;
      clearTimeout(retry);
      es?.close();
      onRun(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, stream]);

  useEffect(() => {
    onRun(run);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.status, run?.updated_at]);

  // Follow the work: show the stage that is happening now.
  useEffect(() => {
    const s = run?.status;
    if (!s || !followStage) return;
    if (run?.workflow === "bridge" || run?.timing_policy === "question_driven") { setTab("result"); return; }
    if (s === "researching") setTab("evidence");
    else if (s === "analyzing" || s === "cross_checking") setTab("compare");
    else if (
      ["planning", "reviewing_plan", "awaiting_approval", "running"].includes(s)
    )
      setTab("plan");
    else if (s === "integrating" || s === "completed" || s === "verifying")
      setTab("result");
    else if (!run?.plan && run?.compare_all) setTab("compare");
  }, [run?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  async function remove() {
    if (
      !window.confirm(
        "Delete this research session and its stored files and results? This cannot be undone.",
      )
    )
      return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/runs/${id}`, { method: "DELETE" });
      back();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  async function action(path: string) {
    setBusy(true);
    setError("");
    try {
      const r = await api<Run | { ok: boolean }>(`/api/runs/${id}/${path}`, {
        method: "POST",
        body: "{}",
      });
      if ("id" in r) setRun(r);
      if (path === "resume" || path === "approve") setStream((n) => n + 1);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const layers = useMemo(
    () => (run?.plan ? layerTasks(run.plan.tasks) : []),
    [run?.plan],
  );

  if (!run)
    return (
      <div className="loading" role="status">
        <p>{error || "Opening your research session…"}</p>
        {error && (
          <button
            className="btn"
            onClick={() => {
              setError("");
              setStream((n) => n + 1);
            }}
          >
            Try again
          </button>
        )}
      </div>
    );
  const live = LIVE.has(run.status);
  const bridge = run.workflow === "bridge" || run.timing_policy === "question_driven";
  const total = run.plan?.tasks.length || 0;
  const done =
    run.plan?.tasks.filter((t) => t.status === "accepted").length || 0;
  const chooseTab = (next: typeof tab) => {
    setFollowStage(false);
    setTab(next);
  };

  return (
    <div className="runview">
      <div className="run-head">
        <button className="btn ghost" onClick={back}>
          ← Workspace
        </button>
        <div className="run-title">
          <StatusBadge status={run.status} />
          {run.simulated && (
            <span className="badge demo">Simulated output</span>
          )}
          {run.eris ? (
            <span
              className="badge eris"
              title="Every model follows the Eris Solver Guidebook. Dry runs only, never a full end-to-end run."
            >
              Project Eris · dry runs only
            </span>
          ) : (
            <span
              className="badge field"
              title="Chosen automatically from your request"
            >
              {FIELD_NAME[run.profile] || run.profile}
            </span>
          )}
          <RunQuestion text={run.request} />
        </div>
        <div className="run-actions">
          {run.status === "awaiting_approval" && (
            <button
              className="btn primary"
              disabled={busy}
              onClick={() => action("approve")}
            >
              Approve plan and start
            </button>
          )}
          {live && (
            <button
              className="btn danger"
              disabled={busy}
              onClick={() => action("cancel")}
            >
              Cancel
            </button>
          )}
          {["interrupted", "budget_stopped", "failed", "cancelled"].includes(
            run.status,
          ) &&
            (bridge || run.plan || run.analyses.length > 0) && (
              <button
                className="btn"
                disabled={busy}
                onClick={() => action("resume")}
              >
                Resume from checkpoint
              </button>
            )}
          {TERMINAL.has(run.status) && (
            <button
              className="btn danger"
              disabled={busy}
              onClick={remove}
              title="Remove this session and its stored files and results"
            >
              Delete
            </button>
          )}
        </div>
      </div>
      <OriginalRequest
        key={run.id}
        text={run.request}
        action={<CopyButton text={run.request} label="Copy request" />}
      />
      {run.simulated && (
        <div className="notice warn" role="status">
          <strong>Preview mode · simulated output.</strong> These results
          demonstrate the workflow and are not real model answers. Review scores
          and generated content are demonstrations.
        </div>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {run.error && <div className="notice warn">{run.error}</div>}

      {run.files.length > 0 && (
        <div className="file-strip">
          {run.files.map((f) => (
            <a
              key={f.id}
              className="file-chip glass"
              href={`/api/runs/${run.id}/files/${f.id}`}
              target="_blank"
              rel="noreferrer"
              title={f.name}
            >
              {f.kind === "image" && f.size <= 2 * 1024 ** 2 ? (
                <img src={`/api/runs/${run.id}/files/${f.id}`} alt="" />
              ) : (
                <span className="file-kind">
                  {f.name.includes(".")
                    ? f.name.split(".").pop()!.slice(0, 8)
                    : f.kind}
                </span>
              )}
              <span className="file-name">{f.name}</span>
              <small className="muted">{fileSize(f.size)}</small>
            </a>
          ))}
        </div>
      )}

      <FileCoverage files={run.files} />

      {(run.team?.length || 0) > 0 && (
        <div
          className={`notice team-note small ${!bridge && run.skipped && Object.keys(run.skipped).length ? "warn" : ""}`}
        >
          {teamOrder(run).some((p) => !(p in (run.skipped || {}))) ? (
            <b>
              Team:{" "}
              {joinNames(
                teamOrder(run)
                  .filter((p) => !(p in (run.skipped || {})))
                  .map((p) => providerMeta(p).name),
              )}
              .
            </b>
          ) : (
            <b>No model could take part.</b>
          )}{" "}
          {bridge && Object.keys(run.skipped || {}).length > 0 ? (
            <details>
              <summary>{Object.keys(run.skipped || {}).length} unavailable models</summary>
              <ul>{Object.entries(run.skipped || {}).map(([p, why]) => (
                <li key={p}>{providerMeta(p).name}: {why.replace(/\.+$/, "")}.</li>
              ))}</ul>
            </details>
          ) : Object.entries(run.skipped || {}).map(([p, why]) => (
            <span key={p}>
              {providerMeta(p).name}: {why.replace(/\.+$/, "")}.{" "}
            </span>
          ))}
        </div>
      )}

      {bridge ? <BridgeSession
        run={run}
        evidence={<EvidencePanel run={run} />}
        solution={run.solution ? <SolutionFile run={run} /> : null}
        copyAnswer={run.final ? <CopyButton text={run.final.output} label="Copy answer" /> : null}
      /> : <>
      <div className="progress-strip glass">
        {run.research_status &&
          !["not_started", "not_needed"].includes(run.research_status) && (
            <Stage
              label="Research"
              state={
                run.research_status === "collecting"
                  ? "now"
                  : run.evidence?.length
                    ? "done"
                    : "todo"
              }
            />
          )}
        {run.compare_all && (
          <Stage label="Analyse" state={compareState(run, 0)} />
        )}
        {run.compare_all && (
          <Stage label="Compare" state={compareState(run, 1)} />
        )}
        <Stage label="Plan" state={stageState(run, 0)} />
        <Stage label="Review plan" state={stageState(run, 1)} />
        <Stage label="Approve" state={stageState(run, 2)} />
        <Stage label={`Work ${done}/${total}`} state={stageState(run, 3)} />
        <Stage label="Build one result" state={stageState(run, 4)} />
        <Stage label="Review & checks" state={stageState(run, 5)} />
        <div className="usage">
          <RunClock run={run} />
          {fmt(run.usage.calls)} calls ·{" "}
          {fmt(run.usage.input_tokens + run.usage.output_tokens)} tokens
        </div>
      </div>

      <ActiveCalls run={run} />
      <UsageDetails usage={run.usage} />

      <div className="tabs">
        <button
          className={tab === "result" ? "tab on" : "tab"}
          onClick={() => chooseTab("result")}
        >
          Result
        </button>
        <button
          className={tab === "plan" ? "tab on" : "tab"}
          onClick={() => chooseTab("plan")}
        >
          Plan & tasks {total > 0 && `· ${done}/${total}`}
        </button>
        <button
          className={tab === "evidence" ? "tab on" : "tab"}
          onClick={() => chooseTab("evidence")}
        >
          Evidence & checks{" "}
          {(run.evidence?.length || 0) > 0 && `· ${run.evidence!.length}`}
        </button>
        {(run.compare_all || run.analyses.length > 0) && (
          <button
            className={tab === "compare" ? "tab on" : "tab"}
            onClick={() => chooseTab("compare")}
          >
            Model perspectives
          </button>
        )}
        <button
          className={tab === "activity" ? "tab on" : "tab"}
          onClick={() => chooseTab("activity")}
        >
          Activity
        </button>
        {!!run.partial_outputs?.length && (
          <button
            className={tab === "partials" ? "tab on" : "tab"}
            onClick={() => chooseTab("partials")}
          >
            Incomplete work · {run.partial_outputs.length}
          </button>
        )}
      </div>

      {tab === "partials" && (
        <IncompleteWork outputs={run.partial_outputs || []} />
      )}
      {tab === "evidence" && <EvidencePanel run={run} />}
      {tab === "result" && !run.final && !run.live.final && (
        <div className="glass evidence-panel evidence-empty">
          <h2>
            {live
              ? "Your result is taking shape."
              : "No final result was produced."}
          </h2>
          <p>
            {live
              ? "You can follow the plan, inspect source evidence, or read model perspectives while the work continues."
              : "Review the activity log and any completed tasks for saved work and failure details."}
          </p>
          <button className="btn small" onClick={() => chooseTab("plan")}>
            View plan & tasks
          </button>
          {!!run.partial_outputs?.length && (
            <button
              className="btn small ghost"
              onClick={() => chooseTab("partials")}
            >
              View incomplete work
            </button>
          )}
        </div>
      )}

      {tab === "compare" && (
        <div className="compare">
          <h2>Independent analyses</h2>
          <p className="muted small">
            The selected models examine your request
            {run.files.length ? " and files" : ""} independently. Your research
            mode determines how many perspectives are requested.
          </p>
          <OpinionGrid
            run={run}
            opinions={run.analyses}
            prefix="analysis"
            waiting={run.status === "analyzing"}
            empty="Waiting to start…"
          />
          {(run.cross_checks.length > 0 || run.status === "cross_checking") && (
            <>
              <h2>Cross-checks</h2>
              <p className="muted small">
                Each model read the others&apos; analyses and compared them with
                its own: agreements, disagreements and mistakes.
              </p>
              <OpinionGrid
                run={run}
                opinions={run.cross_checks}
                prefix="cross"
                waiting={run.status === "cross_checking"}
                empty="Waiting for the analyses…"
              />
            </>
          )}
          <Scoreboard run={run} />
        </div>
      )}

      {tab === "result" && run.live.final && (
        <article className="glass result">
          <div className="result-meta">
            <AgentChip id={run.live.final.provider} />
            {run.wrapping_up
              ? " is assembling the available work within the finalization allowance"
              : run.final
                ? " is fixing what the other models found"
                : " is writing the one combined result"}{" "}
            <span className="typing" />
          </div>
          <Markdown text={run.live.final.text} />
        </article>
      )}

      {tab === "plan" && (
        <div className="plan-grid">
          <div className="glass plan-card">
            {!run.plan && (
              <p className={live ? "muted pulse" : "muted"}>
                {live
                  ? "The plan will appear here as the work develops."
                  : "No plan was produced for this session."}
              </p>
            )}
            {run.plan && (
              <>
                <div className="kv">
                  <span>Goal</span>
                  <p>{run.plan.goal}</p>
                </div>
                <div className="kv">
                  <span>Planned by</span>
                  <p>
                    <AgentChip id={run.plan.planner} />{" "}
                    <small className="muted">v{run.plan.version}</small>
                  </p>
                </div>
                <List
                  title="Success criteria"
                  items={run.plan.success_criteria}
                />
                <List title="Assumptions" items={run.plan.assumptions} />
                <List title="Deliverables" items={run.plan.deliverables} />
                {run.plan_review && (
                  <div className="review-box">
                    <div>
                      <AgentChip id={run.plan_review.reviewer} /> reviewed the
                      plan: <b>{run.plan_review.verdict}</b>
                      {run.plan_review.score != null &&
                        ` · ${run.plan_review.score}/10`}
                    </div>
                    <ul>
                      {run.plan_review.findings.map((f, i) => (
                        <li key={i}>{f}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </div>
          <div className="graph">
            {layers.map((layer, i) => (
              <div key={i} className="layer">
                <div className="layer-label">
                  {i === 0 ? "Starts first" : `Step ${i + 1}`}
                  {layer.length > 1 ? " · parallel" : ""}
                </div>
                <div className="layer-tasks">
                  {layer.map((t) => (
                    <TaskCard
                      key={t.id}
                      task={t}
                      live={run.live[`task:${t.id}`]?.text}
                      open={openTask === t.id}
                      toggle={() =>
                        setOpenTask(openTask === t.id ? null : t.id)
                      }
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === "result" && run.final && !run.live.final && (
        <div className="result-grid">
          <article className="glass result">
            <div className="one-banner">
              <div>
                <span className="eyebrow">Your research result</span>
                <p className="small muted">
                  {run.final.best_approach && (
                    <>
                      Built on the approach of{" "}
                      <AgentChip id={run.final.best_approach} /> ·{" "}
                    </>
                  )}
                  written by <AgentChip id={run.final.integrator} />
                  {lastRound(currentJudges(run)).length > 0 && (
                    <>
                      {" "}
                      · reviewed by{" "}
                      {lastRound(currentJudges(run)).map((j) => (
                        <AgentChip key={j.judge} id={j.judge} />
                      ))}
                    </>
                  )}
                  {run.final.fixes > 0 && (
                    <>
                      {" "}
                      · revised{" "}
                      {run.final.fixes === 1
                        ? "once"
                        : `${run.final.fixes} times`}{" "}
                      after review
                    </>
                  )}
                </p>
              </div>
              {run.final.score != null ? (
                <div
                  className="big-score"
                  title="A model opinion score, not a probability of correctness or an execution test"
                >
                  <b>{run.final.score}</b>
                  <small>/10</small>
                  <span>Model review score</span>
                </div>
              ) : run.status === "integrating" ? (
                <small className="muted">
                  In review <span className="typing" />
                </small>
              ) : null}
            </div>
            {run.solution && <SolutionFile run={run} />}
            <div className="result-meta">
              <CopyButton text={run.final.output} label="Copy all" />
            </div>
            <Markdown text={run.final.output} />
          </article>
          <aside className="glass evidence">
            <h3>Model review</h3>
            <p className="small muted">
              Model opinions can identify issues. They do not establish that
              code ran or that every claim is correct.
            </p>
            <button className="btn small" onClick={() => chooseTab("evidence")}>
              Inspect evidence & checks
            </button>
            {run.status === "integrating" && (
              <p className="muted pulse small">The result is being reviewed…</p>
            )}
            {run.final.meets_criteria != null && (
              <p className={run.final.meets_criteria ? "ok" : "warn-text"}>
                {run.final.meets_criteria
                  ? "Models judged the criteria met"
                  : "Models identified unmet criteria"}
              </p>
            )}
            <JudgeList
              judges={run.final.judges}
              currentHash={run.final.artifact_hash || undefined}
            />
            <List title="Gaps" items={run.final.gaps} />
            <List title="Limitations" items={run.final.limitations} />
            <h3>Who did what</h3>
            <ul className="who">
              {run.plan?.tasks.map((t) => (
                <li key={t.id}>
                  <span>{t.title}</span>
                  <span>
                    <AgentChip id={t.implementer} /> →{" "}
                    <AgentChip id={t.reviewer} />
                  </span>
                  <small className="muted">{t.assurance || t.status}</small>
                </li>
              ))}
            </ul>
          </aside>
        </div>
      )}

      {tab === "activity" && (
        <div className="glass activity">
          {!run.events.length && (
            <p className="evidence-empty">Session activity will appear here.</p>
          )}
          {[...run.events].reverse().map((e, i) => (
            <div key={i} className="event">
              <span className="when">
                {new Date(e.at * 1000).toLocaleTimeString()}
              </span>
              <span
                className="edot"
                style={{
                  background: e.provider
                    ? providerMeta(e.provider).color
                    : "#7b74b8",
                }}
              />
              <span>{e.message}</span>
            </div>
          ))}
        </div>
      )}
      </>}
    </div>
  );
}

function OpinionGrid({
  run,
  opinions,
  prefix,
  waiting,
  empty,
}: {
  run: Run;
  opinions: Opinion[];
  prefix: string;
  waiting: boolean;
  empty: string;
}) {
  const live = Object.entries(run.live)
    .filter(([k]) => k.startsWith(`${prefix}:`))
    .map(([k, v]) => ({ provider: k.slice(prefix.length + 1), text: v.text }));
  const providers = teamOrder(run).filter(
    (p) =>
      opinions.some((o) => o.provider === p) ||
      live.some((l) => l.provider === p),
  );
  if (!providers.length)
    return <p className="muted pulse">{waiting ? "Starting…" : empty}</p>;
  return (
    <div className="opinions">
      {providers.map((p) => {
        const done = opinions.find((o) => o.provider === p);
        const writing = live.find((l) => l.provider === p);
        return (
          <div
            key={p}
            className={`glass opinion ${writing ? "live" : ""}`}
            style={{ ["--c" as string]: providerMeta(p).color }}
          >
            <div className="opinion-head">
              <AgentChip id={p} />
              {writing ? (
                <small className="ok">
                  writing <span className="typing" />
                </small>
              ) : done?.error ? (
                <small className="error">failed</small>
              ) : (
                <small className="muted">{done?.model}</small>
              )}
            </div>
            {done?.error && <p className="error small">{done.error}</p>}
            {done && !done.error && !done.output && !writing && (
              <p className="error small">
                No answer text came back from this model.
              </p>
            )}
            <div className="opinion-body">
              <Markdown
                text={
                  prefix === "cross"
                    ? stripScores(writing ? writing.text : done?.output || "")
                    : writing
                      ? writing.text
                      : done?.output || ""
                }
              />
            </div>
            {!writing &&
              done?.scores &&
              Object.keys(done.scores).length > 0 && (
                <div className="opinion-scores small">
                  Scores given:{" "}
                  {teamOrder(run)
                    .filter((x) => done.scores![x] != null)
                    .map((x) => `${providerMeta(x).name} ${done.scores![x]}`)
                    .join(" · ")}
                </div>
              )}
          </div>
        );
      })}
    </div>
  );
}

/** Copies text and says so for a moment. */
function CopyButton({
  text,
  label,
  primary,
}: {
  text: string;
  label: string;
  primary?: boolean;
}) {
  const [state, setState] = useState<"" | "done" | "failed">("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setState("done");
    } catch {
      setState("failed");
    }
    setTimeout(() => setState(""), 1800);
  }
  return (
    <button className={`btn small ${primary ? "" : "ghost"}`} onClick={copy}>
      {state === "done" ? "Copied" : state === "failed" ? "Copy failed" : label}
    </button>
  );
}

/** The final code as one file to download or copy (solution.py on Project Eris runs). */
function SolutionFile({ run }: { run: Run }) {
  const sol = run.solution!;
  return (
    <div className="solution-file">
      <div className="solution-name">
        <span className="solution-kind">PY</span>
        <div>
          <b>{sol.name}</b>
          <small className="muted">
            {sol.lines} lines · generated code · check verification details
          </small>
        </div>
      </div>
      <div className="solution-actions">
        <a
          className="btn primary small"
          href={`/api/runs/${run.id}/solution`}
          download={sol.name}
        >
          Download {sol.name}
        </a>
        <CopyButton text={sol.code} label="Copy code" />
      </div>
    </div>
  );
}

function stripScores(text: string): string {
  const at = text.lastIndexOf("```json");
  if (at < 0 || !/"scores"|```json\s*$/.test(text.slice(at))) return text;
  return text
    .slice(0, at)
    .replace(/#+\s*Scores\s*$/i, "")
    .trimEnd();
}

function joinNames(names: string[]): string {
  return names.length < 2
    ? names.join("")
    : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

function compareState(run: Run, idx: number): "done" | "now" | "todo" {
  const now =
    run.status === "analyzing" ? 0 : run.status === "cross_checking" ? 1 : -1;
  if (now === idx) return "now";
  if (idx === 0) return run.analyses.length ? "done" : "todo";
  return run.cross_checks.length || run.plan ? "done" : "todo";
}

function Stage({
  label,
  state,
}: {
  label: string;
  state: "done" | "now" | "todo";
}) {
  return (
    <div className={`stage ${state}`}>
      <span className="stage-dot" />
      {label}
    </div>
  );
}

function stageState(run: Run, idx: number): "done" | "now" | "todo" {
  if (run.status === "analyzing" || run.status === "cross_checking")
    return "todo";
  const order = [
    "planning",
    "reviewing_plan",
    "awaiting_approval",
    "running",
    "integrating",
    "completed",
  ];
  let cur = order.indexOf(run.status);
  if (cur < 0) {
    // Terminal but not completed: show progress reached.
    cur = run.final ? 5 : run.plan_approved ? 3 : run.plan ? 2 : 0;
    return idx < cur ? "done" : "todo";
  }
  if (run.status === "running" && !run.require_plan_approval && idx === 2)
    return "done";
  if (run.status === "integrating" && run.final && !run.live.final)
    return idx <= 4 ? "done" : "now";
  if (idx < cur || run.status === "completed") return "done";
  return idx === cur ? "now" : "todo";
}

function List({ title, items }: { title: string; items: string[] }) {
  if (!items?.length) return null;
  return (
    <div className="kv">
      <span>{title}</span>
      <ul>
        {items.map((x, i) => (
          <li key={i}>{x}</li>
        ))}
      </ul>
    </div>
  );
}

function TaskCard({
  task,
  live,
  open,
  toggle,
}: {
  task: Task;
  live?: string;
  open: boolean;
  toggle: () => void;
}) {
  const working = ["working", "reviewing", "revising"].includes(task.status);
  const color = providerMeta(
    task.status === "reviewing" ? task.reviewer : task.implementer,
  ).color;
  return (
    <div
      className={`glass task ${working ? "live" : ""} t-${task.status}`}
      style={{ ["--c" as string]: color || "#8f7dff" }}
    >
      <button className="task-head" onClick={toggle} aria-expanded={open}>
        <StatusBadge status={task.status} />
        <h4>{task.title}</h4>
      </button>
      <div className="task-agents">
        <AgentChip id={task.implementer} label="does" />{" "}
        <AgentChip id={task.reviewer} label="checks" />
      </div>
      {live && (
        <div className="task-live">
          <Markdown text={live.slice(-900)} />
        </div>
      )}
      {open && (
        <div className="task-body">
          <p className="muted">{task.description}</p>
          {task.depends_on.length > 0 && (
            <p className="small muted">Needs: {task.depends_on.join(", ")}</p>
          )}
          {task.route_reason && (
            <p className="small muted">Why these models: {task.route_reason}</p>
          )}
          {task.attempts.map((a, i) => (
            <div key={i} className="attempt">
              <div className="attempt-head">
                <AgentChip id={a.provider} />{" "}
                {a.is_revision ? "revision" : "attempt"}{" "}
                <small className="muted">{a.model}</small>
              </div>
              {a.error && <p className="error">{a.error}</p>}
              {a.output && (
                <details>
                  <summary>Show output</summary>
                  <Markdown text={a.output} />
                </details>
              )}
              {a.review && (
                <div className="review-box">
                  <AgentChip id={a.review.reviewer} />:{" "}
                  <b>{a.review.verdict}</b>
                  {a.review.score != null && ` · ${a.review.score}/10`}
                  <ul>
                    {a.review.findings.map((f, j) => (
                      <li key={j}>{f}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Group tasks into dependency layers so parallel work sits side by side. */
function layerTasks(tasks: Task[]): Task[][] {
  const depth = new Map<string, number>();
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const visit = (t: Task, seen: Set<string>): number => {
    if (depth.has(t.id)) return depth.get(t.id)!;
    if (seen.has(t.id)) return 0;
    seen.add(t.id);
    const d = t.depends_on.length
      ? 1 +
        Math.max(
          ...t.depends_on.map((x) =>
            byId.get(x) ? visit(byId.get(x)!, seen) : 0,
          ),
        )
      : 0;
    depth.set(t.id, d);
    return d;
  };
  tasks.forEach((t) => visit(t, new Set()));
  const layers: Task[][] = [];
  tasks.forEach((t) => {
    const d = depth.get(t.id) || 0;
    (layers[d] ||= []).push(t);
  });
  return layers.filter(Boolean);
}

function lastRound(judges: Judgement[]): Judgement[] {
  if (!judges?.length) return [];
  const last = Math.max(...judges.map((j) => j.round));
  return judges.filter((j) => j.round === last);
}

function currentJudges(run: Run): Judgement[] {
  return (run.final?.judges || []).filter(
    (judge) =>
      !judge.artifact_hash ||
      !run.final?.artifact_hash ||
      judge.artifact_hash === run.final.artifact_hash,
  );
}

function JudgeList({
  judges,
  currentHash,
}: {
  judges: Judgement[];
  currentHash?: string;
}) {
  if (!judges?.length) return null;
  const rounds = [...new Set(judges.map((j) => j.round))].sort((a, b) => b - a);
  const currentRounds = judges
    .filter(
      (j) =>
        !j.artifact_hash || !currentHash || j.artifact_hash === currentHash,
    )
    .map((j) => j.round);
  const latestCurrentRound = currentRounds.length
    ? Math.max(...currentRounds)
    : -1;
  return (
    <details className="judges" style={{ marginTop: 18 }}>
      <summary>Model review details · {judges.length}</summary>
      {rounds.map((r) => (
        <div key={r} className="judge-round">
          <span className="eyebrow">
            {r === 0 ? "First model review" : "Review after revisions"}
          </span>
          {judges
            .filter((j) => j.round === r)
            .map((j) => (
              <div key={j.judge} className="judge">
                <div className="judge-head">
                  <AgentChip id={j.judge} />
                  <b>{j.score != null ? `${j.score}/10` : "no score"}</b>
                </div>
                {j.artifact_hash &&
                  currentHash &&
                  j.artifact_hash !== currentHash && (
                    <p
                      className="small warn-text"
                      style={{ margin: "7px 0 0" }}
                    >
                      Review of an earlier result
                    </p>
                  )}
                {(!j.artifact_hash ||
                  !currentHash ||
                  j.artifact_hash === currentHash) &&
                  j.round < latestCurrentRound && (
                    <p className="small muted" style={{ margin: "7px 0 0" }}>
                      Earlier review · superseded by a later review round
                    </p>
                  )}
                {j.must_fix.length > 0 && (
                  <ul className="must-fix">
                    {j.must_fix.map((m, i) => (
                      <li key={i}>{m}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
        </div>
      ))}
    </details>
  );
}

function Scoreboard({ run }: { run: Run }) {
  const c = run.consensus;
  if (!c) return null;
  const ranked = Object.entries(c.average).sort((a, b) => b[1] - a[1]);
  const name = (p: string) => providerMeta(p).name;
  return (
    <div className="glass scoreboard">
      <h2>Approach comparison</h2>
      <p className="muted small">
        These scores record model preferences. The selected approach guides the
        work; source evidence and explicit checks are shown separately and may
        contradict these opinions.
      </p>
      {ranked.map(([p, avg]) => {
        const from = Object.entries(c.votes)
          .filter(([j, v]) => j !== p && v[p] != null)
          .map(([j, v]) => `${name(j)} ${v[p]}`);
        return (
          <div
            key={p}
            className={p === c.best ? "score-row best" : "score-row"}
            style={{ ["--c" as string]: providerMeta(p).color }}
          >
            <AgentChip id={p} />
            <span className="score-bar">
              <i style={{ width: `${Math.max(4, avg * 10)}%` }} />
            </span>
            <b>{avg.toFixed(1)}</b>
            <small className="muted">
              {from.length ? `from ${from.join(" · ")}` : "own score"}
            </small>
            {p === c.best && (
              <span className="badge best">Chosen as the base</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function EvidencePanel({ run }: { run: Run }) {
  const sources = run.evidence || [];
  const checks = run.final?.checks || [];
  const latestCurrentJudges = lastRound(currentJudges(run));
  const currentReviewRounds = latestCurrentJudges.map((j) => j.round);
  const latestReviewRound = currentReviewRounds.length
    ? Math.max(...currentReviewRounds)
    : null;
  const suggestions = useMemo(
    () =>
      run.search_entry_html
        ? DOMPurify.sanitize(run.search_entry_html, {
            FORBID_TAGS: ["script", "iframe", "object", "embed", "form"],
            FORBID_ATTR: ["srcdoc"],
          })
        : "",
    [run.search_entry_html],
  );
  const status = run.final?.verification_status || "unverified";
  const statusText: Record<string, string> = {
    unverified: "Not independently verified",
    reviewed: "Model reviewed",
    partial: "Some checks completed",
    failed: "Checks found an issue",
  };
  const kindText: Record<string, string> = {
    python_syntax: "Python syntax",
    execution: "Code execution",
    citations: "Citation coverage",
    artifact_hash: "Artifact fingerprint",
    model_review: "Model review",
  };
  function sourceDate(value: string | number) {
    const d = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(d.valueOf())
      ? ""
      : d.toLocaleDateString(undefined, {
          day: "numeric",
          month: "short",
          year: "numeric",
        });
  }
  function safeUrl(value: string) {
    try {
      const url = new URL(value);
      return ["https:", "http:"].includes(url.protocol) ? url.href : null;
    } catch {
      return null;
    }
  }
  return (
    <div className="evidence-layout">
      <section className="glass evidence-panel">
        <h2>Sources & research</h2>
        <p>
          Inspect the material behind the answer. A source link alone does not
          prove a claim.
        </p>
        {run.research_status && run.research_status !== "not_started" && (
          <span className="badge">
            Research: {run.research_status.replaceAll("_", " ")}
          </span>
        )}
        {run.research_summary && (
          <details style={{ marginTop: 16 }}>
            <summary>Read research notes</summary>
            <div style={{ marginTop: 14 }}>
              <Markdown text={run.research_summary} />
            </div>
          </details>
        )}
        {run.search_queries && run.search_queries.length > 0 && (
          <details style={{ marginTop: 12 }}>
            <summary>Search queries · {run.search_queries.length}</summary>
            <ul className="small muted">
              {run.search_queries.map((q, i) => (
                <li key={i}>{q}</li>
              ))}
            </ul>
          </details>
        )}
        {!sources.length && (
          <div className="evidence-empty">
            {run.research_status === "collecting"
              ? "Gathering source evidence…"
              : "No external sources were recorded for this session."}
          </div>
        )}
        {sources.map((source, i) => {
          const url = safeUrl(source.url);
          return (
            <article className="source-card" key={source.id || i}>
              <div className="source-heading">
                <span className="source-number">{i + 1}</span>
                <span>
                  {source.source_kind === "google_search"
                    ? "Google Search grounding"
                    : source.source_kind === "web"
                      ? "Retrieved web page"
                      : source.source_kind === "attachment"
                        ? "Supplied attachment"
                        : source.source_kind}
                </span>
              </div>
              {url ? (
                <a href={url} target="_blank" rel="noreferrer noopener">
                  {source.title || source.url} ↗
                </a>
              ) : (
                <strong>{source.title || "Source"}</strong>
              )}
              {source.excerpt && (
                <>
                  <small>
                    {source.source_kind === "google_search"
                      ? "Source-backed model summary · not an original source quotation"
                      : source.source_kind === "attachment"
                        ? "Supplied attachment excerpt"
                        : "Retrieved page excerpt"}
                  </small>
                  <p>{source.excerpt}</p>
                </>
              )}
              {source.truncated && (
                <small>
                  Partial source · the retrieved content was truncated.
                </small>
              )}
              <small>
                {sourceDate(source.retrieved_at)}
                {url ? " · " + new URL(url).hostname : ""}
              </small>
            </article>
          );
        })}
        {suggestions && (
          <div style={{ marginTop: 20 }}>
            <h3>Google search suggestions</h3>
            <iframe
              title="Google search suggestions"
              className="search-suggestions"
              sandbox=""
              referrerPolicy="no-referrer"
              srcDoc={suggestions}
            />
          </div>
        )}
      </section>
      <section className="glass evidence-panel">
        <h2>Verification</h2>
        <span className={"badge " + (status === "failed" ? "s-failed" : "")}>
          {statusText[status] || status}
        </span>
        <p style={{ marginTop: 13 }}>
          These checks apply to the recorded answer or code artifact. Syntax
          checks do not run code; citation coverage does not establish factual
          support.
        </p>
        {!checks.length && (
          <div className="evidence-empty">
            {run.final
              ? "No independent check records are available for this result."
              : "Check results will appear after an answer is produced."}
          </div>
        )}
        {checks.map((check, i) => {
          const answerScoped =
            check.kind === "citations" || check.kind === "model_review";
          const stale =
            answerScoped &&
            check.artifact_hash &&
            run.final?.artifact_hash &&
            check.artifact_hash !== run.final.artifact_hash;
          const roundMatch = /^review:[^:]+:(\d+):/.exec(check.id);
          const earlierReview =
            check.kind === "model_review" &&
            latestReviewRound !== null &&
            roundMatch !== null &&
            Number(roundMatch[1]) < latestReviewRound;
          return (
            <article key={check.id + "-" + i} className="check-item">
              <div>
                <span
                  className={
                    "check-status " +
                    (stale || earlierReview ? "" : check.status)
                  }
                >
                  {stale
                    ? "Earlier version"
                    : earlierReview
                      ? "Earlier review"
                      : check.status === "unknown"
                        ? "Not verified"
                        : check.status}
                </span>
                {kindText[check.kind] || check.kind.replaceAll("_", " ")}
              </div>
              <p>{check.summary}</p>
              {check.artifact_hash && (
                <details>
                  <summary>Artifact fingerprint</summary>
                  <code className="hash">{check.artifact_hash}</code>
                </details>
              )}
            </article>
          );
        })}
        {run.final?.artifact_hash && (
          <details style={{ marginTop: 19 }}>
            <summary>Final answer fingerprint</summary>
            <code className="hash">{run.final.artifact_hash}</code>
          </details>
        )}
        {run.final && (
          <div style={{ marginTop: 25 }}>
            <List title="Known gaps" items={run.final.gaps} />
            <List title="Limitations" items={run.final.limitations} />
          </div>
        )}
      </section>
    </div>
  );
}
