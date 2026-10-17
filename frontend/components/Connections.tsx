"use client";

import { FormEvent, useEffect, useState } from "react";
import {
  api,
  fileSize,
  ProviderStatus,
  RouteStatus,
  RunLimits,
  VertexInfo,
} from "@/lib/api";
import { AgentChip } from "./ui";

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

function creditCutoff(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "Invalid configured cutoff";
  return date.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
    hour12: false,
  });
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
          {busy
            ? "Checking…"
            : p.has_key
              ? "Replace key"
              : `Connect ${p.display_name}`}
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
      text:
        p.provider === "claude"
          ? p.has_key
            ? "Claude key saved; connection unavailable"
            : "Claude key not connected"
          : trialCreditsOnly
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
  if (p.ok === null)
    return { cls: "muted", text: "Connection not checked yet" };
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
  claudeApiKeyAllowed = false,
  trialCreditExpiresAt = null,
  trialCreditWindowError = null,
  refresh,
  reload,
}: {
  providers: ProviderStatus[];
  vertex: VertexInfo | null;
  trialCreditsOnly?: boolean;
  claudeApiKeyAllowed?: boolean;
  trialCreditExpiresAt?: string | null;
  trialCreditWindowError?: string | null;
  refresh: () => Promise<void>;
  reload: () => Promise<void>;
}) {
  const vertexOn = !!vertex?.on;
  const [limits, setLimits] = useState<RunLimits | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [allMsg, setAllMsg] = useState<{ warn: boolean; text: string } | null>(
    null,
  );

  async function removeAll() {
    const after = trialCreditsOnly
      ? " The trial-credit policy and its configured cutoff remain in place."
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
        ? "All API keys removed. The trial-credit policy and its configured cutoff remain in place."
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
    api<RunLimits>("/api/limits")
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
              <strong>
                {trialCreditWindowError
                  ? "Trial access paused"
                  : claudeApiKeyAllowed
                    ? "Vertex Gemini + your Claude key"
                    : "Trial credits only · Vertex Gemini"}
              </strong>
              <p>
                {trialCreditWindowError ||
                  (claudeApiKeyAllowed
                    ? "The policy allows Vertex Gemini using your Google Cloud project and Claude using your own Anthropic API key. Claude is billed separately. Open models and the Gemini API-key route remain disabled."
                    : "SASI allows Gemini through your Google Cloud project only. Claude, open-model managed APIs and all direct API-key routes are disabled by this policy.")}
              </p>
              {trialCreditExpiresAt && (
                <p className="small">
                  <b>Configured credit cutoff:</b>{" "}
                  {creditCutoff(trialCreditExpiresAt)}. SASI stops model access
                  at this time. Credits may run out earlier.
                </p>
              )}
            </div>
          </div>
          <p className="sub">
            {trialCreditWindowError
              ? "New research is paused under the trial-credit policy. Your saved research remains accessible."
              : claudeApiKeyAllowed
                ? "Connect Claude below to make it available alongside Vertex Gemini. The connection status on each card shows which models are ready."
                : "Research can run with Vertex Gemini alone. Other integrations remain available in SASI for a future billing-policy change."}
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
            {p.key_option && !p.policy_blocked && !trialCreditWindowError ? (
              <>
                {p.provider === "claude" && (
                  <p className="small muted">
                    <b>Separate Anthropic billing.</b> Claude usage is charged
                    to your Anthropic account. Google Cloud credits and billing
                    limits do not cover or cap it. The key is sent to SASI’s
                    server; only a masked hint is displayed after connection.
                  </p>
                )}
                <KeyForm
                  p={p}
                  vertexOn={p.routes.some(
                    (route) => route.via === "vertex" && route.ok === true,
                  )}
                  onChange={reload}
                />
              </>
            ) : trialCreditsOnly || p.policy_blocked ? (
              <p className="small muted">
                {trialCreditWindowError
                  ? "Model access and API-key entry are paused by the configured trial cutoff."
                  : p.provider === "claude"
                    ? "Claude integration is ready when your billing policy allows a separate Anthropic API account. API-key entry is disabled for now."
                    : "The Gemini API-key route is disabled. Vertex AI is the only eligible route; its current access status is shown above."}
              </p>
            ) : null}
          </div>
        ))}
      </div>
      {!trialCreditWindowError &&
        providers.some((p) => p.key_option && !p.policy_blocked) && (
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
            {claudeApiKeyAllowed && (
              <>
                {" "}
                Claude API-key usage is billed separately by Anthropic; Google
                Cloud credits and billing limits do not apply.
              </>
            )}
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
              ? trialCreditWindowError
                ? "Refresh access status"
                : claudeApiKeyAllowed
                  ? "Check allowed connections"
                  : "Check Vertex Gemini"
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
          ? claudeApiKeyAllowed
            ? "The current policy permits Vertex Gemini and your Claude API key before the configured cutoff. Check Google Cloud credits and Anthropic spending separately."
            : "The current policy permits only Vertex Gemini before the configured cutoff. Keep an eye on your credit balance and expiry in Google Cloud Billing."
          : vertexOn
            ? "Calls through Google Cloud are billed to your Google Cloud project, and calls with an API key to that provider's account. Set a budget alert in Google Cloud Billing, and a monthly limit in each provider's console."
            : "Every model call is billed to your own API accounts, so set a monthly spending limit in each provider's console too."}
      </div>

      {limits && (
        <>
          <h2>Timing & run limits</h2>
          <p className="muted small">
            {limits.timing_policy === "graceful"
              ? "The research time target guides new work. Research stops earlier when needed to reserve the working allowance, finishing grace and final assembly within the total maximum. Work can finish early; provider errors, cancellation and usage budgets still apply."
              : "Historical strict timing sets an upper limit and reserves time to assemble a result. Work may finish earlier."}{" "}
            The session clock pauses while a plan waits for approval.
          </p>
          <div className="glass limits">
            <div>
              <b>{limits.time_limit_minutes} min</b>
              <span>
                {limits.timing_policy === "graceful"
                  ? "default research time target"
                  : "strict time limit"}
              </span>
            </div>
            {!!limits.max_total_run_minutes && (
              <div>
                <b>{limits.max_total_run_minutes} min total</b>
                <span>hard active-time maximum, including finishing</span>
              </div>
            )}
            {limits.timing_policy === "graceful" ? (
              <>
                <div>
                  <b>{limits.model_phase_min_minutes ?? 20} min minimum</b>
                  <span>working allowance per active model call</span>
                </div>
                <div>
                  <b>+ {limits.model_phase_grace_minutes ?? 5} min</b>
                  <span>finishing grace before an unfinished call stops</span>
                </div>
              </>
            ) : (
              <div>
                <b>Up to {limits.wrap_up_minutes} min</b>
                <span>delivery reserve, adjusted per run</span>
              </div>
            )}
            <div>
              <b>
                {trialCreditsOnly
                  ? trialCreditWindowError
                    ? "Paused"
                    : claudeApiKeyAllowed
                      ? "Gemini + Claude"
                      : "Gemini"
                  : limits.team_size}
              </b>
              <span>
                {trialCreditsOnly
                  ? trialCreditWindowError
                    ? "trial-credit access"
                    : "allowed by current policy"
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
              <span>total tokens across all calls per run</span>
            </div>
            <div>
              <b>
                {limits.eris_max_tokens_per_run
                  ? limits.eris_max_tokens_per_run.toLocaleString()
                  : "No cap"}
              </b>
              <span>total tokens across all calls per Eris run</span>
            </div>
            <div>
              <b>{limits.max_output_tokens_per_call.toLocaleString()}</b>
              <span>output ceiling per call; model-specific limits apply</span>
            </div>
            {!!limits.max_run_bytes && <div><b>{fileSize(limits.max_run_bytes)}</b><span>combined original file size per session</span></div>}
            {!!limits.max_files_per_run && <div><b>{limits.max_files_per_run}</b><span>files per session</span></div>}
            {limits.resumable_uploads !== undefined && <div><b>{limits.resumable_uploads ? "Resumable" : "Direct"}</b><span>{limits.resumable_uploads ? "chunked cloud uploads with retry" : `${fileSize(limits.raw_upload_max_bytes ?? 25 * 1024 ** 2)} per-file upload limit`}</span></div>}
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
          <p className="muted small">A session token allowance is shared across repeated calls. It is separate from a model’s per-request context window. Cached input still counts toward the allowance. Original files stay in storage; focused previews and compact reports reduce repeated input. File coverage and token breakdowns are available in each session.</p>
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
