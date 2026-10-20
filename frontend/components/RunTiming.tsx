"use client";

import { useEffect, useState } from "react";
import { clock, elapsed, Run } from "@/lib/api";
import { AgentChip } from "./ui";

const TERMINAL = new Set([
  "completed",
  "failed",
  "cancelled",
  "budget_stopped",
  "interrupted",
]);

function useNow(running: boolean) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => tick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [running]);
  return Date.now() / 1000;
}

export function RunClock({ run }: { run: Run }) {
  const terminal = TERMINAL.has(run.status);
  const questionDriven = run.workflow === "bridge" || run.timing_policy === "question_driven";
  const running = !!run.clock_started_at && !terminal && !questionDriven;
  const now = useNow(running);
  if (questionDriven) return null;
  const target = run.time_limit_s || 0;
  if (!target) return null;
  const used = elapsed(run, now);
  const total = run.total_time_limit_s || 0;
  const capReached = total > 0 && used >= total;

  if (run.timing_policy === "graceful") {
    return (
      <span
        className={
          "run-clock" +
          ((used >= target || capReached) && !terminal ? " wrap" : "")
        }
      >
        {clock(used)} elapsed · {clock(target)} target
        {!terminal && !running ? " · paused" : ""}
        {!terminal && capReached ? " · total cap reached" : ""}
        {!terminal && !capReached && run.wrapping_up ? " · finalizing" : ""}
        {!terminal && !capReached && used >= target && !run.wrapping_up
          ? " · finishing active work"
          : ""}
        {total > 0 && (
          <span className="run-clock-cap">
            {total / 60} min total maximum · includes finishing
          </span>
        )}
      </span>
    );
  }

  if (terminal)
    return (
      <span className="run-clock">
        {clock(used)} elapsed · {clock(target)} legacy limit
      </span>
    );

  return (
    <span className={"run-clock" + (run.wrapping_up ? " wrap" : "")}>
      {clock(Math.max(0, target - used))} left · strict limit
      {!running ? " · paused" : ""}
      {run.wrapping_up ? " · wrapping up" : ""}
    </span>
  );
}

export function ActiveCalls({ run }: { run: Run }) {
  const terminal = TERMINAL.has(run.status);
  const questionDriven = run.workflow === "bridge" || run.timing_policy === "question_driven";
  const calls = Object.entries(run.active_calls || {});
  const now = useNow(!questionDriven && !terminal && (calls.length > 0 || !!run.clock_started_at));
  if (questionDriven) return null;
  const used = elapsed(run, now);
  const graceful = run.timing_policy === "graceful";
  const targetReached = !!run.time_limit_s && used >= run.time_limit_s;
  const total = run.total_time_limit_s || 0;
  const totalRemaining = total > 0 ? Math.max(0, total - used) : null;
  const capReached = totalRemaining === 0;
  if (
    terminal ||
    (!calls.length &&
      !(graceful && (targetReached || capReached || run.wrapping_up)))
  )
    return null;

  return (
    <section
      className="active-calls"
      aria-label="Active model calls and timing"
    >
      <div className="active-calls-heading">
        <h2>
          {capReached
            ? "Total time cap reached"
            : run.wrapping_up
              ? "Assembling the result"
              : "Calls in progress"}
        </h2>
        {graceful && targetReached && !capReached && (
          <span className="badge">Time target reached</span>
        )}
      </div>
      {graceful && (
        <p>
          {capReached
            ? "The total allowance, including finishing, is exhausted. Waiting for the server to confirm the session has stopped."
            : run.wrapping_up
              ? "New research has stopped. Available work is being assembled within the remaining total allowance."
              : targetReached
                ? "No additional research will start. Active work and final assembly must fit within the remaining total allowance."
                : total > 0
                  ? `New research stops early when needed to leave a ${(run.model_phase_min_s ?? 1200) / 60}-minute working allowance, ${(run.model_phase_grace_s ?? 300) / 60} minutes of finishing grace, and final assembly before the total maximum. Calls can finish earlier.`
                  : `Each model call has a working allowance of at least ${(run.model_phase_min_s ?? 1200) / 60} minutes, then ${(run.model_phase_grace_s ?? 300) / 60} minutes of finishing grace. Calls can finish earlier; provider errors and usage budgets still apply.`}
          {run.wrapping_up && run.finalization_deadline_elapsed_s != null && (
            <>
              {" "}
              Final assembly allowance remaining:{" "}
              {clock(
                Math.max(
                  0,
                  Math.min(
                    run.finalization_deadline_elapsed_s - used,
                    totalRemaining ?? Infinity,
                  ),
                ),
              )}
              .
            </>
          )}
        </p>
      )}
      {totalRemaining !== null && (
        <div className="active-time-budget">
          <strong>{clock(totalRemaining)} remaining within total cap</strong>
          <span>
            {total / 60} minutes maximum, including finishing and final assembly
          </span>
        </div>
      )}
      {calls.map(([id, call]) => {
        const hardDeadline = Math.min(
          call.hard_deadline_at,
          totalRemaining === null ? Infinity : now + totalRemaining,
        );
        const softDeadline = Math.min(call.soft_deadline_at, hardDeadline);
        const grace = call.in_grace || now >= softDeadline;
        const expired = now >= hardDeadline;
        return (
          <div className={"active-call" + (grace ? " in-grace" : "")} key={id}>
            <div className="active-call-label">
              <AgentChip id={call.provider} />
              <span>{call.label || call.kind.replaceAll("_", " ")}</span>
            </div>
            <div className="active-call-time">
              <strong>
                {expired
                  ? "Awaiting stop confirmation"
                  : grace
                    ? "Grace period"
                    : "Working"}
              </strong>
              <span>{clock(Math.max(0, now - call.started_at))} elapsed</span>
              <span>
                {expired
                  ? "The call reached its timeout; waiting for the server update."
                  : grace
                    ? `${clock(hardDeadline - now)} until call timeout`
                    : hardDeadline <= call.soft_deadline_at
                      ? `${clock(hardDeadline - now)} until call timeout`
                      : `${clock(softDeadline - now)} until grace`}
              </span>
            </div>
          </div>
        );
      })}
    </section>
  );
}
