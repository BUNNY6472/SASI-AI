export type Provider = "claude" | "chatgpt" | "gemini";

export const PROVIDER_META: Record<Provider, { name: string; color: string }> = {
  claude: { name: "Claude", color: "#f6a26b" },
  chatgpt: { name: "ChatGPT", color: "#3ddc97" },
  gemini: { name: "Gemini", color: "#7aa2ff" },
};

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

export interface Run {
  id: string;
  created_at: number;
  updated_at: number;
  request: string;
  materials: string;
  profile: string;
  require_plan_approval: boolean;
  status: string;
  simulated: boolean;
  plan: Plan | null;
  plan_review: Review | null;
  plan_approved: boolean;
  final: {
    output: string;
    integrator: string | null;
    evaluator: string | null;
    meets_criteria: boolean | null;
    score: number | null;
    gaps: string[];
    limitations: string[];
  } | null;
  usage: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    by_provider: Record<string, { calls: number; input_tokens: number; output_tokens: number }>;
  };
  events: RunEvent[];
  error: string | null;
}

export interface RunSummary {
  id: string;
  created_at: number;
  request: string;
  profile: string;
  status: string;
  simulated: boolean;
  tasks: number;
  tokens: number;
}

export interface ProviderStatus {
  provider: Provider;
  display_name: string;
  connected: boolean;
  simulated: boolean;
  ok: boolean | null;
  model: string | null;
  error: string | null;
  key_hint: string | null;
}

export interface Profile {
  key: string;
  name: string;
  description: string;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
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
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
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
  const out = new Set<string>();
  if (run.status === "planning" && run.events.length) {
    const last = [...run.events].reverse().find((e) => e.provider);
    if (last?.provider) out.add(last.provider);
  }
  for (const t of run.plan?.tasks || []) {
    if ((t.status === "working" || t.status === "revising") && t.implementer) out.add(t.implementer);
    if (t.status === "reviewing" && t.reviewer) out.add(t.reviewer);
  }
  if (run.status === "reviewing_plan" || run.status === "integrating") {
    const last = [...run.events].reverse().find((e) => e.provider);
    if (last?.provider) out.add(last.provider);
  }
  return [...out];
}

export const STATUS_LABEL: Record<string, string> = {
  planning: "Planning",
  reviewing_plan: "Reviewing plan",
  awaiting_approval: "Waiting for your approval",
  running: "Working",
  integrating: "Integrating",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  budget_stopped: "Stopped at budget limit",
  interrupted: "Interrupted",
  pending: "Waiting",
  working: "Working",
  reviewing: "In review",
  revising: "Revising",
  accepted: "Accepted",
  needs_human_review: "Needs your review",
  blocked: "Blocked",
};
