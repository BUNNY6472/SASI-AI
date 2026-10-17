"use client";

import { useEffect, useState } from "react";
import { api, ProviderStatus } from "@/lib/api";
import { AgentChip } from "./ui";

interface Limits {
  max_model_calls_per_run: number;
  max_tokens_per_run: number;
  max_output_tokens_per_call: number;
  max_tasks_per_plan: number;
  max_revisions_per_task: number;
  max_concurrent_runs: number;
  storage: string;
}

interface HistoryRow { provider: string; profile: string; attempts: number; accepted: number; score: number }

export default function Connections({ providers, refresh }: { providers: ProviderStatus[]; refresh: () => Promise<void> }) {
  const [limits, setLimits] = useState<Limits | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Limits>("/api/limits").then(setLimits).catch(() => {});
    api<{ routing_history: HistoryRow[] }>("/api/stats").then((s) => setHistory(s.routing_history)).catch(() => {});
  }, []);

  return (
    <div className="connections">
      <h1>Connections and settings</h1>
      <p className="sub">
        Keys never reach the browser. They live in Google Cloud Secret Manager and only the backend uses them.
      </p>
      <div className="provider-grid">
        {providers.map((p) => (
          <div key={p.provider} className="glass provider-card">
            <AgentChip id={p.provider} />
            <p className={p.connected ? (p.ok === false ? "warn-text" : "ok") : "muted"}>
              {p.connected ? (p.ok === false ? "Key added, but the check failed" : "Connected") : "Not connected (demo agent)"}
            </p>
            {p.model && <p className="small">Model: <code>{p.model}</code></p>}
            {p.error && <p className="error small">{p.error}</p>}
            {!p.connected && (
              <p className="small muted">
                Add secret <code>{{ claude: "ANTHROPIC_API_KEY", chatgpt: "OPENAI_API_KEY", gemini: "GEMINI_API_KEY" }[p.provider]}</code> in Secret Manager, then redeploy.
              </p>
            )}
          </div>
        ))}
      </div>
      <button className="btn" disabled={busy} onClick={async () => { setBusy(true); await refresh(); setBusy(false); }}>
        {busy ? "Checking…" : "Re-check providers"}
      </button>

      {limits && (
        <>
          <h2>Budget guards</h2>
          <div className="glass limits">
            <div><b>{limits.max_model_calls_per_run}</b><span>model calls per run</span></div>
            <div><b>{limits.max_tokens_per_run.toLocaleString()}</b><span>tokens per run</span></div>
            <div><b>{limits.max_output_tokens_per_call.toLocaleString()}</b><span>output tokens per call</span></div>
            <div><b>{limits.max_tasks_per_plan}</b><span>tasks per plan</span></div>
            <div><b>{limits.max_revisions_per_task}</b><span>revisions per task</span></div>
            <div><b>{limits.max_concurrent_runs}</b><span>runs at once</span></div>
            <div><b>{limits.storage}</b><span>project record</span></div>
          </div>
        </>
      )}

      <h2>Routing history</h2>
      <p className="muted small">How often each model's work passed independent review, per field. The router uses this to choose who does what.</p>
      <div className="glass table">
        {history.length === 0 && <p className="muted">No reviewed work yet.</p>}
        {history.map((h) => (
          <div key={`${h.provider}-${h.profile}`} className="row">
            <AgentChip id={h.provider} />
            <span>{h.profile}</span>
            <span>{h.accepted}/{h.attempts} accepted</span>
            <span className="bar"><i style={{ width: `${h.score * 100}%` }} /></span>
          </div>
        ))}
      </div>
    </div>
  );
}
