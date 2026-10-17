/** A model id from the server's catalog: "claude", "gemini" (frontier) or an open model such as "glm" or "deepseek". */
export type Provider = string;

const META: Record<string, { name: string; color: string }> = {
  claude: { name: "Claude", color: "#f6a26b" },
  gemini: { name: "Gemini", color: "#7aa2ff" },
  glm: { name: "GLM", color: "#3ddc97" },
  deepseek: { name: "DeepSeek", color: "#b48cff" },
  qwen: { name: "Qwen", color: "#ff7eb6" },
  kimi: { name: "Kimi", color: "#4dd4e8" },
  minimax: { name: "MiniMax", color: "#ffd166" },
  gptoss: { name: "gpt-oss", color: "#9ad17a" },
  chatgpt: { name: "ChatGPT", color: "#3ddc97" }, // runs from before the change
};

/** Name and colour for any model id, including ones this screen has not heard of. */
export function providerMeta(id: string | null | undefined): {
  name: string;
  color: string;
} {
  if (!id) return { name: "Unknown", color: "#8a93a6" };
  return (
    META[id] || {
      name: id.charAt(0).toUpperCase() + id.slice(1),
      color: "#8a93a6",
    }
  );
}

export interface Review {
  reviewer: string;
  model: string;
  verdict: string;
  score: number | null;
  findings: string[];
  at: number;
}

export interface Attempt {
  provider: string;
  model: string;
  output: string;
  is_revision: boolean;
  review: Review | null;
  error: string | null;
  at: number;
}

export interface Task {
  id: string;
  title: string;
  description: string;
  depends_on: string[];
  status: string;
  implementer: string | null;
  reviewer: string | null;
  route_reason: string;
  attempts: Attempt[];
  output: string;
  assurance: string | null;
}

export interface Plan {
  goal: string;
  assumptions: string[];
  deliverables: string[];
  success_criteria: string[];
  tasks: Task[];
  planner: string | null;
  version: number;
}

export interface RunEvent {
  at: number;
  type: string;
  message: string;
  provider: string | null;
  task_id: string | null;
}

export interface FileRef {
  id: string;
  name: string;
  mime: string;
  kind: "image" | "video" | "audio" | "pdf" | "text" | "other";
  size: number;
}

export interface Opinion {
  provider: string;
  model: string;
  output: string;
  error: string | null;
  scores?: Record<string, number>;
  at: number;
}

export interface Consensus {
  best: string;
  average: Record<string, number>;
  votes: Record<string, Record<string, number>>;
}

export interface Judgement {
  artifact_hash?: string;
  judge: string;
  model: string;
  score: number | null;
  meets_criteria: boolean | null;
  must_fix: string[];
  gaps: string[];
  round: number;
}

export interface LiveText {
  provider: string;
  label: string;
  text: string;
  at: number;
}

export interface Run {
  mode?: "adaptive" | "quick" | "deep";
  evidence?: Evidence[];
  research_status?: string;
  research_summary?: string;
  search_queries?: string[];
  search_entry_html?: string;
  id: string;
  created_at: number;
  updated_at: number;
  request: string;
  materials: string;
  profile: string;
  eris: boolean;
  require_plan_approval: boolean;
  compare_all: boolean;
  files: FileRef[];
  analyses: Opinion[];
  cross_checks: Opinion[];
  consensus: Consensus | null;
  skipped?: Record<string, string>;
  /** The models working on this run, best first. */
  team?: string[];
  live: Record<string, LiveText>;
  status: string;
  simulated: boolean;
  plan: Plan | null;
  plan_review: Review | null;
  plan_approved: boolean;
  final: {
    verification_status?: "unverified" | "reviewed" | "partial" | "failed";
    artifact_hash?: string | null;
    checks?: VerificationCheck[];
    output: string;
    integrator: string | null;
    evaluator: string | null;
    meets_criteria: boolean | null;
    score: number | null;
    gaps: string[];
    limitations: string[];
    judges: Judgement[];
    fixes: number;
    best_approach: string | null;
  } | null;
  usage: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    by_provider: Record<
      string,
      { calls: number; input_tokens: number; output_tokens: number }
    >;
  };
  events: RunEvent[];
  error: string | null;
  /** The code file in the final result (solution.py on Project Eris runs), when there is one. */
  solution?: { name: string; code: string; lines: number } | null;
  /** Time is the run's limit. The clock pauses while the plan waits for approval. */
  time_limit_s?: number;
  wrap_up_s?: number;
  used_s?: number;
  clock_started_at?: number | null;
  deadline_at?: number | null;
  wrapping_up?: boolean;
  finished_at?: number | null;
}

export interface Evidence {
  id: string;
  url: string;
  title: string;
  excerpt: string;
  retrieved_at: number | string;
  source_kind: string;
  requested_url?: string;
  content_hash?: string;
  truncated?: boolean;
}

export interface VerificationCheck {
  id: string;
  kind: string;
  status: "pass" | "fail" | "unknown";
  summary: string;
  artifact_hash?: string | null;
}

/** Seconds of the run's time used so far. */
export function elapsed(run: Run, nowS = Date.now() / 1000): number {
  return (
    (run.used_s || 0) +
    (run.clock_started_at ? Math.max(0, nowS - run.clock_started_at) : 0)
  );
}

export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}

export interface RunSummary {
  mode?: "adaptive" | "quick" | "deep";
  id: string;
  created_at: number;
  request: string;
  profile: string;
  eris?: boolean;
  status: string;
  simulated: boolean;
  tasks: number;
  tokens: number;
  files: number;
  score?: number | null;
  team?: string[];
  elapsed_s?: number;
  time_limit_s?: number;
}

export interface ProviderStatus {
  provider: Provider;
  display_name: string;
  /** "frontier" (Claude, Gemini) or "open" (open models on Google Cloud). */
  tier: "frontier" | "open";
  about: string;
  /** Tokens the model reads in one request. */
  context: number;
  /** Whether SASI takes an API key for this model (Claude and Gemini only). */
  key_option: boolean;
  connected: boolean;
  simulated: boolean;
  ok: boolean | null;
  model: string | null;
  error: string | null;
  /** Disabled by the server's billing policy, even if credentials exist. */
  policy_blocked?: boolean;
  /** Scheduled MaaS retirement date, in UTC (YYYY-MM-DD). */
  retirement_date?: string | null;
  key_hint: string | null;
  /** How the model is reached right now: "vertex" (Google Cloud) or "key" (an API key). */
  via: "vertex" | "key" | null;
  has_key: boolean;
  routes: RouteStatus[];
}

export interface RouteStatus {
  via: "vertex" | "key";
  label: string;
  ok: boolean | null;
  model: string | null;
  error: string | null;
}

export interface VertexInfo {
  on: boolean;
  project: string;
  location: string;
}

export interface ProvidersResponse {
  providers: ProviderStatus[];
  needs_keys: boolean;
  demo_mode?: boolean;
  trial_credits_only?: boolean;
  trial_credit_expires_at?: string | null;
  trial_credit_window_error?: string | null;
  vertex?: VertexInfo;
  checked_at?: number | null;
  team_size?: number;
}

export interface Profile {
  key: string;
  name: string;
  description: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Upload one file with progress. Returns its reference for the run. */
export function uploadFile(
  file: File,
  onProgress: (pct: number) => void,
): Promise<FileRef> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/files");
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
    xhr.setRequestHeader(
      "Content-Type",
      file.type || "application/octet-stream",
    );
    xhr.upload.onprogress = (e) =>
      e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      let body: { detail?: string } & Partial<FileRef> = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as FileRef);
      else
        reject(
          new Error(
            typeof body.detail === "string"
              ? body.detail
              : xhr.status === 413
                ? "File is too large."
                : `Upload failed (${xhr.status})`,
          ),
        );
    };
    xhr.onerror = () =>
      reject(new Error("Upload failed. Check your connection."));
    xhr.send(file);
  });
}

export function fileSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg =
        typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail);
    } catch {
      /* keep status text */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

/** Providers currently doing something in a run, so the 3D scene can light them up. */
export function activeProviders(run: Run | null): string[] {
  if (!run) return [];
  const out = new Set<string>(
    Object.values(run.live || {}).map((l) => l.provider),
  );
  if (run.status === "planning" && run.events.length) {
    const last = [...run.events].reverse().find((e) => e.provider);
    if (last?.provider) out.add(last.provider);
  }
  for (const t of run.plan?.tasks || []) {
    if ((t.status === "working" || t.status === "revising") && t.implementer)
      out.add(t.implementer);
    if (t.status === "reviewing" && t.reviewer) out.add(t.reviewer);
  }
  if (run.status === "reviewing_plan" || run.status === "integrating") {
    const last = [...run.events].reverse().find((e) => e.provider);
    if (last?.provider) out.add(last.provider);
  }
  return [...out];
}

export const STATUS_LABEL: Record<string, string> = {
  researching: "Researching",
  verifying: "Checking evidence",
  analyzing: "Analysing",
  cross_checking: "Comparing",
  planning: "Planning",
  reviewing_plan: "Reviewing plan",
  awaiting_approval: "Waiting for your approval",
  running: "Working",
  integrating: "Integrating",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  budget_stopped: "Stopped at a cap",
  interrupted: "Interrupted",
  pending: "Waiting",
  working: "Working",
  reviewing: "In review",
  revising: "Revising",
  accepted: "Accepted",
  needs_human_review: "Needs your review",
  blocked: "Blocked",
};
