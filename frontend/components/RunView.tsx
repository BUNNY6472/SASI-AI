"use client";

import { useEffect, useMemo, useState } from "react";
import { api, fileSize, Opinion, PROVIDER_META, Provider, Run, Task } from "@/lib/api";
import Markdown from "./Markdown";
import { AgentChip, StatusBadge, fmt } from "./ui";

const LIVE = new Set(["analyzing", "cross_checking", "planning", "reviewing_plan", "awaiting_approval", "running", "integrating"]);
const ORDER = ["claude", "chatgpt", "gemini"];
const FIELD_NAME: Record<string, string> = { software: "Software engineering", research: "Research and analysis", general: "General" };
const TERMINAL = new Set(["completed", "failed", "cancelled", "budget_stopped", "interrupted"]);

export default function RunView({ id, onRun, back }: { id: string; onRun: (r: Run | null) => void; back: () => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"compare" | "plan" | "result" | "activity">("plan");
  const [openTask, setOpenTask] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stream, setStream] = useState(0);

  useEffect(() => {
    let es: EventSource | null = null;
    let stopped = false;
    const connect = () => {
      es = new EventSource(`/api/runs/${id}/events`);
      es.onmessage = (m) => {
        const r = JSON.parse(m.data) as Run;
        setRun(r);
      };
      es.onerror = () => {
        es?.close();
        // Reconnect while the run is live (Cloud Run can drop long streams).
        if (!stopped) setTimeout(() => api<Run>(`/api/runs/${id}`).then((r) => { setRun(r); if (LIVE.has(r.status)) connect(); }).catch((e) => setError(e.message)), 1500);
      };
    };
    connect();
    return () => {
      stopped = true;
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
    if (!s) return;
    if (s === "analyzing" || s === "cross_checking") setTab("compare");
    else if (["planning", "reviewing_plan", "awaiting_approval", "running"].includes(s)) setTab("plan");
    else if (s === "integrating" || s === "completed") setTab("result");
    else if (!run?.plan && run?.compare_all) setTab("compare");
  }, [run?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  async function remove() {
    if (!window.confirm("Delete this run? Its files and every model's results are removed from BigQuery. This cannot be undone.")) return;
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
      const r = await api<Run | { ok: boolean }>(`/api/runs/${id}/${path}`, { method: "POST", body: "{}" });
      if ("id" in r) setRun(r);
      if (path === "resume" || path === "approve") setStream((n) => n + 1);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const layers = useMemo(() => (run?.plan ? layerTasks(run.plan.tasks) : []), [run?.plan]);

  if (!run) return <div className="loading">{error || "Loading run…"}</div>;
  const live = LIVE.has(run.status);
  const total = run.plan?.tasks.length || 0;
  const done = run.plan?.tasks.filter((t) => t.status === "accepted").length || 0;

  return (
    <div className="runview">
      <div className="run-head">
        <button className="btn ghost" onClick={back}>← Workspace</button>
        <div className="run-title">
          <StatusBadge status={run.status} />
          {run.simulated && <span className="badge demo">Test agents</span>}
          <span className="badge field" title="Chosen automatically from your request">{FIELD_NAME[run.profile] || run.profile}</span>
          <h1>{run.request}</h1>
        </div>
        <div className="run-actions">
          {run.status === "awaiting_approval" && (
            <button className="btn primary" disabled={busy} onClick={() => action("approve")}>Approve plan and start</button>
          )}
          {live && <button className="btn danger" disabled={busy} onClick={() => action("cancel")}>Cancel</button>}
          {["interrupted", "budget_stopped", "failed", "cancelled"].includes(run.status) && (run.plan || run.analyses.length > 0) && (
            <button className="btn" disabled={busy} onClick={() => action("resume")}>Resume from checkpoint</button>
          )}
          {TERMINAL.has(run.status) && (
            <button className="btn danger" disabled={busy} onClick={remove} title="Remove the run, its files and results from BigQuery">Delete</button>
          )}
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {run.error && <div className="notice warn">{run.error}</div>}

      {run.files.length > 0 && (
        <div className="file-strip">
          {run.files.map((f) => (
            <a key={f.id} className="file-chip glass" href={`/api/runs/${run.id}/files/${f.id}`} target="_blank" rel="noreferrer" title={f.name}>
              {f.kind === "image" ? <img src={`/api/runs/${run.id}/files/${f.id}`} alt="" /> : <span className="file-kind">{f.kind}</span>}
              <span className="file-name">{f.name}</span>
              <small className="muted">{fileSize(f.size)}</small>
            </a>
          ))}
        </div>
      )}

      <div className="progress-strip glass">
        {run.compare_all && <Stage label="Analyse" state={compareState(run, 0)} />}
        {run.compare_all && <Stage label="Compare" state={compareState(run, 1)} />}
        <Stage label="Plan" state={stageState(run, 0)} />
        <Stage label="Review plan" state={stageState(run, 1)} />
        <Stage label="Approve" state={stageState(run, 2)} />
        <Stage label={`Work ${done}/${total}`} state={stageState(run, 3)} />
        <Stage label="Integrate" state={stageState(run, 4)} />
        <Stage label="Deliver" state={stageState(run, 5)} />
        <div className="usage">
          {fmt(run.usage.calls)} calls · {fmt(run.usage.input_tokens + run.usage.output_tokens)} tokens
        </div>
      </div>

      <div className="tabs">
        {run.compare_all && <button className={tab === "compare" ? "tab on" : "tab"} onClick={() => setTab("compare")}>Side by side</button>}
        <button className={tab === "plan" ? "tab on" : "tab"} onClick={() => setTab("plan")}>Plan and tasks</button>
        <button className={tab === "result" ? "tab on" : "tab"} onClick={() => setTab("result")} disabled={!run.final && !run.live.final}>Result and evidence</button>
        <button className={tab === "activity" ? "tab on" : "tab"} onClick={() => setTab("activity")}>Activity</button>
      </div>

      {tab === "compare" && (
        <div className="compare">
          <h2>Independent analyses</h2>
          <p className="muted small">Each model studied your request{run.files.length ? " and files" : ""} on its own, at the same time.</p>
          <OpinionGrid run={run} opinions={run.analyses} prefix="analysis" waiting={run.status === "analyzing"} empty="Waiting to start…" />
          {(run.cross_checks.length > 0 || run.status === "cross_checking") && (
            <>
              <h2>Cross-checks</h2>
              <p className="muted small">Each model read the others&apos; analyses and compared them with its own: agreements, disagreements and mistakes.</p>
              <OpinionGrid run={run} opinions={run.cross_checks} prefix="cross" waiting={run.status === "cross_checking"} empty="Waiting for the analyses…" />
            </>
          )}
        </div>
      )}

      {tab === "result" && !run.final && run.live.final && (
        <article className="glass result">
          <div className="result-meta"><AgentChip id={run.live.final.provider} /> is writing the final result <span className="typing" /></div>
          <Markdown text={run.live.final.text} />
        </article>
      )}

      {tab === "plan" && (
        <div className="plan-grid">
          <div className="glass plan-card">
            {!run.plan && <p className="muted pulse">The planner is reading your request…</p>}
            {run.plan && (
              <>
                <div className="kv"><span>Goal</span><p>{run.plan.goal}</p></div>
                <div className="kv"><span>Planned by</span><p><AgentChip id={run.plan.planner} /> <small className="muted">v{run.plan.version}</small></p></div>
                <List title="Success criteria" items={run.plan.success_criteria} />
                <List title="Assumptions" items={run.plan.assumptions} />
                <List title="Deliverables" items={run.plan.deliverables} />
                {run.plan_review && (
                  <div className="review-box">
                    <div><AgentChip id={run.plan_review.reviewer} /> reviewed the plan: <b>{run.plan_review.verdict}</b>{run.plan_review.score != null && ` · ${run.plan_review.score}/10`}</div>
                    <ul>{run.plan_review.findings.map((f, i) => <li key={i}>{f}</li>)}</ul>
                  </div>
                )}
              </>
            )}
          </div>
          <div className="graph">
            {layers.map((layer, i) => (
              <div key={i} className="layer">
                <div className="layer-label">{i === 0 ? "Starts first" : `Step ${i + 1}`}{layer.length > 1 ? " · parallel" : ""}</div>
                <div className="layer-tasks">
                  {layer.map((t) => (
                    <TaskCard key={t.id} task={t} live={run.live[`task:${t.id}`]?.text} open={openTask === t.id} toggle={() => setOpenTask(openTask === t.id ? null : t.id)} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === "result" && run.final && (
        <div className="result-grid">
          <article className="glass result">
            <div className="result-meta">
              Integrated by <AgentChip id={run.final.integrator} /> · evaluated by <AgentChip id={run.final.evaluator} />
              {run.final.score != null && <span className="score">{run.final.score}/10</span>}
              <button className="btn ghost small" onClick={() => navigator.clipboard.writeText(run.final!.output)}>Copy</button>
            </div>
            <Markdown text={run.final.output} />
          </article>
          <aside className="glass evidence">
            <h3>Evaluation</h3>
            <p className={run.final.meets_criteria ? "ok" : "warn-text"}>
              {run.final.meets_criteria ? "Meets the agreed criteria" : "Does not fully meet the criteria"}
            </p>
            <List title="Gaps" items={run.final.gaps} />
            <List title="Limitations" items={run.final.limitations} />
            <h3>Who did what</h3>
            <ul className="who">
              {run.plan?.tasks.map((t) => (
                <li key={t.id}>
                  <span>{t.title}</span>
                  <span><AgentChip id={t.implementer} /> → <AgentChip id={t.reviewer} /></span>
                  <small className="muted">{t.assurance || t.status}</small>
                </li>
              ))}
            </ul>
            <h3>Usage</h3>
            <ul className="who">
              {Object.entries(run.usage.by_provider).map(([p, u]) => (
                <li key={p}><AgentChip id={p} /><small>{u.calls} calls · {fmt(u.input_tokens)} in · {fmt(u.output_tokens)} out</small></li>
              ))}
            </ul>
          </aside>
        </div>
      )}

      {tab === "activity" && (
        <div className="glass activity">
          {[...run.events].reverse().map((e, i) => (
            <div key={i} className="event">
              <span className="when">{new Date(e.at * 1000).toLocaleTimeString()}</span>
              <span className="edot" style={{ background: PROVIDER_META[e.provider as Provider]?.color || "#7b74b8" }} />
              <span>{e.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function OpinionGrid({ run, opinions, prefix, waiting, empty }: { run: Run; opinions: Opinion[]; prefix: string; waiting: boolean; empty: string }) {
  const live = Object.entries(run.live).filter(([k]) => k.startsWith(`${prefix}:`)).map(([k, v]) => ({ provider: k.slice(prefix.length + 1), text: v.text }));
  const providers = ORDER.filter((p) => opinions.some((o) => o.provider === p) || live.some((l) => l.provider === p));
  if (!providers.length) return <p className="muted pulse">{waiting ? "Starting…" : empty}</p>;
  return (
    <div className="opinions">
      {providers.map((p) => {
        const done = opinions.find((o) => o.provider === p);
        const writing = live.find((l) => l.provider === p);
        return (
          <div key={p} className={`glass opinion ${writing ? "live" : ""}`} style={{ ["--c" as string]: PROVIDER_META[p as Provider]?.color }}>
            <div className="opinion-head">
              <AgentChip id={p} />
              {writing ? <small className="ok">writing <span className="typing" /></small> : done?.error ? <small className="error">failed</small> : <small className="muted">{done?.model}</small>}
            </div>
            {done?.error && <p className="error small">{done.error}</p>}
            <div className="opinion-body"><Markdown text={writing ? writing.text : done?.output || ""} /></div>
          </div>
        );
      })}
    </div>
  );
}

function compareState(run: Run, idx: number): "done" | "now" | "todo" {
  const now = run.status === "analyzing" ? 0 : run.status === "cross_checking" ? 1 : -1;
  if (now === idx) return "now";
  if (idx === 0) return run.analyses.length ? "done" : "todo";
  return run.cross_checks.length || run.plan ? "done" : "todo";
}

function Stage({ label, state }: { label: string; state: "done" | "now" | "todo" }) {
  return <div className={`stage ${state}`}><span className="stage-dot" />{label}</div>;
}

function stageState(run: Run, idx: number): "done" | "now" | "todo" {
  if (run.status === "analyzing" || run.status === "cross_checking") return "todo";
  const order = ["planning", "reviewing_plan", "awaiting_approval", "running", "integrating", "completed"];
  let cur = order.indexOf(run.status);
  if (cur < 0) {
    // Terminal but not completed: show progress reached.
    cur = run.final ? 5 : run.plan_approved ? 3 : run.plan ? 2 : 0;
    return idx < cur ? "done" : "todo";
  }
  if (run.status === "running" && !run.require_plan_approval && idx === 2) return "done";
  if (idx < cur || run.status === "completed") return "done";
  return idx === cur ? "now" : "todo";
}

function List({ title, items }: { title: string; items: string[] }) {
  if (!items?.length) return null;
  return (
    <div className="kv">
      <span>{title}</span>
      <ul>{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </div>
  );
}

function TaskCard({ task, live, open, toggle }: { task: Task; live?: string; open: boolean; toggle: () => void }) {
  const working = ["working", "reviewing", "revising"].includes(task.status);
  const color = PROVIDER_META[(task.status === "reviewing" ? task.reviewer : task.implementer) as Provider]?.color;
  return (
    <div className={`glass task ${working ? "live" : ""} t-${task.status}`} style={{ ["--c" as string]: color || "#8f7dff" }}>
      <button className="task-head" onClick={toggle}>
        <StatusBadge status={task.status} />
        <h4>{task.title}</h4>
      </button>
      <div className="task-agents">
        <AgentChip id={task.implementer} label="does" /> <AgentChip id={task.reviewer} label="checks" />
      </div>
      {live && <div className="task-live"><Markdown text={live.slice(-900)} /></div>}
      {open && (
        <div className="task-body">
          <p className="muted">{task.description}</p>
          {task.depends_on.length > 0 && <p className="small muted">Needs: {task.depends_on.join(", ")}</p>}
          {task.route_reason && <p className="small muted">Why these models: {task.route_reason}</p>}
          {task.attempts.map((a, i) => (
            <div key={i} className="attempt">
              <div className="attempt-head">
                <AgentChip id={a.provider} /> {a.is_revision ? "revision" : "attempt"} <small className="muted">{a.model}</small>
              </div>
              {a.error && <p className="error">{a.error}</p>}
              {a.output && <details><summary>Show output</summary><Markdown text={a.output} /></details>}
              {a.review && (
                <div className="review-box">
                  <AgentChip id={a.review.reviewer} />: <b>{a.review.verdict}</b>{a.review.score != null && ` · ${a.review.score}/10`}
                  <ul>{a.review.findings.map((f, j) => <li key={j}>{f}</li>)}</ul>
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
    const d = t.depends_on.length ? 1 + Math.max(...t.depends_on.map((x) => (byId.get(x) ? visit(byId.get(x)!, seen) : 0))) : 0;
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
