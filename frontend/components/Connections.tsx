"use client";

import { FormEvent, useEffect, useState } from "react";
import { api, Provider, ProviderStatus } from "@/lib/api";
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

const WHERE: Record<Provider, { label: string; url: string }> = {
  claude: { label: "console.anthropic.com", url: "https://console.anthropic.com/settings/keys" },
  chatgpt: { label: "platform.openai.com", url: "https://platform.openai.com/api-keys" },
  gemini: { label: "aistudio.google.com", url: "https://aistudio.google.com/apikey" },
};

function KeyForm({ p, onChange }: { p: ProviderStatus; onChange: () => Promise<void> }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; warn?: boolean; text: string } | null>(null);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ model: string; saved: boolean; warning?: string }>(`/api/keys/${p.provider}`, { method: "PUT", body: JSON.stringify({ key }) });
      setKey("");
      setMsg(r.saved ? { ok: true, text: `Saved and working. Using ${r.model}.` } : { ok: false, warn: true, text: r.warning || "Working, but not saved." });
      await onChange();
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm(`Remove the ${p.display_name} key from SASI?`)) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ saved: boolean; warning?: string }>(`/api/keys/${p.provider}`, { method: "DELETE" });
      setMsg(r.saved ? { ok: true, text: "Removed. Paste a new key any time." } : { ok: false, warn: true, text: r.warning || "Removed from SASI." });
      await onChange();
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  const where = WHERE[p.provider];
  return (
    <form className="key-form" onSubmit={save} autoComplete="off">
      <input
        type="password"
        placeholder={p.connected ? `Replace key (${p.key_hint})` : `Paste your ${p.display_name} API key`}
        value={key}
        onChange={(e) => setKey(e.target.value)}
        autoComplete="new-password"
        spellCheck={false}
        aria-label={`${p.display_name} API key`}
      />
      <div className="row2">
        <button className="btn primary" disabled={busy || key.trim().length < 10}>{busy ? "Checking…" : p.connected ? "Replace key" : "Save key"}</button>
        {p.connected && <button type="button" className="btn ghost" disabled={busy} onClick={remove}>Remove</button>}
      </div>
      {msg && <p className={msg.ok ? "ok small" : msg.warn ? "warn-text small" : "error small"}>{msg.text}</p>}
      {!p.connected && (
        <p className="small muted">
          Get a key at <a href={where.url} target="_blank" rel="noreferrer">{where.label}</a>.
        </p>
      )}
    </form>
  );
}

export default function Connections({ providers, refresh, reload }: { providers: ProviderStatus[]; refresh: () => Promise<void>; reload: () => Promise<void> }) {
  const [limits, setLimits] = useState<Limits | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [allMsg, setAllMsg] = useState<{ warn: boolean; text: string } | null>(null);

  async function removeAll() {
    if (!window.confirm("Remove all API keys from SASI? Runs can't start until you add a key again.")) return;
    setBusy(true);
    setAllMsg(null);
    try {
      const r = await api<{ saved: boolean; warning?: string | null }>("/api/keys", { method: "DELETE" });
      setAllMsg(r.saved ? { warn: false, text: "All keys removed. Paste new ones above." } : { warn: true, text: r.warning || "Removed from SASI." });
      await reload();
    } catch (err) {
      setAllMsg({ warn: true, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    api<Limits>("/api/limits").then(setLimits).catch(() => {});
    api<{ routing_history: HistoryRow[] }>("/api/stats").then((s) => setHistory(s.routing_history)).catch(() => {});
  }, []);

  return (
    <div className="connections">
      <h1>Connections</h1>
      <p className="sub">
        Paste each API key once. SASI checks it, stores it on the server in Google Secret Manager, and never sends it back
        to any browser. Only the last 4 characters are shown so you can tell which key is saved.
      </p>
      <div className="provider-grid">
        {providers.map((p) => (
          <div key={p.provider} className="glass provider-card">
            <AgentChip id={p.provider} />
            <p className={p.connected ? (p.ok === false ? "warn-text" : "ok") : "muted"}>
              {p.connected ? (p.ok === false ? "Key saved, but the last check failed" : `Connected · key ${p.key_hint || ""}`) : "No key yet"}
            </p>
            {p.connected && p.model && <p className="small">Model: <code>{p.model}</code></p>}
            {p.error && <p className="error small">{p.error}</p>}
            <KeyForm p={p} onChange={reload} />
          </div>
        ))}
      </div>
      <p className="small muted">To change a key, paste the new one and press Replace key. You don&apos;t need to remove the old one first.</p>
      <div className="key-actions">
        <button className="btn" disabled={busy} onClick={async () => { setBusy(true); await refresh(); setBusy(false); }}>
          {busy ? "Checking…" : "Re-check all keys"}
        </button>
        {providers.some((p) => p.connected) && (
          <button className="btn danger" disabled={busy} onClick={removeAll}>Remove all keys</button>
        )}
      </div>
      {allMsg && <p className={allMsg.warn ? "warn-text small" : "ok small"}>{allMsg.text}</p>}

      <div className="glass lock-note">
        Only people with your access code can open SASI. Keep the code private. Every model call is billed to your own API accounts,
        so set a monthly spending limit in each provider's console too.
      </div>

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
