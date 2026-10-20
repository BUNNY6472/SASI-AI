"use client";

import { ReactNode } from "react";
import { Run, STATUS_LABEL, providerMeta } from "@/lib/api";
import Markdown from "./Markdown";
import UsageDetails from "./UsageDetails";
import IncompleteWork from "./IncompleteWork";
import { AgentChip } from "./ui";

const TERMINAL = new Set(["completed", "failed", "cancelled", "budget_stopped", "interrupted"]);
const CHECK_LABEL: Record<string, string> = {
  unverified: "Not independently verified",
  reviewed: "Model reviewed",
  partial: "Some checks completed",
  failed: "Checks found an issue",
};
const PHASE_LABEL: Record<string, string> = {
  routing: "Choosing an approach", planning: "Organizing the work", answering: "Preparing your answer",
  verifying: "Checking the answer", correcting: "Addressing review findings", complete: "Work finished",
};

/** Only recorded actions and usage are shown; model agreement is not a benchmark. */
export function CollaborationSummary({ run }: { run: Run }) {
  const live = !TERMINAL.has(run.status);
  const calls = Object.entries(run.active_calls || {});
  const latest = run.events.at(-1);
  const bridge = run.bridge;
  return (
    <section className="bridge-collaboration glass" aria-label="Model collaboration">
      <div className="bridge-collaboration-head">
        <div><span className={"bridge-state-dot" + (live ? " active" : "")} /><strong>{live ? PHASE_LABEL[bridge?.phase || ""] || "Models at work" : "Collaboration recorded"}</strong></div>
        <span className="small muted">{run.usage.calls.toLocaleString("en-IN")} calls · {(run.usage.input_tokens + run.usage.output_tokens).toLocaleString("en-IN")} tokens</span>
      </div>
      {live && calls.length > 0 ? (
        <div className="bridge-active-models">{calls.map(([key, call]) => <span key={key}><AgentChip id={call.provider} /><span>{call.label || call.kind.replaceAll("_", " ")}</span></span>)}</div>
      ) : <p className="bridge-latest">{latest?.message || (live ? "Preparing the work your question needs…" : "No collaboration events were recorded.")}</p>}
      <details className="bridge-activity">
        <summary>Collaboration details{run.events.length > 0 ? ` · ${run.events.length} events` : ""}</summary>
        <p className="small muted">Each model contributes through its configured API or Vertex AI connection. Agreement is a review signal, not proof of correctness.</p>
        {bridge && <div className="bridge-routing">
          {(bridge.answerer || bridge.verifier) && <p>{bridge.answerer && <span>Answer: <AgentChip id={bridge.answerer} /></span>}{bridge.verifier && <span>Review: <AgentChip id={bridge.verifier} /></span>}</p>}
          {!!bridge.reasons.length && <ul>{bridge.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul>}
          {bridge.reviews.length > 0 && <p className="small muted">{bridge.reviews.length} review records. Evidence and checks below describe the current answer; earlier reviews may refer to earlier drafts.</p>}
          {bridge.correction_attempted && <p className="small muted">{bridge.correction_completed ? "A correction pass completed. Check the final review status below." : "A correction was requested; completion has not been recorded."}</p>}
        </div>}
        <ol>{run.events.map((event, index) => <li key={`${event.at}-${index}`}>
          <span className="bridge-event-label">{event.provider ? providerMeta(event.provider).name : "SASI"}</span>
          <span>{event.message}</span>
        </li>)}</ol>
        {!run.events.length && <p className="small muted">Events will appear as work is recorded.</p>}
      </details>
    </section>
  );
}

export default function BridgeSession({ run, evidence, solution, copyAnswer }: {
  run: Run;
  evidence: ReactNode;
  solution: ReactNode;
  copyAnswer: ReactNode;
}) {
  const live = !TERMINAL.has(run.status);
  const draft = run.live.final;
  const answer = draft?.text || run.final?.output;
  const checkStatus = run.final?.verification_status || "unverified";
  return (
    <div className="bridge-session">
      <CollaborationSummary run={run} />
      <article className="glass result bridge-answer" aria-label="SASI answer">
        <div className="bridge-answer-heading">
          <div><span className="eyebrow">SASI</span><h2>{answer ? "Your answer" : live ? "Working on your question" : "No final answer was produced"}</h2></div>
          {answer && <span className={"badge " + (checkStatus === "failed" ? "s-failed" : "")}>{live ? "Draft · review in progress" : CHECK_LABEL[checkStatus] || checkStatus}</span>}
        </div>
        {answer ? <>
          <div className="result-meta">{!draft && copyAnswer}{live && <span className="small muted">The answer may change as checks finish. <span className="typing" /></span>}</div>
          {!draft && solution}
          <Markdown text={answer} />
        </> : <p className="bridge-answer-placeholder">{live ? `${STATUS_LABEL[run.status] || "Working"}. SASI is selecting relevant work, gathering evidence when needed and preparing one answer.` : "The activity details and any saved partial work explain where the process stopped."}</p>}
        {run.final && <div className="bridge-assurance">
          {run.final.meets_criteria === false && <p className="warn-text">The review identified unmet criteria. Check the gaps before relying on this answer.</p>}
          {!!run.final.gaps.length && <div><h3>Known gaps</h3><ul>{run.final.gaps.map((gap, i) => <li key={i}>{gap}</li>)}</ul></div>}
          {!!run.final.limitations.length && <div><h3>Limitations</h3><ul>{run.final.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}
        </div>}
      </article>
      <UsageDetails usage={run.usage} />
      <details className="bridge-evidence">
        <summary><span>Evidence & checks</span><span>{run.evidence?.length || 0} sources · {CHECK_LABEL[checkStatus] || checkStatus}</span></summary>
        {evidence}
      </details>
      {!!run.partial_outputs?.length && <details className="bridge-evidence"><summary>Saved incomplete work · {run.partial_outputs.length}</summary><IncompleteWork outputs={run.partial_outputs} /></details>}
    </div>
  );
}
