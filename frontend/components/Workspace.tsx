"use client";

import { DragEvent, FormEvent, useEffect, useRef, useState } from "react";
import {
  api,
  FileRef,
  fileSize,
  ProviderStatus,
  Run,
  RunLimits,
  RunSummary,
  StagedUpload,
} from "@/lib/api";
import { uploadFile, uploadSettings, UPLOAD_SETTINGS_UNAVAILABLE, UploadStatus, validateAttachment } from "@/lib/uploads";
import { isZipArchive, ZIP_COVERAGE_NOTE } from "@/lib/file-coverage";
import { AgentChip, StatusBadge, fmt, timeAgo } from "./ui";
import Icon from "./Icon";
import { researchTitle } from "@/lib/research-title";

interface Attachment {
  key: string;
  name: string;
  size: number;
  progress: number;
  status: UploadStatus | "queued" | "ready" | "failed";
  id?: string;
  stagedId?: string;
  error?: string;
  ref?: FileRef;
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
  const [minutes, setMinutes] = useState(120);
  const [limits, setLimits] = useState<RunLimits | null>(null);
  const [limitsLoading, setLimitsLoading] = useState(true);
  const [limitsError, setLimitsError] = useState("");
  const limitsRequest = useRef<Promise<RunLimits | null> | null>(null);
  const [files, setFiles] = useState<Attachment[]>([]);
  const [inventoryLoading, setInventoryLoading] = useState(true);
  const [inventoryUnavailable, setInventoryUnavailable] = useState(false);
  const [dragging, setDragging] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const question = useRef<HTMLTextAreaElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const removed = useRef(new Set<string>());
  const fileObjects = useRef(new Map<string, File>());
  const controllers = useRef(new Map<string, AbortController>());
  const uploadQueue = useRef<string[]>([]);
  const filesRef = useRef<Attachment[]>([]);
  const mounted = useRef(true);
  const uploading = files.some((f) => !f.id && !f.error);
  const failedUploads = files.some((f) => f.error);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileLimits = uploadSettings(limits);

  function refreshUploadLimits(): Promise<RunLimits | null> {
    if (limitsRequest.current) return limitsRequest.current;
    setLimitsLoading(true);
    setLimitsError("");
    const pending = api<RunLimits>("/api/limits", { cache: "no-store", signal: AbortSignal.timeout(15_000) })
      .then((value) => {
        if (mounted.current) {
          setLimits(value);
          setLimitsError(uploadSettings(value) ? "" : UPLOAD_SETTINGS_UNAVAILABLE);
        }
        return uploadSettings(value) ? value : null;
      })
      .catch(() => {
        if (mounted.current) {
          setLimits(null);
          setLimitsError("Upload limits could not be loaded. Retry settings, or save your question text and refresh the page.");
        }
        return null;
      })
      .finally(() => {
        limitsRequest.current = null;
        if (mounted.current) setLimitsLoading(false);
      });
    limitsRequest.current = pending;
    return pending;
  }

  useEffect(() => {
    let ignore = false;
    refreshUploadLimits();
    try {
      setEris(localStorage.getItem("sasi.eris") === "1");
    } catch {}
    api<StagedUpload[]>("/api/files/uploads")
      .then((staged) => {
        if (ignore || !mounted.current) return;
        updateFiles(() => staged.map((entry) => ({
          key: `restored-${entry.id}`,
          name: entry.name,
          size: entry.size,
          stagedId: entry.id,
          id: entry.state === "ready" ? entry.file?.id : undefined,
          ref: entry.state === "ready" ? entry.file : undefined,
          progress: entry.state === "ready" ? 100 : 0,
          status: entry.state === "ready" && entry.file ? "ready" : "failed",
          error: entry.state === "ready" && entry.file ? undefined : "An earlier upload was interrupted. Remove this entry, then select the file again.",
        })));
        setInventoryLoading(false);
      })
      .catch(() => {
        if (!ignore && mounted.current) {
          setInventoryLoading(false);
          setInventoryUnavailable(true);
          setError("Staged files could not be recovered. Refresh the page before attaching more files.");
        }
      });
    return () => { ignore = true; };
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      uploadQueue.current = [];
      controllers.current.forEach((controller) => controller.abort());
    };
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

  function updateFiles(update: (current: Attachment[]) => Attachment[]) {
    const next = update(filesRef.current);
    filesRef.current = next;
    if (mounted.current) setFiles(next);
  }
  function patchFile(key: string, patch: Partial<Attachment>) {
    updateFiles((current) => current.map((f) => f.key === key ? { ...f, ...patch } : f));
  }
  function pumpUploads(uploadLimits: RunLimits) {
    // Limit concurrent transfers; a large dataset never becomes one in-memory buffer.
    while (mounted.current && controllers.current.size < 2 && uploadQueue.current.length) {
      const key = uploadQueue.current.shift()!;
      const file = fileObjects.current.get(key);
      if (!file || removed.current.has(key) || controllers.current.has(key)) continue;
      const controller = new AbortController();
      controllers.current.set(key, controller);
      uploadFile(file, uploadLimits, controller.signal, (state) => patchFile(key, state))
        .then((ref) => {
          if (removed.current.has(key) || !mounted.current) {
            api("/api/files/" + ref.id, { method: "DELETE" }).catch(() => {});
            return;
          }
          patchFile(key, { id: ref.id, ref, status: "ready", progress: 100, error: undefined });
          fileObjects.current.delete(key);
        })
        .catch((err: Error) => {
          if (!removed.current.has(key) && mounted.current)
            patchFile(key, { error: err.message, status: "failed" });
        })
        .finally(() => {
          controllers.current.delete(key);
          pumpUploads(uploadLimits);
        });
    }
  }
  async function addFiles(list: FileList | null) {
    if (!list || busy || inventoryLoading || inventoryUnavailable) return;
    const selected = Array.from(list);
    if (!selected.length) return;
    // An open tab may span a deployment. Validate against fresh server limits.
    const currentLimits = await refreshUploadLimits();
    if (!currentLimits || !mounted.current) return;
    const errors: string[] = [];
    for (const file of selected) {
      const problem = validateAttachment(file, filesRef.current.reduce((sum, f) => sum + f.size, 0), filesRef.current.length, currentLimits);
      if (problem) { errors.push(problem); continue; }
      const key = crypto.randomUUID();
      fileObjects.current.set(key, file);
      updateFiles((current) => [...current, { key, name: file.name, size: file.size, progress: 0, status: "queued" }]);
      uploadQueue.current.push(key);
    }
    setError([...new Set(errors)].join(" "));
    pumpUploads(currentLimits);
  }
  async function retryFile(a: Attachment) {
    if (busy || controllers.current.has(a.key) || limitsLoading) return;
    const currentLimits = await refreshUploadLimits();
    const file = fileObjects.current.get(a.key);
    if (!currentLimits || !file || !mounted.current || removed.current.has(a.key)) return;
    const otherFiles = filesRef.current.filter((f) => f.key !== a.key);
    const problem = validateAttachment(file, otherFiles.reduce((sum, f) => sum + f.size, 0), otherFiles.length, currentLimits);
    if (problem) { patchFile(a.key, { error: problem }); return; }
    patchFile(a.key, { status: "queued", error: undefined, progress: 0 });
    uploadQueue.current.push(a.key);
    pumpUploads(currentLimits);
  }
  function removeFile(a: Attachment) {
    if (busy) return;
    removed.current.add(a.key);
    uploadQueue.current = uploadQueue.current.filter((key) => key !== a.key);
    controllers.current.get(a.key)?.abort();
    fileObjects.current.delete(a.key);
    updateFiles((fs) => fs.filter((f) => f.key !== a.key));
    const storedId = a.id || a.stagedId;
    if (storedId) api("/api/files/" + storedId, { method: "DELETE" }).catch(() => setError("The stored upload could not be removed. Refresh the page and try again."));
  }
  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || uploading || limitsLoading || inventoryLoading || failedUploads || needsKeys || request.trim().length < 3) return;
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
      updateFiles(() => []);
      fileObjects.current.clear();
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
          <div className="attachments upload-list" aria-label="Attached files">
            {files.map((f) => (
              <div key={f.key} className={"attach upload-item " + (f.error ? "err" : "")}>
                <Icon name="file" size={16} />
                <div className="upload-file-info">
                  <span className="attach-name" title={f.name}>{f.name}</span>
                  <small>{fileSize(f.size)} · {f.error ? "Upload failed" : f.status === "ready" ? "Stored" : f.status === "queued" ? "Queued" : f.status === "starting" ? "Preparing upload…" : f.status === "finalizing" ? "Preparing preview…" : f.status === "retrying" ? "Connection interrupted · retrying…" : `${f.progress}% uploaded`}</small>
                  {f.error && <span className="upload-detail error" role="alert">{f.error}</span>}
                  {f.ref?.coverage_note && <span className="upload-detail">{f.ref.coverage_note}</span>}
                  {f.ref && isZipArchive(f.name, f.ref.mime) && <span className="upload-detail">{ZIP_COVERAGE_NOTE}</span>}
                </div>
                {!f.id && !f.error && (
                  <span
                    className="attach-bar" role="progressbar"
                    aria-label={"Uploading " + f.name}
                    aria-valuemin={0} aria-valuemax={100} aria-valuenow={f.progress}
                    style={{ width: f.progress + "%" }}
                  />
                )}
                {f.error && fileObjects.current.has(f.key) && <button type="button" className="btn ghost small upload-retry" disabled={busy || limitsLoading} onClick={() => retryFile(f)}>Retry</button>}
                <button
                  type="button"
                  className="attach-x"
                  disabled={busy}
                  aria-label={(f.id || f.error ? "Remove " : "Cancel upload of ") + f.name}
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
              disabled={busy || !fileLimits || limitsLoading || inventoryLoading || inventoryUnavailable}
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
            Or drop files here
          </span>
        </div>
        <div className="upload-policy" aria-live="polite">
          {fileLimits && <span><strong>{fileSize(files.reduce((sum, f) => sum + f.size, 0))}</strong> / {fileSize(fileLimits.total)} total · up to {fileSize(fileLimits.perFile)} per file · {files.length} / {fileLimits.count} files</span>}
          <span>{limitsLoading ? "Checking current upload limits…" : inventoryLoading ? "Recovering staged uploads…" : inventoryUnavailable ? "File recovery is unavailable. Refresh before attaching files." : fileLimits ? fileLimits.resumable ? "Uploads resume automatically after connection interruptions. Keep this page open." : `Local storage: direct uploads up to ${fileSize(fileLimits.perFile)} per file.` : "File uploads are unavailable until settings are confirmed."}</span>
          {limitsError && <span className="error" role="alert">{limitsError} <button type="button" className="link-btn" disabled={limitsLoading} onClick={() => refreshUploadLimits()}>Retry settings</button></span>}
          <span>Original files are stored. Models receive bounded previews and compact data reports, not every uploaded byte. Check file coverage in the session.</span>
          <span>{ZIP_COVERAGE_NOTE}</span>
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
            <span className="time-picker-copy">
              <span>Research time target</span>
              <select
                value={minutes}
                aria-describedby="research-timing-note"
                onChange={(e) => setMinutes(Number(e.target.value))}
              >
                {[20, 30, 60, 120].map((m) => (
                  <option key={m} value={m}>
                    {m} minutes
                  </option>
                ))}
              </select>
            </span>
          </label>
          <button
            className="btn primary start-btn"
            disabled={
              busy || uploading || limitsLoading || inventoryLoading || failedUploads || needsKeys || request.trim().length < 3
            }
          >
            {busy ? "Starting…" : limitsLoading ? "Checking limits…" : inventoryLoading ? "Loading files…" : uploading ? "Uploading…" : failedUploads ? "Retry or remove failed files" : "Start research"}
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
        <p className="token-budget-note"><strong>{(eris ? limits?.eris_max_tokens_per_run : limits?.max_tokens_per_run)?.toLocaleString("en-IN") ?? "50,00,000"} tokens maximum per session.</strong> Shared across model calls, input, cached input and output. Each model has its own per-request context limit. Adaptive research uses focused context and expands work when needed.</p>
        <p id="research-timing-note" className="timing-note">
          <strong>
            {limits?.max_total_run_minutes ?? 120} minutes total active time
            maximum, including finishing.
          </strong>{" "}
          Work can finish early. New research stops before the target when
          needed to leave a {limits?.model_phase_min_minutes ?? 20}-minute
          working allowance, {limits?.model_phase_grace_minutes ?? 5} minutes of
          finishing grace, and final assembly within that maximum. Provider
          errors and usage budgets still apply.
        </p>
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
                  <p>{researchTitle(r.request)}</p>
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
