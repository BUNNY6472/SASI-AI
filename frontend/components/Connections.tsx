"use client";

import { FormEvent, useEffect, useState } from "react";
import { api, ProviderStatus, RouteStatus, VertexInfo } from "@/lib/api";
import { AgentChip } from "./ui";

interface Limits {
  time_limit_minutes: number;
  wrap_up_minutes: number;
  team_size: number;
  max_model_calls_per_run: number;
  max_tokens_per_run: number;
  max_output_tokens_per_call: number;
  max_tasks_per_plan: number;
  max_revisions_per_task: number;
  max_concurrent_runs: number;
  storage: string;
}

interface HistoryRow { provider: string; profile: string; attempts: number; accepted: number; score: number }

const WHERE: Record<string, { label: string; url: string }> = {
  claude: { label: "console.anthropic.com", url: "https://console.anthropic.com/settings/keys" },
  gemini: { label: "aistudio.google.com", url: "https://aistudio.google.com/apikey" },
};

const GARDEN = "https://console.cloud.google.com/vertex-ai/model-garden";

function tokens(n: number): string {
  return n >= 1_000_000 ? `${n / 1_000_000}M` : `${Math.round(n / 1000)}K`;
}

function RouteList({ p }: { p: ProviderStatus }) {
  const active = p.ok ? p.via : null;
  const line = (r: RouteStatus) => {
    const name = r.via === "vertex" ? "Google Cloud (Vertex AI)" : `API key ${p.key_hint || ""}`.trim();
    const state = r.ok === null ? "not checked yet" : r.ok ? `working${r.model ? ` · ${r.model}` : ""}` : "not working";
    const role = r.via === active ? "in use" : r.ok ? "backup" : "";
    return (
      <li key={r.via} className={r.ok === false ? "route bad" : r.ok ? "route good" : "route"}>
        <span><b>{name}</b>{role && <em>{role}</em>}</span>
        <span className="small">{state}</span>
        {r.ok === false && r.error && <span className="error small">{r.error}</span>}
      </li>
    );
  };
  return <ul className="routes">{p.routes.map(line)}</ul>;
}

function KeyForm({ p, vertexOn, onChange }: { p: ProviderStatus; vertexOn: boolean; onChange: () => Promise<void> }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; warn?: boolean; text: string } | null>(null);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ model: string; saved: boolean; warning?: string; via?: string | null }>(`/api/keys/${p.provider}`, { method: "PUT", body: JSON.stringify({ key }) });
      setKey("");
      const ok = r.via === "vertex"
        ? `Saved and working. ${p.display_name} keeps running through Google Cloud; this key is the backup.`
        : `Saved and working. Using ${r.model}.`;
      setMsg(r.saved ? { ok: true, text: ok } : { ok: false, warn: true, text: r.warning || "Working, but not saved." });
      await onChange();
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    const after = vertexOn ? ` ${p.display_name} will keep running through Google Cloud.` : "";
    if (!window.confirm(`Remove the ${p.display_name} key from SASI?${after}`)) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ saved: boolean; warning?: string }>(`/api/keys/${p.provider}`, { method: "DELETE" });
      const ok = vertexOn ? `Removed. ${p.display_name} runs through Google Cloud now. Paste a key any time.` : "Removed. Paste a new key any time.";
      setMsg(r.saved ? { ok: true, text: ok } : { ok: false, warn: true, text: r.warning || "Removed from SASI." });
      await onChange();
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  const where = WHERE[p.provider] || { label: "the provider's console", url: "#" };
  return (
    <form className="key-form" onSubmit={save} autoComplete="off">
      <input
        type="password"
        placeholder={p.has_key ? `Replace key (${p.key_hint})` : vertexOn ? `Optional: a ${p.display_name} API key` : `Paste your ${p.display_name} API key`}
        value={key}
        onChange={(e) => setKey(e.target.value)}
        autoComplete="new-password"
        spellCheck={false}
        aria-label={`${p.display_name} API key`}
      />
      <div className="row2">
        <button className="btn primary" disabled={busy || key.trim().length < 10}>{busy ? "Checking…" : p.has_key ? "Replace key" : "Save key"}</button>
        {p.has_key && <button type="button" className="btn ghost" disabled={busy} onClick={remove}>Remove</button>}
      </div>
      {msg && <p className={msg.ok ? "ok small" : msg.warn ? "warn-text small" : "error small"}>{msg.text}</p>}
      {!p.has_key && (
        <p className="small muted">
          {vertexOn ? "Not needed. To add one as the other route, get" : "Get"} a key at{" "}
          <a href={where.url} target="_blank" rel="noreferrer">{where.label}</a>.
        </p>
      )}
    </form>
  );
}

function status(p: ProviderStatus): { cls: string; text: string } {
  if (!p.connected) return { cls: "muted", text: "No key yet" };
  if (p.tier === "open") {
    if (p.ok === false) return { cls: "warn-text", text: "Not enabled or not working" };
    if (p.ok) return { cls: "ok", text: "Working through Google Cloud" };
    return { cls: "muted", text: "Not checked yet" };
  }
  if (p.ok === false) return { cls: "warn-text", text: p.has_key && p.routes.length < 2 ? "Key saved, but the last check failed" : "Not working right now" };
  if (p.via === "vertex") return { cls: "ok", text: "Connected through Google Cloud" };
  if (p.has_key) return { cls: "ok", text: `Connected · key ${p.key_hint || ""}` };
  return { cls: "ok", text: "Connected" };
}

export default function Connections({ providers, vertex, refresh, reload }: {
  providers: ProviderStatus[]; vertex: VertexInfo | null; refresh: () => Promise<void>; reload: () => Promise<void>;
}) {
  const vertexOn = !!vertex?.on;
  const [limits, setLimits] = useState<Limits | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [allMsg, setAllMsg] = useState<{ warn: boolean; text: string } | null>(null);

  async function removeAll() {
    const after = vertexOn ? " The models keep running through Google Cloud." : " Runs can't start until you add a key again.";
    if (!window.confirm(`Remove all API keys from SASI?${after}`)) return;
    setBusy(true);
    setAllMsg(null);
    try {
      const r = await api<{ saved: boolean; warning?: string | null }>("/api/keys", { method: "DELETE" });
      const ok = vertexOn ? "All keys removed. The models run through Google Cloud." : "All keys removed. Paste new ones above.";
      setAllMsg(r.saved ? { warn: false, text: ok } : { warn: true, text: r.warning || "Removed from SASI." });
      await reload();
    } catch (err) {
      setAllMsg({ warn: true, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  const frontier = providers.filter((p) => p.tier !== "open");
  const open = providers.filter((p) => p.tier === "open");

  useEffect(() => {
    api<Limits>("/api/limits").then(setLimits).catch(() => {});
    api<{ routing_history: HistoryRow[] }>("/api/stats").then((s) => setHistory(s.routing_history)).catch(() => {});
  }, []);

  return (
    <div className="connections">
      <h1>Connections</h1>
      {vertexOn ? (
        <p className="sub">
          Each run gets a team of up to {limits?.team_size ?? 4} models: the frontier models Claude and Gemini first, then the open
          models that suit the task best. They all run through Google Cloud Vertex AI
          {vertex?.project ? <> in project <code>{vertex.project}</code></> : null}, signed in as the app&apos;s own service account,
          so no API keys are needed. A Claude or Gemini API key you add is the backup route. Keys are stored in Google Secret
          Manager and never sent back to any browser.
        </p>
      ) : (
        <p className="sub">
          Paste a Claude or Gemini API key. SASI checks it, stores it on the server in Google Secret Manager, and never sends
          it back to any browser. Only the last 4 characters are shown so you can tell which key is saved. Open models need
          Google Cloud Vertex AI to be on.
        </p>
      )}
      <h2>Frontier models</h2>
      <div className="provider-grid">
        {frontier.map((p) => (
          <div key={p.provider} className="glass provider-card">
            <AgentChip id={p.provider} />
            <p className={status(p).cls}>{status(p).text}</p>
            {p.connected && p.model && <p className="small">Model: <code>{p.model}</code></p>}
            {p.routes.length > 1 || (vertexOn && p.routes.length) ? <RouteList p={p} /> : p.error && <p className="error small">{p.error}</p>}
            <KeyForm p={p} vertexOn={vertexOn} onChange={reload} />
          </div>
        ))}
      </div>
      <p className="small muted">To change a key, paste the new one and press Replace key. You don&apos;t need to remove the old one first.</p>
      {open.length > 0 && (
        <>
          <h2>Open models on Google Cloud</h2>
          <p className="muted small">
            SASI adds the open models that suit each run, best first, until the team is full. A model that does not work is
            skipped and the next one takes its seat. These run through Google Cloud only, with no API key.
          </p>
          <div className="provider-grid">
            {open.map((p) => (
              <div key={p.provider} className="glass provider-card">
                <AgentChip id={p.provider} />
                <p className={status(p).cls}>{status(p).text}</p>
                <p className="small muted">{p.about} · reads {tokens(p.context)} tokens</p>
                {p.ok && p.model && <p className="small">Model: <code>{p.model}</code></p>}
                {p.ok === false && p.error && <p className="error small">{p.error}</p>}
              </div>
            ))}
          </div>
        </>
      )}
      {vertexOn && (
        <p className="small muted">
          If a model shows &ldquo;not working&rdquo;, enable it for your project in{" "}
          <a href={GARDEN} target="_blank" rel="noreferrer">Vertex AI Model Garden</a> (open the model and press Enable), then
          press Check again.
        </p>
      )}
      <div className="glass lock-note small">
        <b>What costs money.</b> Every call through Google Cloud is billed to your Google Cloud project, and free-trial credit
        pays for it only where Google allows (it does not cover partner models such as Claude). The one free option is a Gemini
        API key from Google AI Studio on its free tier: it has rate limits, and Google may use free-tier prompts to improve its
        products.
      </div>
      <div className="key-actions">
        <button className="btn" disabled={busy} onClick={async () => { setBusy(true); await refresh(); setBusy(false); }}>
          {busy ? "Checking…" : vertexOn ? "Check again" : "Re-check all keys"}
        </button>
        {providers.some((p) => p.has_key) && (
          <button className="btn danger" disabled={busy} onClick={removeAll}>Remove all keys</button>
        )}
      </div>
      {allMsg && <p className={allMsg.warn ? "warn-text small" : "ok small"}>{allMsg.text}</p>}

      <div className="glass lock-note">
        Only people with your access code can open SASI. Keep the code private.{" "}
        {vertexOn
          ? "Calls through Google Cloud are billed to your Google Cloud project, and calls with an API key to that provider's account. Set a budget alert in Google Cloud Billing, and a monthly limit in each provider's console."
          : "Every model call is billed to your own API accounts, so set a monthly spending limit in each provider's console too."}
      </div>

      {limits && (
        <>
          <h2>Run limits</h2>
          <p className="muted small">
            Time is the limit, not tokens or calls. In the last {limits.wrap_up_minutes} minutes SASI stops new work and delivers the
            complete result from what is done. The clock pauses while a plan waits for your approval.
          </p>
          <div className="glass limits">
            <div><b>{limits.time_limit_minutes} min</b><span>per run</span></div>
            <div><b>{limits.wrap_up_minutes} min</b><span>wrap-up at the end</span></div>
            <div><b>{limits.team_size}</b><span>models per run</span></div>
            <div><b>{limits.max_model_calls_per_run || "No cap"}</b><span>model calls per run</span></div>
            <div><b>{limits.max_tokens_per_run ? limits.max_tokens_per_run.toLocaleString() : "No cap"}</b><span>tokens per run</span></div>
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
