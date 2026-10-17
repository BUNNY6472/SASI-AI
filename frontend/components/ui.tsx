"use client";

import { PROVIDER_META, Provider, STATUS_LABEL } from "@/lib/api";

export function AgentChip({ id, label }: { id: string | null | undefined; label?: string }) {
  if (!id) return <span className="chip muted">unassigned</span>;
  const meta = PROVIDER_META[id as Provider];
  return (
    <span className="chip" style={{ ["--c" as string]: meta?.color || "#999" }}>
      <span className="dot" />
      {label ? `${label} ` : ""}
      {meta?.name || id}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge s-${status}`}>{STATUS_LABEL[status] || status}</span>;
}

export function timeAgo(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function fmt(n: number): string {
  return n.toLocaleString();
}
