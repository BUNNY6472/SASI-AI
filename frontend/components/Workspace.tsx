"use client";

import { FormEvent, useEffect, useState } from "react";
import { api, Profile, ProviderStatus, Run, RunSummary } from "@/lib/api";
import { AgentChip, StatusBadge, fmt, timeAgo } from "./ui";

const EXAMPLES = [
  { profile: "software", text: "Design and write a small REST API for a to-do app in FastAPI, with tests." },
  { profile: "research", text: "Compare solar and wind power for a small farm in Andhra Pradesh: costs, payback and risks." },
  { profile: "general", text: "Plan a 4-week launch for a new mobile app on a ₹50,000 budget." },
];

export default function Workspace({ open, demo, providers }: { open: (id: string) => void; demo: boolean; providers: ProviderStatus[] }) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [request, setRequest] = useState("");
  const [materials, setMaterials] = useState("");
  const [showMaterials, setShowMaterials] = useState(false);
  const [profile, setProfile] = useState("general");
  const [approval, setApproval] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Profile[]>("/api/profiles").then(setProfiles).catch(() => {});
    const load = () => api<RunSummary[]>("/api/runs").then(setRuns).catch(() => {});
    load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const run = await api<Run>("/api/runs", {
        method: "POST",
        body: JSON.stringify({ request, materials, profile, require_plan_approval: approval }),
      });
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
          SASI plans your request, splits it across Claude, ChatGPT and Gemini, has each one's work reviewed by another,
          and delivers one integrated result with the evidence behind it.
        </p>
        <div className="agents-row">
          {providers.map((p) => (
            <span key={p.provider} className="agent-pill">
              <AgentChip id={p.provider} />
              <small>{p.connected ? (p.ok === false ? "error" : "connected") : "demo"}</small>
            </span>
          ))}
        </div>
      </section>

      {demo && (
        <div className="notice">
          <strong>Demo mode.</strong> No API keys are connected yet, so simulated agents run the full workflow. Add the keys in
          Google Cloud Secret Manager and the real models take over automatically.
        </div>
      )}

      <form className="glass composer" onSubmit={submit}>
        <div className="profile-tabs" role="tablist">
          {profiles.map((p) => (
            <button type="button" key={p.key} role="tab" aria-selected={profile === p.key} className={profile === p.key ? "tab on" : "tab"} onClick={() => setProfile(p.key)} title={p.description}>
              {p.name}
            </button>
          ))}
        </div>
        <textarea
          value={request}
          onChange={(e) => setRequest(e.target.value)}
          placeholder="What should the team work on?"
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
        <div className="composer-row">
          <button type="button" className="btn ghost" onClick={() => setShowMaterials((v) => !v)}>
            {showMaterials ? "Hide material" : "+ Add material"}
          </button>
          <label className="toggle">
            <input type="checkbox" checked={approval} onChange={(e) => setApproval(e.target.checked)} />
            <span>Let me approve the plan first</span>
          </label>
          <button className="btn primary" disabled={busy || request.trim().length < 3}>
            {busy ? "Starting…" : "Start collaboration"}
          </button>
        </div>
        {error && <p className="error">{error}</p>}
        {!request && (
          <div className="examples">
            {EXAMPLES.map((ex) => (
              <button type="button" key={ex.text} className="example" onClick={() => { setRequest(ex.text); setProfile(ex.profile); }}>
                {ex.text}
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
                {r.profile} · {r.tasks} tasks · {fmt(r.tokens)} tokens{r.simulated ? " · demo" : ""}
              </div>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
