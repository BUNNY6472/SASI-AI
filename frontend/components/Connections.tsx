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
  eris_max_tokens_per_run: number;
  max_output_tokens_per_call: number;
  max_tasks_per_plan: number;
  max_revisions_per_task: number;
  max_concurrent_runs: number;
  storage: string;
}

interface HistoryRow {
  provider: string;
  profile: string;
  attempts: number;
  accepted: number;
  score: number;
}

const WHERE: Record<string, { label: string; url: string }> = {
  claude: {
    label: "console.anthropic.com",
    url: "https://console.anthropic.com/settings/keys",
  },
  gemini: {
    label: "aistudio.google.com",
    url: "https://aistudio.google.com/apikey",
  },
};

const GARDEN = "https://console.cloud.google.com/vertex-ai/model-garden";

function tokens(n: number): string {
  return n >= 1_000_000 ? `${n / 1_000_000}M` : `${Math.round(n / 1000)}K`;
}

function RouteList({ p }: { p: ProviderStatus }) {
  const active = p.ok ? p.via : null;
  const line = (r: RouteStatus) => {
    const name =
      r.via === "vertex"
        ? "Google Cloud (Vertex AI)"
        : `API key ${p.key_hint || ""}`.trim();
    const state =
      r.ok === null
        ? "not checked yet"
        : r.ok
          ? `working${r.model ? ` · ${r.model}` : ""}`
          : "not working";
    const role = r.via === active ? "in use" : r.ok ? "backup" : "";
    return (
      <li
        key={r.via}
        className={r.ok === false ? "route bad" : r.ok ? "route good" : "route"}
      >
        <span>
          <b>{name}</b>
          {role && <em>{role}</em>}
        </span>
        <span className="small">{state}</span>
        {r.ok === false && r.error && (
          <span className="error small">{r.error}</span>
        )}
      </li>
    );
  };
  return <ul className="routes">{p.routes.map(line)}</ul>;
}

function KeyForm({
  p,
  vertexOn,
  onChange,
}: {
  p: ProviderStatus;
  vertexOn: boolean;
  onChange: () => Promise<void>;
}) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{
    ok: boolean;
    warn?: boolean;
    text: string;
  } | null>(null);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{
        model: string;
        saved: boolean;
        warning?: string;
        via?: string | null;
      }>(`/api/keys/${p.provider}`, {
        method: "PUT",
        body: JSON.stringify({ key }),
      });
      setKey("");
      const ok =
        r.via === "vertex"
          ? `Saved and working. ${p.display_name} keeps running through Google Cloud; this key is the backup.`
          : `Saved and working. Using ${r.model}.`;
      setMsg(
        r.saved
          ? { ok: true, text: ok }
          : {
              ok: false,
              warn: true,
              text: r.warning || "Working, but not saved.",
            },
      );
      await onChange();
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    const after = vertexOn
      ? ` ${p.display_name} will keep running through Google Cloud.`
      : "";
    if (!window.confirm(`Remove the ${p.display_name} key from SASI?${after}`))
      return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ saved: boolean; warning?: string }>(
        `/api/keys/${p.provider}`,
        { method: "DELETE" },
      );
      const ok = vertexOn
        ? `Removed. ${p.display_name} runs through Google Cloud now. Paste a key any time.`
        : "Removed. Paste a new key any time.";
      setMsg(
        r.saved
          ? { ok: true, text: ok }
          : { ok: false, warn: true, text: r.warning || "Removed from SASI." },
      );
      await onChange();
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  const where = WHERE[p.provider] || {
    label: "the provider's console",
    url: "#",
  };
  return (
    <form className="key-form" onSubmit={save} autoComplete="off">
      <input
        type="password"
        placeholder={
          p.has_key
            ? `Replace key (${p.key_hint})`
            : vertexOn
              ? `Optional: a ${p.display_name} API key`
              : `Paste your ${p.display_name} API key`
        }
        value={key}
        onChange={(e) => setKey(e.target.value)}
        autoComplete="new-password"
        spellCheck={false}
        aria-label={`${p.display_name} API key`}
      />
      <div className="row2">
        <button
          className="btn primary"
          disabled={busy || key.trim().length < 10}
        >
          {busy ? "Checking…" : p.has_key ? "Replace key" : "Save key"}
        </button>
        {p.has_key && (
          <button
            type="button"
            className="btn ghost"
            disabled={busy}
            onClick={remove}
          >
            Remove
          </button>
        )}
      </div>
      {msg && (
        <p
          className={
            msg.ok ? "ok small" : msg.warn ? "warn-text small" : "error small"
          }
        >
          {msg.text}
        </p>
      )}
      {!p.has_key && (
        <p className="small muted">
          {vertexOn ? "Not needed. To add one as the other route, get" : "Get"}{" "}
          a key at{" "}
          <a href={where.url} target="_blank" rel="noreferrer">
            {where.label}
          </a>
          .
        </p>
      )}
    </form>
  );
}

function status(
  p: ProviderStatus,
  trialCreditsOnly: boolean,
): { cls: string; text: string } {
  if (p.policy_blocked)
    return { cls: "warn-text", text: "Disabled by trial-credit policy" };
  if (!p.connected)
    return {
      cls: "muted",
      text: trialCreditsOnly
        ? "Vertex Gemini unavailable"
        : p.tier === "open"
          ? "Not connected through Google Cloud"
          : "No connection yet",
    };
  if (p.tier === "open") {
    if (p.ok === false)
      return { cls: "warn-text", text: "Not enabled or not working" };
    if (p.ok) return { cls: "ok", text: "Working through Google Cloud" };
    return { cls: "muted", text: "Not checked yet" };
  }
  if (p.ok === false)
    return {
      cls: "warn-text",
      text:
        p.has_key && p.routes.length < 2
          ? "Key saved, but the last check failed"
          : "Not working right now",
    };
  if (p.via === "vertex")
    return { cls: "ok", text: "Connected through Google Cloud" };
  if (p.has_key)
    return { cls: "ok", text: `Connected · key ${p.key_hint || ""}` };
  return { cls: "ok", text: "Connected" };
}

export default function Connections({
  providers,
  vertex,
  trialCreditsOnly = false,
  refresh,
  reload,
}: {
  providers: ProviderStatus[];
  vertex: VertexInfo | null;
  trialCreditsOnly?: boolean;
  refresh: () => Promise<void>;
  reload: () => Promise<void>;
}) {
  const vertexOn = !!vertex?.on;
  const [limits, setLimits] = useState<Limits | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [allMsg, setAllMsg] = useState<{ warn: boolean; text: string } | null>(
    null,
  );

  async function removeAll() {
    const after = trialCreditsOnly
      ? " Trial-credit policy will continue to allow only Vertex Gemini."
      : vertexOn
        ? " Only models with a working Google Cloud route can keep running."
        : " Runs can't start until you add a key again.";
    if (!window.confirm(`Remove all API keys from SASI?${after}`)) return;
    setBusy(true);
    setAllMsg(null);
    try {
      const r = await api<{ saved: boolean; warning?: string | null }>(
        "/api/keys",
        { method: "DELETE" },
      );
      const ok = trialCreditsOnly
        ? "All API keys removed. Trial-credit policy still allows only Vertex Gemini."
        : vertexOn
          ? "All keys removed. Models with a working Google Cloud route remain available."
          : "All keys removed. Paste new ones above.";
      setAllMsg(
        r.saved
          ? { warn: false, text: ok }
          : { warn: true, text: r.warning || "Removed from SASI." },
      );
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
    api<Limits>("/api/limits")
      .then(setLimits)
      .catch(() => {});
    api<{ routing_history: HistoryRow[] }>("/api/stats")
      .then((s) => setHistory(s.routing_history))
      .catch(() => {});
  }, []);

  return (
    <div className="connections">
      <h1>Connections</h1>
      {trialCreditsOnly ? (
        <>
          <div className="notice warn" role="status">
            <div>
              <strong>Trial credits only · Vertex Gemini</strong>
              <p>
                SASI allows Gemini through your Google Cloud project only.
                Claude, open-model managed APIs and all direct API-key routes
                are disabled by this policy.
              </p>
            </div>
          </div>
          <p className="sub">
            Research can run with Vertex Gemini alone. Other integrations remain
            available in SASI for a future billing-policy change.
            {vertex?.project && (
              <>
                {" "}
                Google Cloud project: <code>{vertex.project}</code>.
              </>
            )}
          </p>
        </>
      ) : vertexOn ? (
        <p className="sub">
          Manage the models available to your research. SASI selects a team for
          the request and research mode, with up to {limits?.team_size ?? 4}{" "}
          models. Google Cloud Vertex AI is enabled
          {vertex?.project ? (
            <>
              {" "}
              for <code>{vertex.project}</code>
            </>
          ) : null}
          . Gemini and open models connect through Google Cloud. Add your Claude
          API key below to connect Claude. Only a masked key hint is returned to
          the browser.
        </p>
      ) : (
        <p className="sub">
          Connect Claude or Gemini to begin. Keys are managed on the server;
          only their last four characters are displayed. Open model connections
          require Google Cloud Vertex AI.
        </p>
      )}
      <h2>Frontier models</h2>
      <div className="provider-grid">
        {frontier.map((p) => (
          <div key={p.provider} className="glass provider-card">
            <AgentChip id={p.provider} />
            <p className={status(p, trialCreditsOnly).cls}>
              {status(p, trialCreditsOnly).text}
            </p>
            {!p.policy_blocked && p.connected && p.model && (
              <p className="small">
                Model: <code>{p.model}</code>
              </p>
            )}
            {!p.policy_blocked &&
            (p.routes.length > 1 || (vertexOn && p.routes.length)) ? (
              <RouteList p={p} />
            ) : (
              p.error && (
                <p className={p.policy_blocked ? "small muted" : "error small"}>
                  {p.error}
                </p>
              )
            )}
            {trialCreditsOnly || p.policy_blocked ? (
              <p className="small muted">
                {p.provider === "claude"
                  ? "Claude integration is ready when your billing policy allows a separate Anthropic API account. API-key entry is disabled for now."
                  : "The Gemini API-key route is disabled. Gemini can use the allowed Vertex AI route only."}
              </p>
            ) : p.key_option ? (
              <KeyForm
                p={p}
                vertexOn={p.routes.some(
                  (route) => route.via === "vertex" && route.ok === true,
                )}
                onChange={reload}
              />
            ) : null}
          </div>
        ))}
      </div>
      {!trialCreditsOnly && (
        <p className="small muted">
          To change a key, paste the new one and press Replace key. You
          don&apos;t need to remove the old one first.
        </p>
      )}
      {open.length > 0 && (
        <>
          <h2>Open models on Google Cloud</h2>
          <p className="muted small">
            {trialCreditsOnly ? (
              "These integrations are disabled under the trial-credit policy. They do not participate in new runs, and adding credentials will not enable them while the policy is active."
            ) : (
              <>
                SASI adds the open models that suit each run, best first, until
                the team is full. A model that does not work is skipped and the
                next one takes its seat. These run through Google Cloud only,
                with no API key.
              </>
            )}
          </p>
          <div className="provider-grid">
            {open.map((p) => (
              <div key={p.provider} className="glass provider-card">
                <AgentChip id={p.provider} />
                <p className={status(p, trialCreditsOnly).cls}>
                  {status(p, trialCreditsOnly).text}
                </p>
                <p className="small muted">
                  {p.about} · reads {tokens(p.context)} tokens
                </p>
                {p.retirement_date && (
                  <p className="small warn-text">
                    Vertex endpoint retirement: {p.retirement_date} (UTC).
                  </p>
                )}
                {!p.policy_blocked && p.ok && p.model && (
                  <p className="small">
                    Model: <code>{p.model}</code>
                  </p>
                )}
                {(p.policy_blocked || p.ok === false) && p.error && (
                  <p
                    className={p.policy_blocked ? "small muted" : "error small"}
                  >
                    {p.error}
                  </p>
                )}
              </div>
            ))}
          </div>
        </>
      )}
      {vertexOn && !trialCreditsOnly && (
        <p className="small muted">
          If a model shows &ldquo;not working&rdquo;, enable it for your project
          in{" "}
          <a href={GARDEN} target="_blank" rel="noreferrer">
            Vertex AI Model Garden
          </a>{" "}
          (open the model and press Enable), then press Check again.
        </p>
      )}
      <div className="glass lock-note small">
        <b>Usage and billing.</b>{" "}
        {trialCreditsOnly ? (
          <>
            The policy limits which model routes SASI can use. SASI does not
            read your remaining Google Cloud credit balance or expiry. Check
            those in{" "}
            <a
              href="https://console.cloud.google.com/billing"
              target="_blank"
              rel="noreferrer"
            >
              Google Cloud Billing
            </a>{" "}
            before running research. Open-weight models on Vertex AI have
            serving charges and are disabled under this policy.
          </>
        ) : (
          <>
            Model calls use the connected provider account or Google Cloud
            project. Open-weight models on Vertex AI still incur serving
            charges; they are not automatically free. Check your provider
            console for current pricing, credits, data terms and quotas.
          </>
        )}
      </div>
      <div className="key-actions">
        <button
          className="btn"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await refresh();
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy
            ? "Checking…"
            : trialCreditsOnly
              ? "Check Vertex Gemini"
              : vertexOn
                ? "Check again"
                : "Re-check all keys"}
        </button>
        {providers.some((p) => p.has_key) && (
          <button className="btn danger" disabled={busy} onClick={removeAll}>
            Remove all keys
          </button>
        )}
      </div>
      {allMsg && (
        <p className={allMsg.warn ? "warn-text small" : "ok small"}>
          {allMsg.text}
        </p>
      )}

      <div className="glass lock-note">
        When the access-code gate is enabled, keep that code private.{" "}
        {trialCreditsOnly
          ? "The current policy allows only Vertex Gemini in your Google Cloud project. Keep an eye on your credit balance and expiry in Google Cloud Billing."
          : vertexOn
            ? "Calls through Google Cloud are billed to your Google Cloud project, and calls with an API key to that provider's account. Set a budget alert in Google Cloud Billing, and a monthly limit in each provider's console."
            : "Every model call is billed to your own API accounts, so set a monthly spending limit in each provider's console too."}
      </div>

      {limits && (
        <>
          <h2>Run limits</h2>
          <p className="muted small">
            Each session respects its requested time and the server limits
            below. SASI reserves time to assemble a result; the clock pauses
            while a plan waits for your approval. A deadline is an upper limit,
            not a required duration.
          </p>
          <div className="glass limits">
            <div>
              <b>{limits.time_limit_minutes} min</b>
              <span>maximum per run</span>
            </div>
            <div>
              <b>Up to {limits.wrap_up_minutes} min</b>
              <span>delivery reserve, adjusted per run</span>
            </div>
            <div>
              <b>{trialCreditsOnly ? "Gemini" : limits.team_size}</b>
              <span>
                {trialCreditsOnly
                  ? "allowed by current policy"
                  : "models per run"}
              </span>
            </div>
            <div>
              <b>{limits.max_model_calls_per_run || "No cap"}</b>
              <span>model calls per run</span>
            </div>
            <div>
              <b>
                {limits.max_tokens_per_run
                  ? limits.max_tokens_per_run.toLocaleString()
                  : "No cap"}
              </b>
              <span>tokens per run</span>
            </div>
            <div>
              <b>
                {limits.eris_max_tokens_per_run
                  ? limits.eris_max_tokens_per_run.toLocaleString()
                  : "No cap"}
              </b>
              <span>tokens per Eris run</span>
            </div>
            <div>
              <b>{limits.max_output_tokens_per_call.toLocaleString()}</b>
              <span>output tokens per call</span>
            </div>
            <div>
              <b>{limits.max_tasks_per_plan}</b>
              <span>tasks per plan</span>
            </div>
            <div>
              <b>{limits.max_revisions_per_task}</b>
              <span>revisions per task</span>
            </div>
            <div>
              <b>{limits.max_concurrent_runs}</b>
              <span>runs at once</span>
            </div>
            <div>
              <b>{limits.storage}</b>
              <span>project record</span>
            </div>
          </div>
        </>
      )}

      <h2>Routing history</h2>
      <p className="muted small">
        Historical model reviews by field. These are preference signals, not
        independent measurements of correctness.
      </p>
      <div className="glass table">
        {history.length === 0 && <p className="muted">No reviewed work yet.</p>}
        {history.map((h) => (
          <div key={`${h.provider}-${h.profile}`} className="row">
            <AgentChip id={h.provider} />
            <span>{h.profile}</span>
            <span>
              {h.accepted}/{h.attempts} accepted
            </span>
            <span className="bar">
              <i style={{ width: `${h.score * 100}%` }} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
