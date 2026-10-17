"use client";

import { DragEvent, FormEvent, useEffect, useRef, useState } from "react";
import {
  api,
  fileSize,
  ProviderStatus,
  Run,
  RunSummary,
  uploadFile,
} from "@/lib/api";
import { AgentChip, StatusBadge, fmt, timeAgo } from "./ui";
import Icon from "./Icon";

interface Attachment {
  key: string;
  name: string;
  size: number;
  progress: number;
  id?: string;
  error?: string;
}
const EXAMPLES = [
  {
    icon: "search",
    title: "Explore a complex question",
    description: "Research with sources and trade-offs",
    text: "Compare solar and wind power for a small farm in Andhra Pradesh. Find current primary sources, calculate indicative payback, and explain assumptions, costs and risks.",
  },
  {
    icon: "code",
    title: "Build something useful",
    description: "Turn requirements into a solution",
    text: "Design a small FastAPI to-do API with persistence, input validation and tests. Explain how to run it and clearly distinguish reviewed code from executed tests.",
  },
  {
    icon: "chart",
    title: "Make sense of your data",
    description: "Find patterns and explain the evidence",
    text: "Analyze the data I attach. Start with data quality and missing values, identify the most useful patterns, and show reproducible calculations with limitations.",
  },
];
type Mode = "quick" | "adaptive" | "deep";
const MODES: { key: Mode; name: string; description: string }[] = [
  { key: "quick", name: "Quick", description: "A focused path to an answer" },
  {
    key: "adaptive",
    name: "Adaptive",
    description: "Match the work to your question",
  },
  {
    key: "deep",
    name: "Deep research",
    description: "More exploration and comparison",
  },
];

export default function Workspace({
  open,
  needsKeys,
  providers,
  demoMode = false,
}: {
  open: (id: string) => void;
  needsKeys: boolean;
  providers: ProviderStatus[];
  demoMode?: boolean;
}) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState("");
  const [request, setRequest] = useState("");
  const [materials, setMaterials] = useState("");
  const [showMaterials, setShowMaterials] = useState(false);
  const [approval, setApproval] = useState(false);
  const [compare, setCompare] = useState(false);
  const [eris, setEris] = useState(false);
  const [mode, setMode] = useState<Mode>("adaptive");
  const [minutes, setMinutes] = useState(30);
  const [files, setFiles] = useState<Attachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const question = useRef<HTMLTextAreaElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const removed = useRef(new Set<string>());
  const uploading = files.some((f) => !f.id && !f.error);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    try {
      setEris(localStorage.getItem("sasi.eris") === "1");
    } catch {}
  }, []);
  function switchEris(on: boolean) {
    setEris(on);
    try {
      localStorage.setItem("sasi.eris", on ? "1" : "0");
    } catch {}
  }
  useEffect(() => {
    const load = () =>
      api<RunSummary[]>("/api/runs")
        .then((r) => {
          setRuns(r);
          setHistoryError("");
        })
        .catch(() => setHistoryError("Recent research couldn’t be loaded."))
        .finally(() => setHistoryLoading(false));
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  function addFiles(list: FileList | null) {
    if (!list) return;
    const room = 10 - files.length;
    Array.from(list)
      .slice(0, Math.max(0, room))
      .forEach((file) => {
        const key = file.name + "-" + file.size + "-" + Math.random();
        if (file.size > 25 * 1024 * 1024) {
          setError(file.name + " is larger than 25 MB.");
          return;
        }
        setFiles((fs) => [
          ...fs,
          { key, name: file.name, size: file.size, progress: 0 },
        ]);
        const patch = (p: Partial<Attachment>) =>
          setFiles((fs) => fs.map((f) => (f.key === key ? { ...f, ...p } : f)));
        uploadFile(file, (progress) => patch({ progress }))
          .then((ref) => {
            if (removed.current.has(key)) {
              api("/api/files/" + ref.id, { method: "DELETE" }).catch(() => {});
              return;
            }
            patch({ id: ref.id, progress: 100 });
          })
          .catch((err: Error) => patch({ error: err.message }));
      });
    if (list.length > room)
      setError("You can attach up to 10 files per research session.");
  }
  function removeFile(a: Attachment) {
    removed.current.add(a.key);
    setFiles((fs) => fs.filter((f) => f.key !== a.key));
    if (a.id) api("/api/files/" + a.id, { method: "DELETE" }).catch(() => {});
  }
  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || uploading || needsKeys || request.trim().length < 3) return;
    setBusy(true);
    setError("");
    try {
      const run = await api<Run>("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          request: request.trim(),
          materials,
          profile: "auto",
          eris,
          mode,
          time_limit_minutes: minutes,
          require_plan_approval: approval,
          compare_all: compare ? true : null,
          file_ids: files.filter((f) => f.id).map((f) => f.id),
        }),
      });
      setFiles([]);
      open(run.id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const available = providers.filter((p) => p.connected && p.ok !== false);

  return (
    <div className="workspace">
      <section className="hero">
        <div className="eyebrow hero-eyebrow">
          <span className="tiny-star">✳</span> A LITTLE CURIOSITY. A BIGGER
          POSSIBILITY.
        </div>
        <h1>
          Bring your hardest question.
          <br />
          <span>Give it room to unfold.</span>
        </h1>
        <p className="sub">
          A focused space for research, code and complex problems.
          <br className="desktop-only" /> Bring together capable models, your
          context and evidence you can inspect.
        </p>
      </section>
      {demoMode && (
        <div className="notice warn" role="status">
          <strong>Preview mode · simulated models.</strong> Results are
          demonstrations, not real model answers.
        </div>
      )}
      {needsKeys && (
        <div className="notice keys-cta">
          <div>
            <strong>Connect a model to begin.</strong>
            <p>
              Add an available provider in Connections, then return to your
              question.
            </p>
          </div>
          <a className="btn primary small" href="#/settings">
            Open connections <Icon name="arrow" size={16} />
          </a>
        </div>
      )}
      <form
        ref={form}
        className={
          "composer glass " +
          (dragging ? "dragging " : "") +
          (eris ? "eris-on" : "")
        }
        onSubmit={submit}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <div className="composer-heading">
          <Icon name="spark" size={20} />
          <label htmlFor="research-question">
            What would you like to work on?
          </label>
          <span className="small muted">YOUR NEXT IDEA STARTS HERE</span>
        </div>
        <textarea
          id="research-question"
          ref={question}
          value={request}
          onChange={(e) => setRequest(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              form.current?.requestSubmit();
            }
          }}
          placeholder={
            eris
              ? "Share the Eris challenge, its rules and your data files…"
              : "Ask a question, describe a challenge, or explore an idea…"
          }
          rows={4}
          maxLength={20000}
          required
          minLength={3}
        />
        {showMaterials && (
          <div className="notes-field">
            <label htmlFor="research-materials">Reference notes</label>
            <textarea
              id="research-materials"
              className="materials"
              value={materials}
              onChange={(e) => setMaterials(e.target.value)}
              placeholder="Requirements, background, constraints or reference material…"
              rows={4}
              maxLength={60000}
            />
          </div>
        )}
        {files.length > 0 && (
          <div className="attachments">
            {files.map((f) => (
              <div key={f.key} className={"attach " + (f.error ? "err" : "")}>
                <Icon name="file" size={16} />
                <span className="attach-name">{f.name}</span>
                <small>
                  {f.error || (f.id ? fileSize(f.size) : f.progress + "%")}
                </small>
                {!f.id && !f.error && (
                  <i
                    className="attach-bar"
                    style={{ width: f.progress + "%" }}
                  />
                )}
                <button
                  type="button"
                  className="attach-x"
                  aria-label={"Remove " + f.name}
                  onClick={() => removeFile(f)}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="composer-tools">
          <div>
            <input
              ref={picker}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <button
              type="button"
              className="btn ghost small"
              onClick={() => picker.current?.click()}
            >
              <Icon name="clip" size={17} /> Attach files
            </button>
            <button
              type="button"
              className="btn ghost small"
              aria-expanded={showMaterials}
              onClick={() => setShowMaterials((v) => !v)}
            >
              <Icon name="plus" size={17} />{" "}
              {showMaterials ? "Hide notes" : "Add context"}
            </button>
          </div>
          <span className="small muted file-hint">
            Or drop files here · 25 MB each
          </span>
        </div>
        <div className="composer-bottom">
          <div className="mode-picker" role="group" aria-label="Research mode">
            {MODES.map((m) => (
              <button
                type="button"
                key={m.key}
                className={mode === m.key ? "selected" : ""}
                aria-pressed={mode === m.key}
                title={m.description}
                onClick={() => setMode(m.key)}
              >
                {m.key === "adaptive" && <Icon name="spark" size={14} />}
                {m.name}
              </button>
            ))}
          </div>
          <label className="time-picker">
            <Icon name="clock" size={16} />
            <span className="sr-only">Maximum research time</span>
            <select
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value))}
            >
              {[5, 15, 30, 60].map((m) => (
                <option key={m} value={m}>
                  Up to {m} min
                </option>
              ))}
            </select>
          </label>
          <button
            className="btn primary start-btn"
            disabled={
              busy || uploading || needsKeys || request.trim().length < 3
            }
          >
            {busy ? "Starting…" : uploading ? "Uploading…" : "Start research"}
            <Icon name="arrow" size={17} />
          </button>
        </div>
        <div className="mode-description">
          <span>
            {MODES.find((m) => m.key === mode)?.description}. Finishes when the
            work is ready.
          </span>
          <span className="keyboard-hint">Ctrl / ⌘ ↵</span>
        </div>
        <details className="advanced-options">
          <summary>Research preferences</summary>
          <div className="preferences-grid">
            <label className="toggle">
              <input
                type="checkbox"
                checked={approval}
                onChange={(e) => setApproval(e.target.checked)}
              />
              <span>Approve the plan before work begins</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={compare}
                onChange={(e) => setCompare(e.target.checked)}
              />
              <span>Always compare independent approaches</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={eris}
                onChange={(e) => switchEris(e.target.checked)}
              />
              <span>Project Eris · guidebook and dry-run instructions</span>
            </label>
          </div>
        </details>
        {error && (
          <p className="error composer-error" role="alert">
            {error}
          </p>
        )}
      </form>
      <div className="team-line">
        <span className="small muted">AVAILABLE MODELS</span>
        <div>
          {demoMode ? (
            <span className="badge demo">Simulated models</span>
          ) : available.length ? (
            available
              .slice(0, 5)
              .map((p) => <AgentChip key={p.provider} id={p.provider} />)
          ) : (
            <span className="small muted">
              {providers.length
                ? "No models connected"
                : "Checking connections…"}
            </span>
          )}
        </div>
        <a href="#/settings">
          Manage <span>↗</span>
        </a>
      </div>
      <section className="inspiration">
        <div className="section-heading">
          <h2>A place to start</h2>
          <span className="small muted">MAKE IT YOUR OWN</span>
        </div>
        <div className="examples">
          {EXAMPLES.map((ex, i) => (
            <button
              type="button"
              key={ex.title}
              className="example"
              onClick={() => {
                setRequest(ex.text);
                question.current?.focus();
              }}
            >
              <span className={"example-icon example-" + i}>
                <Icon name={ex.icon} size={20} />
              </span>
              <strong>{ex.title}</strong>
              <span>{ex.description}</span>
              <span className="example-arrow">↗</span>
            </button>
          ))}
        </div>
      </section>
      <section className="history" id="recent-research">
        <div className="section-heading">
          <h2>Recent research</h2>
          <span className="small muted">
            {runs.length
              ? runs.length + " SESSIONS"
              : "YOUR WORK, ALL TOGETHER"}
          </span>
        </div>
        {historyLoading ? (
          <div className="history-empty" role="status">
            Loading your research…
          </div>
        ) : historyError ? (
          <div className="notice warn" role="status">
            {historyError} We’ll try again shortly.
          </div>
        ) : runs.length === 0 ? (
          <div className="history-empty">
            <span className="empty-icon">
              <Icon name="file" size={24} />
            </span>
            <div>
              <strong>Good questions lead somewhere.</strong>
              <p>Your research sessions and results will be saved here.</p>
            </div>
            <span className="empty-arrow">↗</span>
          </div>
        ) : (
          <div className="run-list">
            {runs.map((r) => (
              <button
                key={r.id}
                className="run-item"
                onClick={() => open(r.id)}
              >
                <span className="run-item-icon">
                  <Icon name={r.profile === "software" ? "code" : "file"} />
                </span>
                <div className="run-item-body">
                  <p>{r.request}</p>
                  <span className="muted small">
                    {r.eris ? "Project Eris" : r.profile} · {r.tasks} tasks ·{" "}
                    {fmt(r.tokens)} tokens
                    {r.simulated ? " · simulated run" : ""}
                  </span>
                </div>
                <div className="run-item-end">
                  <StatusBadge status={r.status} />
                  <span className="muted small">{timeAgo(r.created_at)}</span>
                </div>
                <span className="run-item-arrow">↗</span>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
