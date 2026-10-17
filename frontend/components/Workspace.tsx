"use client";

import { DragEvent, FormEvent, useEffect, useRef, useState } from "react";
import { api, fileSize, ProviderStatus, Run, RunSummary, uploadFile } from "@/lib/api";
import { AgentChip, StatusBadge, fmt, timeAgo } from "./ui";


interface Attachment {
  key: string;
  name: string;
  size: number;
  progress: number;
  id?: string;
  kind?: string;
  error?: string;
}

const KIND_ICON: Record<string, string> = { image: "🖼", video: "🎬", audio: "🎧", pdf: "📄", text: "📊", other: "📦" };

const EXAMPLES = [
  "Compare solar and wind power for a small farm in Andhra Pradesh: costs, payback and risks.",
  "Design and write a small REST API for a to-do app in FastAPI, with tests.",
  "Plan a 4-week launch for a new mobile app on a ₹50,000 budget.",
];

export default function Workspace({ open, needsKeys, providers }: { open: (id: string) => void; needsKeys: boolean; providers: ProviderStatus[] }) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [request, setRequest] = useState("");
  const [materials, setMaterials] = useState("");
  const [showMaterials, setShowMaterials] = useState(false);
  const [approval, setApproval] = useState(true);
  const [compare, setCompare] = useState(true);
  const [files, setFiles] = useState<Attachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const uploading = files.some((f) => !f.id && !f.error);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const load = () => api<RunSummary[]>("/api/runs").then(setRuns).catch(() => {});
    load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, []);

  function addFiles(list: FileList | null) {
    if (!list) return;
    const room = 10 - files.length;
    Array.from(list).slice(0, Math.max(0, room)).forEach((file) => {
      const key = `${file.name}-${file.size}-${Math.random()}`;
      setFiles((fs) => [...fs, { key, name: file.name, size: file.size, progress: 0 }]);
      const patch = (p: Partial<Attachment>) => setFiles((fs) => fs.map((f) => (f.key === key ? { ...f, ...p } : f)));
      uploadFile(file, (progress) => patch({ progress }))
        .then((ref) => patch({ id: ref.id, kind: ref.kind, progress: 100 }))
        .catch((err: Error) => patch({ error: err.message }));
    });
    if (list.length > room) setError("Up to 10 files per run.");
  }

  function removeFile(a: Attachment) {
    setFiles((fs) => fs.filter((f) => f.key !== a.key));
    if (a.id) api(`/api/files/${a.id}`, { method: "DELETE" }).catch(() => {});
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const run = await api<Run>("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          request, materials, profile: "auto", require_plan_approval: approval, compare_all: compare,
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

  return (
    <div className="workspace">
      <section className="hero">
        <h1>
          Three minds.
          <br />
          <span className="grad">One answer you can check.</span>
        </h1>
        <p className="sub">
          Give SASI a hard question and your files. Claude, ChatGPT and Gemini each analyse it on their own, check each
          other's work, split the job, and deliver one result showing where they agreed and disagreed.
        </p>
        <div className="agents-row">
          {providers.map((p) => (
            <span key={p.provider} className="agent-pill">
              <AgentChip id={p.provider} />
              <small>{p.connected ? (p.ok === false ? "error" : "connected") : "no key"}</small>
            </span>
          ))}
        </div>
      </section>

      {needsKeys && (
        <div className="notice keys-cta">
          <div>
            <strong>Add your API keys to start.</strong> Paste your Claude, ChatGPT and Gemini keys on the Connections page.
            They are stored on the server and never shown again.
          </div>
          <a className="btn primary" href="#/settings">Add keys</a>
        </div>
      )}

      <form
        className={dragging ? "glass composer dragging" : "glass composer"}
        onSubmit={submit}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <textarea
          value={request}
          onChange={(e) => setRequest(e.target.value)}
          placeholder="Ask anything: code, research, data, planning. SASI works out what kind of job it is."
          rows={4}
          maxLength={20000}
        />
        {showMaterials && (
          <textarea
            className="materials"
            value={materials}
            onChange={(e) => setMaterials(e.target.value)}
            placeholder="Paste reference material here (requirements, notes, data). It is treated as data, never as instructions."
            rows={5}
            maxLength={60000}
          />
        )}
        {files.length > 0 && (
          <div className="attachments">
            {files.map((f) => (
              <div key={f.key} className={f.error ? "attach err" : "attach"} title={f.error || f.name}>
                <span className="attach-icon">{KIND_ICON[f.kind || ""] || "📎"}</span>
                <span className="attach-name">{f.name}</span>
                <small className="muted">{f.error ? f.error : f.id ? fileSize(f.size) : `${f.progress}%`}</small>
                {!f.id && !f.error && <i className="attach-bar" style={{ width: `${f.progress}%` }} />}
                <button type="button" className="attach-x" aria-label={`Remove ${f.name}`} onClick={() => removeFile(f)}>×</button>
              </div>
            ))}
          </div>
        )}
        <input ref={picker} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
        <div className="composer-row">
          <button type="button" className="btn ghost" onClick={() => picker.current?.click()}>📎 Attach files</button>
          <button type="button" className="btn ghost" onClick={() => setShowMaterials((v) => !v)}>
            {showMaterials ? "Hide notes" : "+ Paste notes"}
          </button>
          <label className="toggle" title="Each model works on it alone first, then they read and check each other's work">
            <input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} />
            <span>All three analyse, then compare</span>
          </label>
          <label className="toggle">
            <input type="checkbox" checked={approval} onChange={(e) => setApproval(e.target.checked)} />
            <span>Let me approve the plan first</span>
          </label>
          <button className="btn primary" disabled={busy || uploading || needsKeys || request.trim().length < 3} title={needsKeys ? "Add an API key first" : undefined}>
            {busy ? "Starting…" : uploading ? "Uploading…" : "Start collaboration"}
          </button>
        </div>
        <p className="small muted drop-hint">Drop any files here: images, video, audio, PDF, CSV, JSON, Parquet, Excel, Word, ZIP, code and more. Up to 10 files, 25 MB each.</p>
        {error && <p className="error">{error}</p>}
        {!request && (
          <div className="examples">
            {EXAMPLES.map((ex) => (
              <button type="button" key={ex} className="example" onClick={() => setRequest(ex)}>
                {ex}
              </button>
            ))}
          </div>
        )}
      </form>

      <section className="history">
        <h2>Recent runs</h2>
        {runs.length === 0 && <p className="muted">No runs yet. Start one above.</p>}
        <div className="run-list">
          {runs.map((r) => (
            <button key={r.id} className="glass run-item" onClick={() => open(r.id)}>
              <div className="run-item-top">
                <StatusBadge status={r.status} />
                <span className="muted">{timeAgo(r.created_at)}</span>
              </div>
              <p>{r.request}</p>
              <div className="muted small">
                {r.profile} · {r.tasks} tasks{r.files ? ` · ${r.files} files` : ""} · {fmt(r.tokens)} tokens{r.simulated ? " · test run" : ""}
              </div>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
