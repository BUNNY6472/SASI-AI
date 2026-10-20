"""Question-driven bridge orchestration, with compatibility for legacy saved runs.

New runs use one answerer, an independent bounded critique and at most one
correction followed by review. Optional planning stays a short checklist.
Original requirements and complete answer artifacts stay intact. The controller
enforces provider request timeouts, usage caps, trial expiry and cancellation;
there is no new-run session timer. Exact completed reviews are checkpointed.

Legacy saved runs retain their analyses/task pipeline and active-time policy.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from .. import files as files_mod
from ..config import Settings
from ..files import FileStore, StoredFile
from ..gateway import fit as fitting
from ..gateway.base import ModelCall, ModelResult, ProviderError, reply_room
from .context import compact_attachments
from ..gateway.catalog import name, reads, seat
from ..gateway.registry import FRONTIER, ModelRegistry
from ..gateway.routes import VIA, lasting_problem
from ..models import (TERMINAL, Attempt, BridgeReview, BridgeState, CallTiming, CheckResult, Consensus, Event, FinalResult, Judgement, LiveText, Opinion,
                      PartialOutput, Plan, Review, Run, Task, now)
from ..store.base import RunStore
from . import eris, policy, prompts
from .profiles import AUTO_FIELDS, get_profile
from .router import PerformanceHistory, Router

log = logging.getLogger("sasi.controller")
STALLED_FINAL_LIMITATION = ("A completed fix returned the unchanged result. Existing failed checks and reviewer findings "
                            "remain unresolved; repeated reviews and fixes were stopped.")

class ReviewPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    verdict: Literal["pass", "revise", "block", "accept"]
    score: float = Field(ge=0, le=10)
    findings: list[str]


class JudgementPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    meets_criteria: StrictBool
    score: float = Field(ge=0, le=10)
    must_fix: list[str]
    gaps: list[str]
    limitations: list[str]


class PlanTaskPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    depends_on: list[str]


class PlanPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    field: Literal["software", "research", "general"] | None = None
    goal: str = Field(min_length=1)
    assumptions: list[str]
    deliverables: list[str] = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)
    tasks: list[PlanTaskPayload] = Field(min_length=1)


def joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else "no model"


class BudgetExceeded(Exception):
    pass


class OutOfTime(Exception):
    """The run's working time (scope "run") or the current stage's share of it (scope "stage") is used up."""

    def __init__(self, scope: str = "run"):
        super().__init__(f"out of time ({scope})")
        self.scope = scope


class RunCancelled(Exception):
    pass


class NoProviders(Exception):
    pass


class CallWindowExpired(ProviderError):
    """One model's phase and finishing grace ended; its partial work is retained."""


class Controller:
    _heartbeat_seconds = 30.0

    def __init__(self, settings: Settings, registry: ModelRegistry, store: RunStore, history: PerformanceHistory,
                 files: FileStore | None = None):
        self.settings = settings
        self.registry = registry
        self.store = store
        self.files = files or FileStore()
        self._last_publish: dict[str, float] = {}
        self._last_checkpoint: dict[str, float] = {}
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}
        self.history = history
        self.router = Router(history)
        self._tasks: dict[str, asyncio.Task] = {}
        self._approvals: dict[str, asyncio.Event] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._unhealthy: dict[str, set[str]] = {}  # run id -> providers that failed this run
        self._stage_end: dict[str, float] = {}  # run id -> when the current stage's share of time ends
        self._partial: dict[str, str] = {}  # run id -> a final result that was cut off by the time limit
        self._trim_noted: set[tuple[str, str]] = set()
        self._token_reservations: dict[str, int] = {}
        self._shutting_down = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def shutdown(self) -> None:
        """Stop active work and persist an honest, resumable interruption state."""
        self._shutting_down = True
        active = [t for t in self._tasks.values() if not t.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._heartbeat_tasks:
            results = await asyncio.gather(*self._heartbeat_tasks.values(), return_exceptions=True)
            self._heartbeat_tasks.clear()
            for result in results:
                if isinstance(result, BaseException):
                    log.error("Active-clock checkpoint failed during shutdown: %s", type(result).__name__)

    async def _gather_evidence(self, run: Run, files: list[StoredFile]) -> None:
        """Evidence collection extension point; model prose is never source evidence."""
        from ..gateway.research import needs_web_research, search, web_research_opted_out
        from ..models import SourceEvidence
        from ..tools import collect_evidence

        if run.research_status not in ("not_started", "collecting"):
            return  # resume uses the same timestamped evidence, not a different answer's sources
        if run.timing_policy == "graceful" and not self._has_time(run, .1):
            run.research_status = "partial"
            self._event(run, "research_unavailable", "No new evidence requests were started because the work target or finalization reserve was reached.")
            return
        run.research_status = "collecting"
        self._event(run, "research", "Collecting supplied evidence and checking whether public research is needed.")
        await self._publish(run)
        remaining, _ = self._time_left(run)
        budget = min(12.0, max(0.1, (remaining - policy.finish_reserve(run)) if remaining is not None else 12.0))
        no_web = run.eris or web_research_opted_out(run.request)
        supplied = await collect_evidence("" if no_web else run.request, "" if no_web else run.materials,
                                          files, deadline_s=budget, max_sources=self.settings.research_max_sources)
        by_url = {e.url: e for e in run.evidence}
        for item in supplied["items"]:
            try:
                evidence = SourceEvidence.model_validate(item)
            except (ValidationError, ValueError, TypeError):
                self._event(run, "evidence_unavailable", "A retrieved source record was invalid and was excluded from evidence.")
                continue
            by_url[evidence.url] = evidence
        run.evidence = list(by_url.values())
        for issue in supplied["issues"][:6]:
            self._event(run, "evidence_unavailable", f"Could not retrieve {issue.get('url', 'source')}: {issue.get('summary', 'unavailable')}")
        if not needs_web_research(run.request, run.profile, eris=run.eris):
            run.research_status = "supplied_only" if run.evidence else "not_needed"
            await self._publish(run, checkpoint=True)
            return
        adapter = self.registry.adapters.get("gemini")
        if not self.settings.enable_web_research or adapter is None or getattr(adapter, "simulated", False):
            run.research_status = "unavailable"
            self._event(run, "research_unavailable", "Public web discovery is unavailable. Only supplied evidence can be used; current facts remain unverified.")
            await self._publish(run, checkpoint=True)
            return
        remaining, _ = self._time_left(run)
        timeout = self.settings.research_timeout_seconds
        if remaining is not None:
            timeout = min(timeout, max(0, remaining - policy.finish_reserve(run)))
        if timeout < 3:
            run.research_status = "partial"
            self._event(run, "research_unavailable", "Public search skipped to preserve time for the answer and its review.")
            await self._publish(run, checkpoint=True)
            return
        reserve = fitting.tokens(run.request) + 12_000
        try:
            self._check_budget(run, reserve)
        except BudgetExceeded:
            run.research_status = "partial"
            self._event(run, "research_unavailable", "Public search skipped because the remaining token or call budget is too small.")
            return
        self._token_reservations[run.id] = self._token_reservations.get(run.id, 0) + reserve
        search_attempts = reported_attempts = 0
        def charge_attempt() -> None:
            nonlocal search_attempts
            # Each route can make a billable request, including a failed fallback.
            self._check_budget(run)
            run.usage.calls += 1
            search_attempts += 1
            run.usage.by_provider.setdefault("gemini", {"calls": 0, "input_tokens": 0, "output_tokens": 0})["calls"] += 1

        def charge_result(result: ModelResult) -> None:
            nonlocal reported_attempts
            reported_attempts += 1
            if result.usage_estimated and not result.total_tokens:
                result.input_tokens = fitting.tokens(run.request)
                result.output_tokens = max(0, reserve - result.input_tokens)
            self._record_usage(run, "research", result)
            # Research route attempts were already counted at dispatch.
            run.usage.by_provider["gemini"]["calls"] -= 1
            self._record(run, ModelCall(system="", prompt=run.request, kind="research"), result)

        started = time.monotonic()
        self._event(run, "research", "Searching public sources with Gemini and Google Search. Attachments are not sent to this search call.")
        await self._publish(run)
        try:
            research = await asyncio.wait_for(search(
                adapter, run.request, max_sources=self.settings.research_max_sources,
                on_attempt=charge_attempt, on_result=charge_result), timeout=timeout)
            result = research.result
            policy.observe(run, "research", time.monotonic() - started)
            run.search_queries = research.queries
            run.search_entry_html = research.search_entry_html
            valid_search = []
            for item in research.evidence:
                try:
                    evidence = SourceEvidence.model_validate(item)
                except (ValidationError, ValueError, TypeError):
                    self._event(run, "evidence_unavailable", "A search source record was invalid and was excluded from evidence.")
                    continue
                valid_search.append(evidence)
                by_url.setdefault(evidence.url, evidence)
            # Ungrounded or invalid-source text is never promoted into the evidence brief.
            run.research_summary = result.text if valid_search else ""
            run.evidence = list(by_url.values())
            run.research_status = "complete" if valid_search else "partial"
            self._event(run, "research", f"Search returned {len(valid_search)} usable source links. Source coverage does not establish that every claim is correct.")
        except (ProviderError, asyncio.TimeoutError, ValidationError, ValueError, TypeError, KeyError) as exc:
            run.research_status = "unavailable"
            self._event(run, "research_unavailable", "Public search did not finish: " + (str(exc)[:300] or "time limit reached") + ". Supplied evidence is still available.")
        finally:
            for _ in range(max(0, search_attempts - reported_attempts)):
                prompt_tokens = fitting.tokens(run.request)
                run.usage.record("gemini", "research", prompt_tokens, max(0, reserve - prompt_tokens), estimated=True)
                run.usage.by_provider["gemini"]["calls"] -= 1
            self._token_reservations[run.id] = max(0, self._token_reservations.get(run.id, 0) - reserve)
        await self._publish(run, checkpoint=True)

    @staticmethod
    def _stamp_final(final: FinalResult) -> None:
        current = hashlib.sha256(final.output.encode("utf-8")).hexdigest()
        if current != final.artifact_hash:
            final.artifact_hash = current
            final.artifact_version += 1
            final.score = final.meets_criteria = final.evaluator = None
            final.verification_status = "unverified"

    async def _verify_final(self, run: Run) -> None:
        """Bind verification to the actual bytes. Deterministic checks plug in here."""
        if run.final:
            self._stamp_final(run.final)
            from ..verification import verify_output

            checks = verify_output(run.final.output, [e.model_dump() for e in run.evidence], artifact=run.solution or {})
            if not run.solution:
                checks = [c for c in checks if c["kind"] != "execution"]
            has_web_evidence = any(e.source_kind in ("web", "google_search") for e in run.evidence)
            if run.profile != "research" and not has_web_evidence and not re.search(r"https?://", run.final.output):
                checks = [c for c in checks if c["kind"] != "citations"]
            # Re-run deterministic checks on every version; keep past model reviews as history.
            run.final.checks = [c for c in run.final.checks if c.kind == "model_review"] + [CheckResult.model_validate(c) for c in checks]
            if run.research_status in ("unavailable", "partial"):
                limitation = "Public research was unavailable or incomplete; current factual claims require further checking."
                if limitation not in run.final.limitations:
                    run.final.limitations.append(limitation)
            self._refresh_final_assessment(run, run.final)

    @staticmethod
    def _current_artifact_hashes(run: Run, final: FinalResult) -> set[str]:
        hashes = {final.artifact_hash}
        solution = run.solution
        if solution and run.final is final:
            hashes.add(hashlib.sha256(solution["code"].encode("utf-8")).hexdigest())
        return hashes

    def _refresh_final_assessment(self, run: Run, final: FinalResult) -> None:
        self._stamp_final(final)
        current = [j for j in final.judges if j.artifact_hash == final.artifact_hash]
        if run.workflow == "bridge" and run.bridge:
            contexts = {(r.provider, r.round, r.artifact_hash) for r in run.bridge.reviews
                        if r.context_hash == run.bridge.review_context_hash}
            current = [j for j in current if (j.judge, j.round, j.artifact_hash) in contexts]
        if current:
            last_round = max(j.round for j in current)
            current = [j for j in current if j.round == last_round]
        scores = [j.score for j in current if j.score is not None]
        final.score = round(sum(scores) / len(scores), 1) if scores else None
        verdicts = [j.meets_criteria for j in current]
        final.meets_criteria = (False if False in verdicts else True if verdicts and all(v is True for v in verdicts) else None)
        final.evaluator = current[0].judge if current else None
        final.gaps = (["Still to fix: " + m for j in current for m in j.must_fix]
                      + [g for j in current for g in j.gaps])[:12]
        current_review_ids: set[str] = set()
        for j in current:
            check = CheckResult(id=f"review:{j.judge}:{j.round}:{j.artifact_hash[:12]}", kind="model_review",
                                status="fail" if j.meets_criteria is False or j.must_fix else
                                "pass" if j.meets_criteria is True else "unknown",
                                summary="; ".join(j.must_fix + j.gaps) or "Model opinion against the supplied criteria; not an executed test.",
                                artifact_hash=j.artifact_hash)
            current_review_ids.add(check.id)
            final.checks = [c for c in final.checks if c.id != check.id] + [check]
        current_hashes = self._current_artifact_hashes(run, final)
        checks = [c for c in final.checks if c.artifact_hash in current_hashes
                  and (c.kind != "model_review" or c.id in current_review_ids)]
        unfinished = any(t.status != "accepted" for t in run.plan.tasks) if run.plan else False
        independent = [j for j in current if j.judge != final.integrator and j.meets_criteria is True]
        if any(c.status == "fail" for c in checks):
            final.meets_criteria = False
            final.verification_status = "failed"
        elif unfinished or any(c.status == "unknown" for c in checks):
            final.verification_status = "partial"
        elif independent and final.meets_criteria is True:
            final.verification_status = "reviewed"
        else:
            final.verification_status = "unverified"

    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    def is_active(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return bool(task and not task.done())

    async def delete(self, run: Run) -> str:
        """Remove a finished run, its files and every model output (from BigQuery too)."""
        if self.is_active(run.id) or run.status not in TERMINAL:
            raise ValueError("Stop the run before deleting it.")
        self._tasks.pop(run.id, None)
        self._approvals.pop(run.id, None)
        self._last_publish.pop(run.id, None)
        self._last_checkpoint.pop(run.id, None)
        await self.files.delete(run.id)
        return await self.store.delete_run(run.id)

    async def start(self, run: Run) -> None:
        run.simulated = self.registry.live_count == 0
        self._set_limit(run)
        await self.store.save(run, checkpoint=True)
        self._launch(run)

    def _set_limit(self, run: Run) -> None:
        if run.workflow == "bridge":
            run.timing_policy = "question_driven"
            run.time_limit_minutes = None
            run.time_limit_s = run.total_time_limit_s = run.wrap_up_s = 0
            run.model_phase_min_s = run.model_phase_grace_s = 0
            run.finalization_deadline_elapsed_s = run.deadline_at = None
            return
        run.timing_policy = "graceful"
        run.model_phase_min_s = self.settings.model_phase_min_minutes * 60
        run.model_phase_grace_s = self.settings.model_phase_grace_minutes * 60
        run.total_time_limit_s = min(120 * 60, self.settings.max_total_run_minutes * 60)
        limit = max(0.0, self.settings.run_time_limit_minutes * 60)
        if run.time_limit_minutes is not None:
            requested = run.time_limit_minutes * 60
            limit = min(limit, requested) if limit else requested
        if limit:
            limit = max(20 * 60, limit)
        run.time_limit_s = min(limit or run.total_time_limit_s, run.total_time_limit_s)
        run.wrap_up_s = min(max(0.0, self.settings.wrap_up_minutes * 60), limit / 4,
                            1.4 * policy.expected_seconds(run, "wrap_up") + 15) if limit else 0.0

    def _launch(self, run: Run) -> None:
        self._approvals.setdefault(run.id, asyncio.Event())
        self._tasks[run.id] = asyncio.create_task(self._execute(run), name=f"run-{run.id}")

    async def approve(self, run: Run, note: str = "") -> None:
        if run.status != "awaiting_approval":
            raise ValueError("this run is not waiting for approval")
        run.plan_approved = True
        self._event(run, "approved", "Plan approved" + (f": {note}" if note else ""))
        await self.store.save(run, checkpoint=True)
        self._approvals.setdefault(run.id, asyncio.Event()).set()

    async def cancel(self, run: Run) -> None:
        task = self._tasks.get(run.id)
        if task and not task.done():
            task.cancel()
        elif run.status not in TERMINAL:
            run.status = "cancelled"
            self._event(run, "cancelled", "Run cancelled")
            await self._publish(run, checkpoint=True)

    async def resume(self, run: Run) -> None:
        if run.status not in ("interrupted", "budget_stopped", "failed", "cancelled"):
            raise ValueError("only interrupted, stopped, cancelled or failed runs can be resumed")
        if self._tasks.get(run.id) and not self._tasks[run.id].done():
            raise ValueError("run is already active")
        run.error = None
        run.wrapping_up = False
        run.finished_at = None
        if run.workflow == "bridge":
            self._set_limit(run)
        elif not run.time_limit_s:  # a run from before time limits
            self._set_limit(run)
        elif run.timing_policy != "graceful":
            run.timing_policy = "graceful"
            run.model_phase_min_s = self.settings.model_phase_min_minutes * 60
            run.model_phase_grace_s = self.settings.model_phase_grace_minutes * 60
        # Persisted call elapsed budgets and the finalization deadline remain intact.
        run.active_calls = {}
        self._event(run, "resumed", "Resuming from the last checkpoint; accepted work is kept")
        for t in run.plan.tasks if run.plan else []:
            if t.status not in ("accepted",):
                t.status = "pending"
        self._launch(run)

    # ------------------------------------------------------------------ subscriptions

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        self._subscribers.get(run_id, set()).discard(q)

    async def _publish(self, run: Run, checkpoint: bool = False) -> None:
        run.updated_at = now()
        await self.store.save(run, checkpoint=checkpoint)
        if checkpoint:
            self._last_checkpoint[run.id] = time.monotonic()
        self._notify(run)

    async def _checkpoint_active(self, run: Run) -> None:
        """Bound crash recovery loss while a provider reasons without visible text."""
        if time.monotonic() - self._last_checkpoint.get(run.id, 0) < self._heartbeat_seconds:
            return
        async with self._lock:
            if time.monotonic() - self._last_checkpoint.get(run.id, 0) >= self._heartbeat_seconds:
                await self._publish(run, checkpoint=True)

    def _scheduled_checkpoint(self, run: Run) -> asyncio.Task | None:
        """Storage must never block the provider deadline watchdog."""
        task = self._heartbeat_tasks.get(run.id)
        if task is not None and task.done():
            self._heartbeat_tasks.pop(run.id, None)
            task.result()  # surface durable failures through the workflow
            task = None
        if task is None and time.monotonic() - self._last_checkpoint.get(run.id, 0) >= self._heartbeat_seconds:
            task = asyncio.create_task(self._checkpoint_active(run), name=f"clock-checkpoint-{run.id}")
            self._heartbeat_tasks[run.id] = task
        return task

    async def _drain_checkpoint(self, run: Run) -> None:
        task = self._heartbeat_tasks.get(run.id)
        if task is not None:
            try:
                await asyncio.shield(task)
            finally:
                # A second cancellation must not orphan a still-writing checkpoint.
                # Shutdown (or the next waiter) retains ownership until it finishes.
                if task.done() and self._heartbeat_tasks.get(run.id) is task:
                    self._heartbeat_tasks.pop(run.id, None)

    def _notify(self, run: Run) -> None:
        """Wake clients even when durable storage is unavailable; this never claims a save."""
        for q in list(self._subscribers.get(run.id, ())):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(run.id)

    def _event(self, run: Run, type_: str, message: str, provider: str | None = None, task_id: str | None = None) -> None:
        run.events.append(Event(type=type_, message=message, provider=provider, task_id=task_id))
        if len(run.events) > 400:
            run.events = run.events[-400:]

    # ------------------------------------------------------------------ model calls

    def _eligible(self, run: Run) -> list[str]:
        """The run's team members that work right now, best first. If every one of them failed, the next best
        working models take over."""
        window_error = self.registry.trial_credit_window_error()
        if window_error:
            raise BudgetExceeded(window_error)
        bad = self._unhealthy.get(run.id, set())
        working = [p for p in self.registry.eligible() if p not in bad]
        if not run.team:
            team = working
        else:
            team = [p for p in run.team if p in working]
            if not team and working:
                spare = [p for p in self.registry.ranked(run.profile) if p not in bad][: max(1, self.settings.team_size)]
                if spare:
                    run.team = list(dict.fromkeys(run.team + spare))
                    self._event(run, "team", f"Every model on the team stopped working, so {joined([name(p) for p in spare])} "
                                f"took over")
                    team = spare
        if not team:
            raise NoProviders("no model is available. Check the Connections page.")
        return team

    def _replace(self, run: Run, failed: str) -> str | None:
        """A team member failed: bring in the next best working model, so the run keeps several models."""
        bad = self._unhealthy.get(run.id, set())
        spare = next((p for p in self.registry.ranked(run.profile) if p not in run.team and p not in bad), None)
        if spare:
            run.team.append(spare)
        return spare

    def _check_budget(self, run: Run, reserve: int = 0) -> None:
        """Usage/trial controls; legacy timed runs also retain their saved deadline."""
        if run.timing_policy == "graceful" and run.elapsed_s >= run.total_time_limit_s:
            raise OutOfTime("total")
        window_error = self.registry.trial_credit_window_error()
        if window_error:
            raise BudgetExceeded(window_error)
        s = self.settings
        if s.max_model_calls_per_run and run.usage.calls >= s.max_model_calls_per_run:
            raise BudgetExceeded(f"reached the limit of {s.max_model_calls_per_run} model calls for one run")
        limit = s.eris_max_tokens_per_run if run.eris else s.max_tokens_per_run
        if limit and run.usage.total_tokens + self._token_reservations.get(run.id, 0) + reserve >= limit:
            raise BudgetExceeded(f"the next call and in-flight reservations would exceed the {limit:,}-token run limit")

    # ------------------------------------------------------------------ the clock

    @staticmethod
    def _min_call(run: Run) -> float:
        """Shortest time worth starting a model call with."""
        return min(20.0, max(0.3, run.wrap_up_s * 0.1))

    @staticmethod
    def _grace(run: Run) -> float:
        """Kept free at the very end for saving the result."""
        return min(15.0, run.wrap_up_s * 0.05)

    def _clock_start(self, run: Run) -> None:
        if run.clock_started_at:  # the app stopped while this run was working: count the time up to its last save
            run.used_s += max(0.0, min(run.updated_at, now()) - run.clock_started_at)
            run.clock_started_at = None
        if not run.time_limit_s:
            run.deadline_at = None
            if run.workflow == "bridge":
                run.clock_started_at = now()
            return
        t = now()
        run.clock_started_at = t
        run.deadline_at = t + max(0.0, run.time_limit_s - run.used_s)

    @staticmethod
    def _clock_stop(run: Run) -> None:
        if run.clock_started_at:
            run.used_s += max(0.0, now() - run.clock_started_at)
            run.clock_started_at = None
        run.deadline_at = None

    def _time_left(self, run: Run) -> tuple[float | None, str]:
        """Time for admitting new work; graceful calls keep their own active windows."""
        if run.workflow == "bridge":
            return None, ""
        if run.timing_policy == "graceful":
            total_left = run.total_time_limit_s - run.elapsed_s
            if run.wrapping_up and run.finalization_deadline_elapsed_s is not None:
                return min(total_left, run.finalization_deadline_elapsed_s - run.elapsed_s), "finalization"
            # The soft target never spends the time reserved for final assembly.
            admission_left = total_left - policy.finalization_allowance(run)
            if run.time_limit_s:
                admission_left = min(admission_left, run.time_limit_s - run.elapsed_s)
            return admission_left, "run"
        if not run.deadline_at:
            return None, ""
        t = now()
        if run.wrapping_up:
            return run.deadline_at - self._grace(run) - t, "run"
        end = run.deadline_at - run.wrap_up_s
        stage = self._stage_end.get(run.id)
        if stage is not None and stage < end:
            return stage - t, "stage"
        return end - t, "run"

    def _has_time(self, run: Run, seconds: float) -> bool:
        if (run.timing_policy == "graceful" and not run.wrapping_up
                and run.total_time_limit_s - run.elapsed_s <
                run.model_phase_min_s + run.model_phase_grace_s + policy.finalization_allowance(run)):
            return False
        left, _ = self._time_left(run)
        return left is None or left >= seconds

    @staticmethod
    def _phase_key(provider: str, kind: str, task_id: str = "", phase: str = "") -> str:
        return ":".join((provider, kind, task_id, phase))

    def _can_continue(self, run: Run, provider: str, kind: str, task_id: str = "", phase: str = "") -> bool:
        key = self._phase_key(provider, kind, task_id, phase)
        return (key in run.call_allowance_s and key not in run.call_completed_keys
                and run.call_elapsed_s.get(key, 0) < run.call_allowance_s[key] + run.model_phase_grace_s)

    def _task_can_continue(self, run: Run, task: Task) -> bool:
        pending_review = bool(task.attempts and task.attempts[-1].output and task.attempts[-1].review is None)
        index = len(task.attempts) - 1 if pending_review else len(task.attempts)
        phase = str(index) if index else ""
        return (bool(not pending_review and task.implementer and self._can_continue(run, task.implementer, "task", task.id, phase))
                or bool(pending_review and task.reviewer and self._can_continue(run, task.reviewer, "review", task.id, phase)))

    def _out_cap(self, run: Run) -> int:
        """Longest answer one call may write. Eris runs need room for a whole training script."""
        s = self.settings
        ceiling = s.eris_max_output_tokens_per_call if run.eris else s.max_output_tokens_per_call
        if run.workflow == "bridge":
            return ceiling
        if run.mode == "deep":
            return ceiling
        return min(ceiling, 32_000 if run.eris else 16_000)

    def _live(self, run: Run, call: ModelCall, provider: str, key: str, label: str) -> None:
        """Stream this call's text to the screen while the model writes it."""
        live = run.live[key] = LiveText(provider=provider, label=label)

        async def on_text(piece: str) -> None:
            if previous:
                await previous(piece)
            live.text += piece
            t = time.monotonic()
            if t - self._last_publish.get(run.id, 0) >= 0.5:
                self._last_publish[run.id] = t
                await self._publish(run)

        previous = call.on_text
        call.on_text = on_text

    @staticmethod
    def _record_usage(run: Run, stage: str, result: ModelResult) -> None:
        run.usage.record(result.provider, stage, result.input_tokens, result.output_tokens,
                         uncached_input_tokens=result.uncached_input_tokens,
                         cache_read_input_tokens=result.cache_read_input_tokens,
                         cache_write_input_tokens=result.cache_write_input_tokens,
                         thinking_tokens=result.thinking_tokens,
                         detailed=result.usage_details_available, estimated=result.usage_estimated)

    def _record(self, run: Run, call: ModelCall, result: ModelResult) -> None:
        """Keep every model output, so each model's work can be seen and compared (saved to BigQuery)."""
        data = prompts.parse_json(result.text) if call.kind in ("review", "plan_review", "final_eval") else None
        score = (data or {}).get("score")
        self.store.record_output(run.id, {
            "run_id": run.id, "stage": call.kind, "provider": result.provider, "model": result.model,
            "task_id": call.hints.get("task_id"), "title": call.hints.get("task_title") or call.kind,
            "content": result.text[:1_000_000], "verdict": str((data or {}).get("verdict") or (data or {}).get("meets_criteria") or "") or None,
            "score": float(score) if isinstance(score, (int, float)) else None,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "created_at": time.time(),
        })

    async def _call(self, run: Run, provider: str, call: ModelCall, live: tuple[str, str] | None = None) -> ModelResult:
        """Bounded retry on transient errors. Non-transient errors mark the provider unhealthy for this run."""
        try:
            return await self._call_inner(run, provider, call, live)
        except OutOfTime:
            cut = run.live.get(live[0]) if live else None
            if live and live[0] == "final" and cut and cut.text.strip():
                self._partial[run.id] = cut.text  # the wrap-up builds on it
            raise
        finally:
            if live:
                run.live.pop(live[0], None)

    def _fit(self, run: Run, provider: str, call: ModelCall, model_max: int) -> None:
        """Trim this call's input, if needed, so it fits the model's context window with room for the reply."""
        context = self.registry.context(provider)
        note = fitting.fit(call, context, reads(provider), reply_room(call, model_max))
        if fitting.measure(call, reads(provider)) + reply_room(call, model_max) + fitting.MARGIN > context:
            raise ProviderError(provider, "The complete request or artifact cannot fit this model's context safely. "
                                "No partial-artifact review is accepted; use a model with a larger context.")
        if note and (run.id, f"{provider}:{call.kind}") not in self._trim_noted:
            self._trim_noted.add((run.id, f"{provider}:{call.kind}"))
            self._event(run, "trimmed", f"Trimmed {note} so the input fits {name(provider)}'s context window "
                        f"({context:,} tokens). Omitted content is not verified by this call.", provider)

    async def _call_inner(self, run: Run, provider: str, call: ModelCall, live: tuple[str, str] | None) -> ModelResult:
        """Admit once, then preserve the active model's window across retries."""
        self._check_budget(run)
        before = len(run.partial_outputs)
        window = None
        phase_key = ""
        started = time.monotonic()
        if run.timing_policy == "graceful":
            left, scope = self._time_left(run)
            phase_key = self._phase_key(provider, call.kind, str(call.hints.get("task_id", "")),
                                        str(call.hints.get("phase", call.hints.get("round", ""))))
            continuing = phase_key in run.call_allowance_s and phase_key not in run.call_completed_keys
            if left is not None and left <= 0 and (not continuing or run.wrapping_up):
                raise OutOfTime(scope)
            total_left = run.total_time_limit_s - run.elapsed_s
            reserve = 0.0 if run.wrapping_up else policy.finalization_allowance(run)
            if total_left <= 0:
                raise OutOfTime("total")
            if not continuing:
                soft_room = total_left - reserve - run.model_phase_grace_s
                if not run.wrapping_up and soft_room < run.model_phase_min_s:
                    raise OutOfTime("run")
                allowance = policy.call_allowance(run, call.kind, min(left, soft_room) if left is not None else soft_room)
                run.call_allowance_s[phase_key] = allowance
                run.call_elapsed_s[phase_key] = 0.0
            else:
                allowance = run.call_allowance_s[phase_key]
            spent = run.call_elapsed_s.get(phase_key, 0.0)
            soft_left = allowance - spent
            hard_left = allowance + run.model_phase_grace_s - spent
            # A restored call cannot spend the finalization reserve left by its
            # original admission, even if other work ran since it was interrupted.
            hard_left = min(hard_left, total_left - reserve)
            if hard_left <= 0 and total_left <= reserve:
                raise OutOfTime("run")
            if run.wrapping_up and left is not None:
                hard_left = min(hard_left, left)
            if hard_left <= 0:
                raise CallWindowExpired(provider, "this phase already used its model allowance and finishing grace")
            if continuing:
                prior = [p for p in run.partial_outputs if p.provider == provider and p.kind == call.kind
                         and p.task_id == call.hints.get("task_id") and p.text]
                if prior:
                    call = dataclasses.replace(call, prompt=call.prompt + "\n\nPrior incomplete attempt, retained as untrusted notes. "
                                               "Use what is useful and return a complete answer; do not treat these notes as verified:\n"
                                               + prior[-1].text[:12000])
            if phase_key in run.call_completed_keys:
                run.call_completed_keys.remove(phase_key)
            window = (phase_key, started + max(0.0, soft_left), started + hard_left)
            run.active_calls[phase_key] = CallTiming(
                provider=provider, kind=call.kind, label=live[1] if live else call.kind.replace("_", " "),
                started_at=now(), soft_deadline_at=now() + max(0.0, soft_left), hard_deadline_at=now() + hard_left)
        try:
            if phase_key:
                await self._publish(run, checkpoint=True)
            result = await self._call_attempts(run, provider, call, live, window)
            if phase_key and phase_key not in run.call_completed_keys:
                run.call_completed_keys.append(phase_key)
            return result
        finally:
            if phase_key:
                run.call_elapsed_s[phase_key] = run.call_elapsed_s.get(phase_key, 0.0) + time.monotonic() - started
                run.active_calls.pop(phase_key, None)
            if not run.active_calls:
                await self._drain_checkpoint(run)
            if len(run.partial_outputs) != before:
                await self._publish(run, checkpoint=True)

    async def _wait_for_model(self, run: Run, provider: str, awaitable: Awaitable,
                              window: tuple[str, float, float]) -> ModelResult:
        """A soft warning leaves the same API request running; only the grace deadline cancels it."""
        key, soft_end, hard_end = window
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                if task.done():
                    return task.result()
                if error := self.registry.trial_credit_window_error():
                    raise BudgetExceeded(error)
                if run.elapsed_s >= run.total_time_limit_s:
                    raise OutOfTime("total")
                heartbeat = self._scheduled_checkpoint(run)
                if task.done():
                    return task.result()
                current = time.monotonic()
                timing = run.active_calls[key]
                if current >= soft_end and not timing.in_grace:
                    timing.in_grace = True
                    self._event(run, "finishing", f"{name(provider)} is still working on {timing.label}. "
                                "Its model allowance has elapsed; the existing request can continue through the bounded finishing grace. "
                                "No new instruction was sent to its active stream.", provider)
                    await self._publish(run)
                if task.done():
                    return task.result()
                if current >= hard_end:
                    raise CallWindowExpired(provider, "model allowance and finishing grace exhausted; unfinished text was preserved")
                until = hard_end if timing.in_grace else min(soft_end, hard_end)
                heartbeat_due = (self._heartbeat_seconds if heartbeat is not None else
                                 self._heartbeat_seconds - (current - self._last_checkpoint.get(run.id, 0)))
                watched = {task, heartbeat} if heartbeat is not None else {task}
                await asyncio.wait(watched, timeout=max(0.001, min(30.0, until - current, heartbeat_due)),
                                   return_when=asyncio.FIRST_COMPLETED)
                if task.done():
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _wait_for_request(self, run: Run, awaitable: Awaitable, timeout: float) -> ModelResult:
        """Finite transport timeout, trial expiry and checkpoints without a session timer."""
        task = asyncio.ensure_future(awaitable)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if task.done():
                    return task.result()
                if error := self.registry.trial_credit_window_error():
                    raise BudgetExceeded(error)
                if time.monotonic() >= deadline:
                    raise asyncio.TimeoutError()
                heartbeat = self._scheduled_checkpoint(run)
                watched = {task, heartbeat} if heartbeat else {task}
                await asyncio.wait(watched, timeout=max(.001, min(30.0, deadline - time.monotonic())),
                                   return_when=asyncio.FIRST_COMPLETED)
                if heartbeat and heartbeat.done():
                    heartbeat.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def _keep_partial(self, run: Run, provider: str, call: ModelCall, text: str, reason: str,
                      reserve: int, usage_recorded: bool = False, model: str = "") -> None:
        partial = PartialOutput(provider=provider, model=model or self.registry.status.get(provider, {}).get("model") or "",
                                kind=call.kind, label=str(call.hints.get("task_title") or call.kind.replace("_", " ")),
                                text=text[:1_000_000], task_id=call.hints.get("task_id"), reason=reason[:500],
                                usage_estimated=not usage_recorded)
        run.partial_outputs.append(partial)
        # An interrupted stream can hide reasoning/usage. Reserve its full allowed
        # token amount conservatively instead of reporting zero or inventing exact usage.
        if not usage_recorded:
            input_estimate = min(reserve, fitting.measure(call, reads(provider)))
            output_estimate = max(0, reserve - input_estimate)
            run.usage.record(provider, call.kind, input_estimate, output_estimate, estimated=True)
        self.store.record_output(run.id, {
            "run_id": run.id, "stage": call.kind, "provider": provider, "model": partial.model,
            "task_id": partial.task_id, "title": partial.label + " (incomplete)", "content": partial.text,
            "verdict": "incomplete", "score": None, "input_tokens": 0, "output_tokens": 0, "created_at": now(),
        })
        self._event(run, "partial_saved", f"Saved {name(provider)}'s unfinished {partial.label}; it is not accepted or scored. "
                    + ("Token usage is conservatively estimated from the reserved allowance." if partial.usage_estimated else
                       "Provider-reported usage was retained."), provider, partial.task_id)

    async def _call_attempts(self, run: Run, provider: str, call: ModelCall, live: tuple[str, str] | None,
                             window: tuple[str, float, float] | None) -> ModelResult:
        adapter = self.registry.adapters.get(provider)
        if adapter is None:  # its key was removed while the run was going
            self._unhealthy.setdefault(run.id, set()).add(provider)
            run.skipped[provider] = "its key was removed during the run"
            raise ProviderError(provider, "its key was removed")
        call = dataclasses.replace(call)  # this model's own copy: trimming for one model never reaches another
        before_chars = sum(len(f.text) for f in call.attachments)
        call.attachments = compact_attachments(call.attachments, call.kind, call.prompt)
        after_chars = sum(len(f.text) for f in call.attachments)
        if after_chars < before_chars and (run.id, "context:" + call.kind) not in self._trim_noted:
            self._trim_noted.add((run.id, "context:" + call.kind))
            self._event(run, "context_compacted", f"Selected {after_chars:,} of {before_chars:,} file-preview characters "
                        f"for {call.kind.replace('_', ' ')}; original files remain stored. Omitted content is not verified.", provider)
        original_callback = call.on_text
        info = seat(provider)
        model_max = info.max_output if info else 32_000
        # Half the model's reply limit for the answer leaves the other half for its thinking.
        call.max_output_tokens = max(500, min(call.max_output_tokens, self._out_cap(run), model_max // 2))
        if call.kind == "analysis":
            call.max_output_tokens = min(call.max_output_tokens, 12_000 if run.eris else 8_000)
        if run.eris:
            call.rules = eris.RULES  # every model, on every step, follows the Eris guidebook
        token_limit = self.settings.eris_max_tokens_per_run if run.eris else self.settings.max_tokens_per_run
        if token_limit:
            remaining = max(0, token_limit - run.usage.total_tokens - self._token_reservations.get(run.id, 0))
            resource_note = ("Resource allowances are ceilings, not targets to spend. Reuse established facts; focus on unresolved "
                             "requirements and concrete checks. Return the requested artifact when complete. Do not repeat "
                             "analyses or claim that omitted file content was inspected. Preserve all material uncertainty.")
            if run.workflow == "bridge":
                # Keep the packet a single valid JSON object and the cacheable prefix stable.
                call.system += "\n\n" + resource_note
            else:
                call.prompt += f"\n\nController resource context: about {remaining:,} aggregate tokens remain across the team. " + resource_note
        base_timeout = 1200 if (call.attachments or run.eris or call.max_output_tokens > 8000) else 600
        if run.workflow == "bridge":
            base_timeout = self.settings.provider_request_timeout_seconds
            call.timeout_seconds = base_timeout + 30
        last: Exception | None = None
        again = shrunk = roomier = 0
        while True:
            left, scope = self._time_left(run)
            call.reasoning_effort = policy.reasoning_effort(run, call.kind, left)
            timeout: float = base_timeout
            if window is not None:
                if run.elapsed_s >= run.total_time_limit_s:
                    raise OutOfTime("total")
                if time.monotonic() >= window[2]:
                    raise CallWindowExpired(provider, "this phase already used its model allowance and finishing grace")
                timeout = max(0.001, window[2] - time.monotonic())
                # SDK read limits must not cut off a controller-admitted model phase.
                call.timeout_seconds = timeout + 30.0
            elif left is not None:
                if left < self._min_call(run):
                    raise OutOfTime(scope)
                timeout = min(timeout, left)
            self._fit(run, provider, call, model_max)
            reserve = fitting.measure(call, reads(provider)) + reply_room(call, model_max)
            self._check_budget(run, reserve)
            self._token_reservations[run.id] = self._token_reservations.get(run.id, 0) + reserve
            run.usage.calls += 1
            if run.workflow == "bridge":
                run.bridge = run.bridge or BridgeState()
                run.bridge.inflight_tokens = reserve
                run.bridge.inflight_provider, run.bridge.inflight_stage = provider, call.kind
                await self._publish(run, checkpoint=True)
            chunks: list[str] = []
            captured = 0
            async def capture(piece: str) -> None:
                nonlocal captured
                if captured < 1_000_000:
                    kept = piece[:1_000_000 - captured]
                    chunks.append(kept)
                    captured += len(kept)
                if original_callback:
                    await original_callback(piece)
            call.on_text = capture
            if live:
                self._live(run, call, provider, *live)
            started = time.monotonic()
            complete = usage_recorded = False
            failed_reason = "request interrupted"
            result = None
            try:
                result = (await self._wait_for_model(run, provider, adapter.generate(call), window) if window else
                          await self._wait_for_request(run, adapter.generate(call), timeout) if run.workflow == "bridge" else
                          await asyncio.wait_for(adapter.generate(call), timeout=timeout))
                if not result.text.strip():  # adapters raise for this already; an empty answer is never recorded
                    raise ProviderError(provider, "the model returned no answer text", retryable=True)
                stop = result.stop_reason.lower()
                if ("max" in stop and "token" in stop) or "length" in stop:
                    # A nonempty prefix is still incomplete, especially for code and JSON.
                    self._record_usage(run, call.kind, result)
                    usage_recorded = True
                    raise ProviderError(provider, "the response was truncated by its output limit", retryable=True)
                complete = True
            except ProviderError as e:
                failed_reason = str(e)
                if isinstance(e, CallWindowExpired):
                    raise
                last = e
                if run.timing_policy == "graceful" and run.wrapping_up:
                    break  # one bounded finalization request, never a fresh retry
                stated = fitting.context_error(str(e))
                if stated is not None and shrunk < 2:
                    shrunk += 1
                    limit = self.registry.learn_context(provider, stated)
                    self._event(run, "retry", f"{name(provider)} said the input was too long, so SASI trimmed it to fit "
                                f"{limit:,} tokens and is trying again", provider)
                    continue
                if ("no answer text" in str(e) or "truncated" in str(e)) and roomier < 1:
                    if run.workflow == "bridge" and call.kind in {"plan", "final_eval"}:
                        break  # compact handoff schemas never grow to narrative-sized output
                    roomier += 1
                    call.max_output_tokens = min(int(call.max_output_tokens * 1.5) + 2_000,
                                                 self._out_cap(run), model_max // 2) if run.workflow == "bridge" else min(
                                                     int(call.max_output_tokens * 1.5) + 2_000, model_max)
                    self._event(run, "retry", f"{name(provider)} did not finish its answer, so SASI is asking again "
                                "with more output room", provider)
                    continue
                if e.retryable and again < 1:
                    again += 1
                    self._event(run, "retry", f"{name(provider)} had a temporary error, retrying once", provider)
                    await asyncio.sleep(2)
                    continue
                break
            except asyncio.TimeoutError:
                failed_reason = "provider request timed out"
                if run.timing_policy == "graceful" and run.wrapping_up:
                    last = ProviderError(provider, failed_reason)
                    break
                if left is not None and timeout >= left - 0.5:
                    raise OutOfTime(scope) from None  # the time limit ended this call, not a fault of the model
                last = ProviderError(provider, f"timed out after {timeout:.0f}s", retryable=True)
                if again < 1:
                    again += 1
                    continue
                break
            except asyncio.CancelledError:
                failed_reason = "request cancelled or application interrupted"
                raise
            except BudgetExceeded as e:
                failed_reason = str(e)
                raise
            except OutOfTime as e:
                failed_reason = f"run time exhausted ({e.scope})"
                raise
            finally:
                if not complete:
                    self._keep_partial(run, provider, call, "".join(chunks) or (result.text if result else ""),
                                       failed_reason, reserve, usage_recorded, result.model if result else "")
                    if run.workflow == "bridge" and run.bridge:
                        run.bridge.inflight_tokens = 0
                        await self._publish(run, checkpoint=True)
                self._token_reservations[run.id] = max(0, self._token_reservations.get(run.id, 0) - reserve)
            policy.observe(run, call.kind, time.monotonic() - started)
            self._record_usage(run, call.kind, result)
            self._record(run, call, result)
            self.registry.sync(provider)  # the route that answered may have changed
            if run.workflow == "bridge" and run.bridge:
                run.bridge.inflight_tokens = 0
                await self._publish(run, checkpoint=True)
            return result
        self._unhealthy.setdefault(run.id, set()).add(provider)
        reason = str(last or "unknown error")
        reason = reason[len(provider) + 2:] if reason.startswith(f"{provider}: ") else reason
        self.registry.sync(provider)
        if lasting_problem(reason):
            self.registry.mark_failed(provider, reason)  # later runs skip it until it passes a check again
        run.skipped[provider] = f"failed during the run: {reason[:200]}"
        spare = self._replace(run, provider)
        others = [name(p) for p in run.team if p not in self._unhealthy.get(run.id, set()) and p in self.registry.adapters]
        self._event(run, "provider_error", f"{name(provider)} failed and is skipped for the rest of this run: {reason[:300]}. "
                    + (f"{name(spare)} joins the team. " if spare else "")
                    + (f"{joined(others)} carr{'y' if len(others) > 1 else 'ies'} on." if others else ""), provider)
        raise last or ProviderError(provider, "unknown error")

    async def _call_any(self, run: Run, role: str, call: ModelCall, load: dict[str, int],
                        exclude: set[str] | None = None, live: tuple[str, str] | None = None,
                        frontier_first: bool = False, strict_exclude: bool = False) -> tuple[str, ModelResult, str]:
        """Pick a provider for a role; on provider failure fall back to the next one and record it.
        Planning and writing the final result go to a frontier model when one works."""
        tried: set[str] = set()
        while True:
            eligible = [p for p in self._eligible(run) if p not in tried]
            if strict_exclude:
                eligible = [p for p in eligible if p not in (exclude or set())]
            if run.timing_policy == "graceful" and not run.wrapping_up:
                continuing = [p for p in eligible if self._can_continue(
                    run, p, call.kind, str(call.hints.get("task_id", "")), str(call.hints.get("phase", call.hints.get("round", ""))))]
                if continuing:
                    eligible = continuing  # preserve the admitted phase before ranking a new provider
                elif not self._has_time(run, 0.001):
                    raise OutOfTime("run")
            if not eligible:
                raise NoProviders(f"every model failed as {role}")
            if frontier_first:
                eligible = [p for p in eligible if p in FRONTIER] or eligible
            kind = "review" if call.kind in {"review", "plan_review", "final_eval", "cross_check"} or "review" in role or "evaluator" in role else "work"
            choice = self.router.pick(eligible, run.profile, load, role, exclude, kind=kind)
            try:
                return choice.provider, await self._call(run, choice.provider, call, live), choice.reason
            except ProviderError:
                tried.add(choice.provider)
                self._event(run, "fallback", f"Reassigning {role} after {name(choice.provider)} failed")

    # ------------------------------------------------------------------ main flow

    def _bridge_context(self, run: Run, kind: str) -> str:
        """Source excerpts and coverage, never a replacement for the original request."""
        checks = []
        if run.final and kind in {"final_eval", "correction"}:
            hashes = self._current_artifact_hashes(run, run.final)
            checks = [c.model_dump() for c in run.final.checks
                      if c.kind != "model_review" and c.artifact_hash in hashes]
        return json.dumps({
            "source_context": prompts.evidence_block(run.evidence, run.request, kind),
            "files": [f.model_dump() for f in run.files],
            "research_status": run.research_status,
            "deterministic_checks": checks,
        }, ensure_ascii=False)

    async def _bridge_review(self, run: Run, load: dict[str, int], files: list[StoredFile], plan):
        from . import bridge

        state, final = run.bridge, run.final
        assert state is not None and final is not None
        await self._verify_final(run)
        system, prompt = bridge.verifier_prompt(run, final.output, context=self._bridge_context(run, "final_eval"), plan=plan)
        context_hash = self._review_context_hash(run, system, prompt, files)
        state.review_context_hash = context_hash
        self._refresh_final_assessment(run, final)
        saved = next((r for r in reversed(state.reviews)
                      if r.context_hash == context_hash and r.artifact_hash == final.artifact_hash
                      and r.provider != final.integrator), None)
        if saved:
            self._event(run, "bridge_reused", "Reused the saved review of this exact answer and unchanged evidence.", saved.provider)
        else:
            state.phase, run.status = "verifying", "integrating"
            await self._publish(run, checkpoint=True)
            try:
                provider, result, _ = await self._call_any(
                    run, "independent bridge verifier",
                    ModelCall(system, prompt, "final_eval", bridge.REVIEW_MAX_OUTPUT_TOKENS,
                              {"request": run.request, "phase": f"bridge-review:{final.artifact_version}"},
                              attachments=files, preserve_prompt=True),
                    load, exclude={final.integrator} if final.integrator else set(), strict_exclude=True,
                    live=("bridge_review", "Checking the complete answer"))
            except NoProviders:
                note = "No independent model was available to review this answer. Its correctness remains unverified."
                if note not in final.limitations:
                    final.limitations.insert(0, note)
                return None
            state.verifier = provider
            try:
                packet = bridge.parse_review(result.text, allowed_refs=bridge.reference_ids(run), answer=final.output)
                payload, error = packet.model_dump(), ""
            except (ValidationError, ValueError, TypeError):
                payload, error = None, "The model's review was invalid or incomplete; it cannot verify this answer."
            saved = BridgeReview(provider=provider, model=result.model, artifact_hash=final.artifact_hash,
                                 context_hash=context_hash, round=max((j.round for j in final.judges), default=-1) + 1,
                                 packet=payload, error=error)
            state.reviews.append(saved)
        state.verifier = saved.provider
        try:
            packet = (bridge.parse_review(json.dumps(saved.packet), allowed_refs=bridge.reference_ids(run),
                                          answer=final.output) if saved.packet else None)
        except (ValidationError, ValueError, TypeError):
            packet = None
        # A passing model opinion with disclosed limitations is still a partial review.
        verdict = (False if packet and packet.verdict == "revise" else
                   True if packet and packet.verdict == "pass" and not packet.limitations else None)
        findings = [f.problem for f in packet.findings] if packet else []
        judgment = Judgement(
            judge=saved.provider, model=saved.model, round=saved.round, artifact_hash=final.artifact_hash,
            artifact_version=final.artifact_version, meets_criteria=verdict,
            must_fix=findings if packet and packet.verdict == "revise" else [],
            gaps=(findings + packet.limitations if packet and verdict is None else
                  [saved.error or "The saved review is invalid."] if packet is None else []))
        # Restoring a review is idempotent; no duplicate history or paid model call.
        final.judges = [j for j in final.judges if not (
            j.judge == judgment.judge and j.round == judgment.round and j.artifact_hash == judgment.artifact_hash)] + [judgment]
        if packet:
            final.limitations = list(dict.fromkeys(final.limitations + packet.limitations))
        self._refresh_final_assessment(run, final)
        self._event(run, "bridge_review", "Independent review: " + (packet.verdict if packet else "unverified") + ".",
                    saved.provider)
        await self._publish(run, checkpoint=True)
        return packet

    async def _execute_bridge(self, run: Run, load: dict[str, int], files: list[StoredFile]) -> None:
        """Answer -> independent compact critique -> at most one correction -> review."""
        from . import bridge

        state = run.bridge = run.bridge or BridgeState()
        route = bridge.route(run)
        state.complexity, state.reasons = route.complexity, list(route.reasons)
        self._event(run, "bridge", "One answerer and an independent verifier exchange a bounded critique; "
                    "the final answer remains complete. Work finishes when these stages finish.")
        plan = None
        if state.plan:
            try:
                plan = bridge.parse_plan(json.dumps(state.plan))
            except (ValidationError, ValueError, TypeError):
                state.plan = None
        if (route.needs_plan or run.require_plan_approval) and not state.plan_attempted and not run.final:
            state.phase, run.status = "planning", "planning"
            state.plan_attempted = True
            await self._publish(run, checkpoint=True)
            system, prompt = bridge.planner_prompt(run, context=self._bridge_context(run, "plan"))
            try:
                provider, result, _ = await self._call_any(
                    run, "bridge planner",
                    ModelCall(system, prompt, "plan", bridge.PLAN_MAX_OUTPUT_TOKENS,
                              {"request": run.request, "phase": "bridge-plan"}, attachments=files, preserve_prompt=True), load,
                    live=("bridge_plan", "Selecting requirements and checks"))
                plan = bridge.parse_plan(result.text)
                state.plan = plan.model_dump()
                run.plan = Plan(goal="Answer the original request", deliverables=plan.focus,
                                success_criteria=plan.checks, assumptions=plan.open_questions, planner=provider)
            except (ValidationError, ValueError, TypeError):
                self._event(run, "bridge_plan_invalid", "The planning packet was invalid; the answerer will use the full original requirements.")
            except NoProviders:
                self._event(run, "bridge_plan_unavailable", "The optional planner was unavailable.")
            await self._publish(run, checkpoint=True)
        if run.require_plan_approval and not run.plan_approved and not run.final:
            run.plan = run.plan or Plan(goal="Answer the original request", deliverables=["One complete answer"],
                                       success_criteria=get_profile(run.profile).final_checks)
            run.status = "awaiting_approval"
            self._clock_stop(run)
            self._event(run, "awaiting_approval", "Review the proposed checks and approve to start the answer.")
            await self._publish(run, checkpoint=True)
            await self._approvals.setdefault(run.id, asyncio.Event()).wait()
            self._clock_start(run)
        if not run.final or not run.final.output:
            state.phase, run.status = "answering", "running"
            await self._publish(run, checkpoint=True)
            system, prompt = bridge.answer_prompt(run, plan=plan, context=self._bridge_context(run, "task"))
            provider, result, _ = await self._call_any(
                run, "bridge answerer",
                ModelCall(system, prompt, "task", policy.answer_tokens(run, self._out_cap(run)),
                          {"request": run.request, "phase": "bridge-answer"}, attachments=files, preserve_prompt=True),
                load, live=("final", "Writing the complete answer"))
            state.answerer = provider
            run.final = FinalResult(output=result.text, integrator=provider)
            await self._verify_final(run)
            await self._publish(run, checkpoint=True)
        final = run.final
        state.answerer = final.integrator
        packet = await self._bridge_review(run, load, files, plan)
        current_hashes = self._current_artifact_hashes(run, final)
        deterministic_failures = [c for c in final.checks if c.kind != "model_review"
                                  and c.artifact_hash in current_hashes and c.status == "fail"]
        needs_correction = packet is not None and (
            packet.verdict == "revise" or deterministic_failures or
            (packet.verdict == "uncertain" and any(f.severity in {"blocker", "major"} for f in packet.findings)))
        if needs_correction and not state.correction_attempted:
            # Persist before dispatch: restart after an unknown outcome cannot launch another correction.
            state.correction_attempted = True
            state.phase, run.status = "correcting", "running"
            await self._publish(run, checkpoint=True)
            system, prompt = bridge.correction_prompt(run, final.output, packet,
                                                       context=self._bridge_context(run, "correction"))
            old_output = final.output
            try:
                provider, result, _ = await self._call_any(
                    run, "bridge corrector",
                    ModelCall(system, prompt, "task", policy.answer_tokens(run, self._out_cap(run)),
                              {"request": run.request, "phase": "bridge-correction"}, attachments=files,
                              preserve_prompt=True), load, live=("final", "Applying the review to the complete answer"),
                    exclude={state.verifier} if state.verifier else set(), strict_exclude=True)
                state.correction_completed = True
                if result.text == old_output:
                    final.limitations.insert(0, STALLED_FINAL_LIMITATION)
                    self._event(run, "no_progress", "The correction returned identical answer bytes. Existing findings remain unresolved; no duplicate review was requested.")
                else:
                    final.output, final.integrator, state.answerer = result.text, provider, provider
                    final.fixes += 1
                    self._stamp_final(final)
                    await self._verify_final(run)
                    await self._publish(run, checkpoint=True)
                    await self._bridge_review(run, load, files, plan)
            except NoProviders:
                final.limitations.insert(0, "The correction could not be completed by an available independent answerer; the reviewed draft is retained.")
        if state.correction_attempted and not state.correction_completed:
            final.limitations.insert(0, "The single correction was interrupted or unavailable. Its completion is not confirmed; the saved answer and unresolved review findings are retained.")
        if final.verification_status != "reviewed":
            final.limitations.insert(0, "Review did not establish that every requirement is met. Read the checks and unresolved findings before relying on this result.")
        final.limitations.append("Model review is an opinion against the supplied requirements and evidence, not an executed test.")
        final.limitations = list(dict.fromkeys(final.limitations))[:14]
        state.phase = "complete"
        self._deliver(run, final, "Delivered one complete answer with its verification status and recorded usage.")

    @staticmethod
    def _estimate(run: Run, files: list[StoredFile]) -> int:
        """Rough size of the run's input in tokens, for choosing models whose context window holds it."""
        texts = sum(fitting.tokens(f.text) for f in files if f.text)
        pdfs = sum(f.size // fitting.PDF_BYTES_PER_TOKEN for f in files
                   if f.kind == "pdf" and f.data and len(f.data) == f.size)
        return fitting.tokens(run.request + run.materials) + texts + pdfs

    def _note_team(self, run: Run, input_tokens: int) -> None:
        """Pick the run's team and say who takes part, how each is reached, and the time limit."""
        unavailable = self.registry.unavailable()
        if not run.team:
            run.team = self.registry.team(run.profile, input_tokens)
        # A missing frontier model always matters; an open model that is not working matters only if the team is short.
        short = len(run.team) < self.settings.team_size
        run.skipped = {p: why for p, why in unavailable.items() if p not in run.team and (p in FRONTIER or short)}
        try:
            working = self._eligible(run)
        except NoProviders:
            return
        via = {p: self.registry.status.get(p, {}).get("via") for p in working}
        names = [f"{name(p)} ({VIA[via[p]]})" if self.registry.uses_vertex and via[p] in VIA else name(p) for p in working]
        msg = "Team for this run: " + joined(names)
        if run.time_limit_s:
            left = max(0.0, run.time_limit_s - run.used_s)
            if run.timing_policy == "graceful":
                msg += (f". New-work target {left / 60:.0f} min; each admitted model phase has at least "
                        f"{run.model_phase_min_s / 60:.0f} min plus {run.model_phase_grace_s / 60:.0f} min finishing grace. "
                        "Models finish early when ready; active work may run beyond the target.")
            else:
                msg += (f". Time limit {left / 60:.0f} min; the last {run.wrap_up_s / 60:.0f} min are kept for delivering the complete result")
        if run.skipped:
            msg += ". Not available: " + "; ".join(f"{name(p)} ({why[:160]})" for p, why in run.skipped.items())
        self._event(run, "team", msg)

    async def _execute(self, run: Run) -> None:
        load: dict[str, int] = {}
        files: list[StoredFile] = []
        self._clock_start(run)
        try:
            try:
                if run.timing_policy == "graceful" and run.elapsed_s >= run.total_time_limit_s:
                    raise OutOfTime("total")
                files = await self.files.get(run.id) if run.files else []
                if run.workflow == "bridge" and run.bridge and run.bridge.inflight_tokens:
                    state = run.bridge
                    run.usage.record(state.inflight_provider, state.inflight_stage, 0, state.inflight_tokens, estimated=True)
                    self._event(run, "usage_estimated", "An interrupted provider request had no saved usage. Its reserved token allowance was conservatively charged.")
                    state.inflight_tokens = 0
                    await self._publish(run, checkpoint=True)
                self._note_team(run, self._estimate(run, files))
                await self._gather_evidence(run, files)
                await self._publish(run)
                if run.workflow == "bridge":
                    await self._execute_bridge(run, load, files)
                    return
                effort = policy.allocate(run, len(self._eligible(run)), self.settings.max_tasks_per_plan)
                self._event(run, "effort", f"{effort.reason}; {effort.analysts} initial analyst(s)")
                analyst_ids = self._eligible(run)[:effort.analysts]
                if not run.plan and effort.analysts and any(not any(a.provider == p and a.output for a in run.analyses) for p in analyst_ids):
                    await self._analyze(run, files)
                if not run.plan and effort.cross_check:
                    await self._cross_check(run)
                if (not run.plan or not run.plan.tasks or (not run.plan_approved and
                        (run.plan_review is None or any(self._can_continue(run, p, "plan", phase=f"revise:{run.plan.version}")
                                                       for p in self._eligible(run))))):
                    await self._plan(run, load, files)
                if (run.plan_review and run.plan_review.verdict != "accept"
                        and not run.plan_approved and not run.require_plan_approval):
                    raise ValueError("The saved plan did not pass review; resume cannot bypass that gate.")
                if run.require_plan_approval and not run.plan_approved:
                    run.status = "awaiting_approval"
                    self._clock_stop(run)  # the clock pauses while the plan waits for the user
                    self._event(run, "awaiting_approval", "Plan is ready. Review it and approve to start the work."
                                + (" The clock is paused until then." if run.time_limit_s else ""))
                    await self._publish(run, checkpoint=True)
                    await self._approvals.setdefault(run.id, asyncio.Event()).wait()
                    self._clock_start(run)
                run.status = "running"
                await self._publish(run)
                await self._run_tasks(run, load, files)
                await self._integrate(run, load)
            except OutOfTime:
                await self._wrap_up(run)
        except asyncio.CancelledError:
            run.status = "interrupted" if self._shutting_down else "cancelled"
            self._event(run, run.status, "Application stopped; accepted work is kept for resume." if self._shutting_down
                        else "Run cancelled. Accepted work is kept.")
        except BudgetExceeded as e:
            run.status = "budget_stopped"
            run.error = str(e)
            self._event(run, "budget_stopped", f"Stopped to protect the budget: {e}. Accepted work is kept.")
        except NoProviders as e:
            run.status = "failed"
            run.error = str(e)
            self._event(run, "failed", str(e))
        except Exception as e:  # noqa: BLE001 - never leave a run hanging
            log.exception("run %s failed", run.id)
            run.status = "failed"
            run.error = f"{type(e).__name__}: {e}"
            self._event(run, "failed", f"Unexpected error: {run.error}")
        finally:
            self._unhealthy.pop(run.id, None)
            self._stage_end.pop(run.id, None)
            self._partial.pop(run.id, None)
            self._trim_noted = {k for k in self._trim_noted if k[0] != run.id}
            self._token_reservations.pop(run.id, None)
            self._clock_stop(run)
            if run.status in TERMINAL:
                run.finished_at = now()
            run.live = {}
            run.active_calls = {}
            try:
                await self._publish(run, checkpoint=True)
            except Exception as e:
                log.exception("could not checkpoint run %s", run.id)
                run.status = "failed"
                run.error = f"Durable checkpoint failed: {type(e).__name__}. The latest work may not survive a restart."
                self._event(run, "failed", run.error)
                self._notify(run)

    # ------------------------------------------------------------------ independent analyses and cross-checks

    @staticmethod
    async def _together(jobs: dict[str, Awaitable], cap: float | None, enough: Callable[[], bool]) -> list[str]:
        """Run the jobs at the same time. Once `cap` seconds have passed and `enough` is true, stop waiting for the
        rest, so one slow model cannot hold the whole run up. Returns the keys of the jobs that were stopped."""
        tasks = {asyncio.ensure_future(j): k for k, j in jobs.items()}
        pending = set(tasks)
        stopped: list[str] = []
        end = None if cap is None else time.monotonic() + max(0.0, cap)
        try:
            while pending:
                t_now = time.monotonic()
                wait = None if end is None or t_now >= end else end - t_now  # after the cap: wait for the next one
                done, pending = await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if not t.cancelled() and t.exception() is not None:
                        raise t.exception()
                if pending and end is not None and time.monotonic() >= end and enough():
                    stopped = [tasks[t] for t in pending]
                    break
        finally:
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return stopped

    async def _analyze(self, run: Run, files: list[StoredFile]) -> None:
        """Every model on the team studies the request and files on its own, at the same time."""
        providers = self._eligible(run)
        effort = policy.allocate(run, len(providers), self.settings.max_tasks_per_plan)
        providers = providers[:effort.analysts]
        providers = [p for p in providers if not any(a.provider == p and a.output for a in run.analyses)]
        if run.timing_policy == "graceful" and not self._has_time(run, 0.001):
            providers = [p for p in providers if self._can_continue(run, p, "analysis")]
        run.analyses = [a for a in run.analyses if a.output or a.provider not in providers]
        if not providers:
            return
        profile = get_profile(run.profile)
        run.status = "analyzing"
        what = "the request and files" if files else "the request"
        self._event(run, "stage", f"{joined([name(p) for p in providers])} "
                    f"{'are' if len(providers) > 1 else 'is'} analysing {what} independently, at the same time")
        await self._publish(run)
        system, prompt = prompts.analysis_prompt(run.request, run.materials, files_mod.describe(files), profile)
        prompt += prompts.evidence_block(run.evidence, run.request, "analysis")

        async def one(p: str) -> None:
            call = ModelCall(system, prompt, "analysis", self._out_cap(run),
                             {"request": run.request, "task_title": "Independent analysis"}, attachments=files)
            try:
                res = await self._call(run, p, call, live=(f"analysis:{p}", "Independent analysis"))
                run.analyses.append(Opinion(provider=p, model=res.model, output=res.text))
                self._event(run, "analysis", f"{name(p)} finished its independent analysis", p)
            except ProviderError as e:
                run.analyses.append(Opinion(provider=p, error=str(e).removeprefix(f"{p}: ")))
            await self._publish(run)

        cap = None if run.timing_policy == "graceful" else policy.stage_allowance(run, "analysis", self._time_left(run)[0])
        stopped = await self._together({p: one(p) for p in providers}, cap, lambda: any(a.output for a in run.analyses))
        for p in stopped:
            run.analyses.append(Opinion(provider=p, error=f"still writing after {cap / 60:.0f} minutes, the time set for "
                                        "analysis, so the team went on with the other analyses"))
            self._event(run, "slow", f"{name(p)} was still analysing after {cap / 60:.0f} minutes; the team goes on without "
                        "its analysis to stay within the time limit", p)
        if not any(a.output for a in run.analyses) and not any(p.kind == "analysis" and p.text for p in run.partial_outputs):
            raise NoProviders("no model could analyse the request: " + "; ".join(
                f"{name(a.provider)}: {a.error}" for a in run.analyses if a.error)[:600])
        await self._publish(run, checkpoint=True)

    async def _cross_check(self, run: Run) -> None:
        """Each model reads the others' analyses and compares them with its own."""
        working = self._eligible(run)
        ok = [a for a in run.analyses if a.output and a.provider in working]
        if len(ok) < 2:
            return
        ok = [a for a in ok if not any(c.provider == a.provider and c.output for c in run.cross_checks)]
        if run.timing_policy == "graceful" and not self._has_time(run, 0.001):
            ok = [a for a in ok if self._can_continue(run, a.provider, "cross_check")]
        run.cross_checks = [c for c in run.cross_checks if c.output or c.provider not in {a.provider for a in ok}]
        if not ok:
            self._decide(run)
            return
        profile = get_profile(run.profile)
        run.status = "cross_checking"
        self._event(run, "stage", "Each model is now reading the others' analyses and comparing them with its own")
        await self._publish(run)

        async def one(a: Opinion) -> None:
            others = [o for o in run.analyses if o.output and o.provider != a.provider]
            system, prompt = prompts.cross_check_prompt(run.request, a, others, profile)
            call = ModelCall(system, prompt, "cross_check", min(6000 if run.eris else 5000, self._out_cap(run)),
                             {"request": run.request, "task_title": "Cross-check"})
            try:
                res = await self._call(run, a.provider, call, live=(f"cross:{a.provider}", "Cross-check"))
                scores = prompts.parse_scores(res.text, [o.provider for o in run.analyses if o.output])
                run.cross_checks.append(Opinion(provider=a.provider, model=res.model, output=res.text, scores=scores))
                self._event(run, "cross_check", f"{name(a.provider)} compared the analyses"
                            + (" and scored each approach" if scores else ""), a.provider)
            except ProviderError as e:
                run.cross_checks.append(Opinion(provider=a.provider, error=str(e).removeprefix(f"{a.provider}: ")))
            await self._publish(run)

        cap = None if run.timing_policy == "graceful" else policy.stage_allowance(run, "cross_check", self._time_left(run)[0])
        stopped = await self._together({a.provider: one(a) for a in ok}, cap,
                                       lambda: any(c.output for c in run.cross_checks))
        for p in stopped:
            run.cross_checks.append(Opinion(provider=p, error=f"still writing after {cap / 60:.0f} minutes, the time set "
                                            "for cross-checks"))
        self._decide(run)
        await self._publish(run, checkpoint=True)

    def _decide(self, run: Run) -> None:
        """Average the scores each approach got from the other models and pick the one to build the single answer on."""
        votes = {c.provider: c.scores for c in run.cross_checks if c.scores}
        candidates = [a.provider for a in run.analyses if a.output]
        average: dict[str, float] = {}
        for cand in candidates:
            others = [v[cand] for judge, v in votes.items() if judge != cand and cand in v]
            own = [v[cand] for judge, v in votes.items() if judge == cand and cand in v]
            pool = others or own  # a model's score for itself counts only when nobody else scored it
            if pool:
                average[cand] = round(sum(pool) / len(pool), 1)
        if not average:
            return
        best = max(average, key=lambda p: (average[p], -candidates.index(p)))
        run.consensus = Consensus(best=best, average=average, votes=votes)
        ranked = ", ".join(f"{name(p)} {s:.1f}" for p, s in sorted(average.items(), key=lambda kv: -kv[1]))
        self._event(run, "consensus", f"The models scored each other's approaches ({ranked}). "
                    f"They build one answer together on {name(best)}'s approach.", best)

    @staticmethod
    def _task_files(run: Run, files: list[StoredFile]) -> list[StoredFile]:
        """Tasks see images, PDFs and data directly. Video and audio reach them through the analyses, to save tokens."""
        if not run.analyses:
            return files
        return [f for f in files if f.kind not in ("video", "audio")]

    async def _plan(self, run: Run, load: dict[str, int], files: list[StoredFile] | None = None) -> None:
        profile = get_profile(run.profile)
        run.status = "planning"
        self._event(run, "stage", "Understanding the request and drafting a plan")
        await self._publish(run)

        shared = (prompts.findings_block(run.analyses, run.cross_checks) + prompts.consensus_block(run.consensus)
                  + prompts.evidence_block(run.evidence, run.request, "plan") + self._partial_notes(run, {"analysis", "cross_check"}))
        task_limit = policy.allocate(run, len(self._eligible(run)), self.settings.max_tasks_per_plan).task_limit
        plan = run.plan
        if plan is None or not plan.tasks:
            system, prompt = prompts.plan_prompt(run.request, run.materials, profile, task_limit,
                                                 shared + ("\n\n" + files_mod.describe(files or []) if files else ""),
                                                 self._task_minutes(run))
            planner, result, reason = await self._call_any(
                run, "planner", ModelCall(system, prompt, "plan", 4000, {"request": run.request},
                                          attachments=[] if run.analyses else (files or [])), load, frontier_first=True)
            plan = self._parse_plan(result.text, run)
            plan.planner = planner
            run.plan = plan
            field = str((prompts.parse_json(result.text) or {}).get("field") or "").lower()
            if not run.eris and field in AUTO_FIELDS and field != run.profile:
                run.profile = field
                profile = get_profile(field)
                self._event(run, "field", f"{name(planner)} judged this to be {profile.name.lower()} work, so the rest of the run uses that approach", planner)
            self._event(run, "plan", f"{name(planner)} drafted a plan with {len(plan.tasks)} tasks ({reason})", planner)
        planner = plan.planner or self._eligible(run)[0]

        # Independent plan review by a different provider, at most one revision.
        run.status = "reviewing_plan"
        await self._publish(run, checkpoint=True)
        for _ in range(2):
            review = run.plan_review
            if review is None:
                system, prompt = prompts.plan_review_prompt(run.request, plan, profile,
                                                            prompts.materials_block(run.materials)
                                                            + prompts.evidence_block(run.evidence, run.request, "plan_review"))
                reviewer, rres, _ = await self._call_any(
                    run, "plan reviewer", ModelCall(system, prompt, "plan_review", 2500,
                                                     {"request": run.request, "phase": str(plan.version - 1) if plan.version > 1 else ""},
                                                     attachments=files or []),
                    load, exclude={planner})
                review = self._parse_review(rres.text, reviewer, rres.model, default="accept")
                run.plan_review = review
                self._event(run, "plan_review", f"{name(reviewer)} reviewed the plan: {review.verdict}", reviewer)
            if review.verdict != "revise" or plan.version >= 2:
                break
            system, prompt = prompts.plan_revise_prompt(run.request, plan, review.findings, profile, task_limit,
                                                        self._task_minutes(run))
            try:
                result = await self._call(run, planner, ModelCall(system, prompt, "plan", 4000,
                                           {"request": run.request, "phase": f"revise:{plan.version}"}))
            except ProviderError:
                break  # keep the reviewed plan; the user still approves it
            revised = self._parse_plan(result.text, run)
            revised.planner = planner
            revised.version = plan.version + 1
            plan = run.plan = revised
            run.plan_review = None
            self._event(run, "plan", f"{name(planner)} revised the plan (v{plan.version})", planner)
            await self._publish(run, checkpoint=True)
        if run.plan_review and run.plan_review.verdict != "accept" and not run.require_plan_approval:
            raise ValueError("The plan did not pass review after revision; no task work was started.")
        await self._publish(run, checkpoint=True)

    def _task_minutes(self, run: Run) -> float | None:
        """About how many minutes the tasks will have, so the plan can be sized to finish in time."""
        if not run.time_limit_s:
            return None
        if run.timing_policy == "graceful":
            return max(0.0, (run.time_limit_s - run.elapsed_s) / 60)
        left = run.time_limit_s - run.elapsed_s - run.wrap_up_s
        return max(0.0, (left - policy.finish_reserve(run)) / 60)

    def _parse_plan(self, text: str, run: Run) -> Plan:
        try:
            data = PlanPayload.model_validate(prompts.parse_json(text)).model_dump()
        except (ValidationError, ValueError, TypeError):
            data = {}
        tasks_raw = data.get("tasks") or []
        if len({t["id"] for t in tasks_raw}) != len(tasks_raw):
            tasks_raw = []  # duplicate task identities cannot define a reliable dependency graph
        tasks: list[Task] = []
        seen: set[str] = set()
        task_limit = policy.allocate(run, len(run.team) or 1, self.settings.max_tasks_per_plan).task_limit
        for i, t in enumerate(tasks_raw[:task_limit]):
            if not isinstance(t, dict):
                continue
            tid = re.sub(r"[^A-Za-z0-9_-]", "", str(t.get("id") or f"t{i + 1}"))[:20] or f"t{i + 1}"
            if tid in seen:
                tid = f"t{i + 1}"
            seen.add(tid)
            tasks.append(Task(id=tid, title=str(t.get("title") or f"Task {i + 1}")[:200],
                              description=str(t.get("description") or "")[:4000],
                              depends_on=[str(d) for d in (t.get("depends_on") or []) if isinstance(d, (str, int))]))
        ids = {t.id for t in tasks}
        if any(d not in ids or d == t.id for t in tasks for d in t.depends_on):
            tasks = []
        else:
            edges = sum(len(t.depends_on) for t in tasks)
            self._break_cycles(tasks)
            if sum(len(t.depends_on) for t in tasks) != edges:
                tasks = []  # never silently reinterpret a cyclic dependency graph
        if not tasks:
            self._event(run, "plan_fallback", "The plan schema or dependencies were invalid; a complete-request task must pass plan review")
            tasks = [Task(id="t1", title="Complete the request", description=run.request)]

        def strs(key: str) -> list[str]:
            return [str(x) for x in (data.get(key) or []) if isinstance(x, (str, int, float))][:10]

        return Plan(goal=str(data.get("goal") or run.request)[:2000], assumptions=strs("assumptions"),
                    deliverables=strs("deliverables") or ["Final deliverable"],
                    success_criteria=strs("success_criteria") or ["Fully addresses the request"], tasks=tasks)

    @staticmethod
    def _break_cycles(tasks: list[Task]) -> None:
        """Drop dependency edges that would deadlock the task graph."""
        by_id = {t.id: t for t in tasks}
        state: dict[str, int] = {}

        def visit(tid: str) -> None:
            state[tid] = 1
            task = by_id[tid]
            for d in list(task.depends_on):
                if state.get(d) == 1:
                    task.depends_on.remove(d)
                elif state.get(d) is None:
                    visit(d)
            state[tid] = 2

        for t in tasks:
            if state.get(t.id) is None:
                visit(t.id)

    def _parse_review(self, text: str, reviewer: str, model: str, default: str) -> Review:
        try:
            data = ReviewPayload.model_validate(prompts.parse_json(text))
            allowed = {"accept", "revise"} if default == "accept" else {"pass", "revise", "block"}
            if data.verdict not in allowed:
                raise ValueError("verdict is invalid for this review stage")
        except (ValidationError, ValueError, TypeError):
            return Review(reviewer=reviewer, model=model, verdict="revise",
                          findings=["The review was invalid or incomplete and cannot approve this work. Recheck the requirements and return the required schema."])
        return Review(reviewer=reviewer, model=model, verdict=data.verdict,
                      score=data.score, findings=data.findings[:12])

    async def _run_tasks(self, run: Run, load: dict[str, int], files: list[StoredFile] | None = None) -> None:
        plan = run.plan
        assert plan is not None
        if run.deadline_at and run.timing_policy != "graceful":
            # Task work stops early enough to leave time for writing and testing the final result.
            work_end = run.deadline_at - run.wrap_up_s
            reserve = policy.finish_reserve(run)
            left = work_end - now()
            self._stage_end[run.id] = work_end - min(reserve, max(0.0, left - self._min_call(run)))
        try:
            while True:
                by_id = {t.id: t for t in plan.tasks}
                pending = [t for t in plan.tasks if t.status == "pending"]
                if not pending:
                    return
                ready = [t for t in pending if all(by_id[d].status == "accepted" for d in t.depends_on)]
                if not ready:
                    # Dependency gate: inputs were not accepted, so these tasks cannot start.
                    for t in pending:
                        t.status = "blocked"
                        self._event(run, "blocked", f"'{t.title}' is blocked because a required input was not accepted", task_id=t.id)
                    await self._publish(run, checkpoint=True)
                    return
                if not self._has_time(run, self._min_call(run)):
                    ready = ([t for t in ready if self._task_can_continue(run, t)]
                             if run.timing_policy == "graceful" else [])
                    if not ready:
                        raise OutOfTime("stage")
                if len(ready) > 1:
                    self._event(run, "parallel", f"Running {len(ready)} independent tasks in parallel")
                results = await asyncio.gather(*(self._run_task(run, t, load, files or []) for t in ready), return_exceptions=True)
                stage_over = False
                for r in results:
                    if isinstance(r, OutOfTime) and r.scope == "stage":
                        stage_over = True
                    elif isinstance(r, BaseException):
                        raise r
                if stage_over:
                    raise OutOfTime("stage")
        except OutOfTime as e:
            if e.scope != "stage":
                raise
            self._stop_tasks(run)
            await self._publish(run, checkpoint=True)
        finally:
            self._stage_end.pop(run.id, None)

    def _settle_tasks(self, run: Run) -> list[Task]:
        """Tasks cut off by the time limit: a task with a draft keeps it for the final step, the rest stay unfinished."""
        open_ = [t for t in (run.plan.tasks if run.plan else []) if t.status != "accepted"]
        for t in open_:
            if t.status in ("working", "reviewing", "revising"):
                t.status = "needs_human_review" if self._draft(t) else "pending"
        return open_

    def _stop_tasks(self, run: Run) -> None:
        """The time for task work is used up: keep drafts for the final step, leave the rest unfinished."""
        open_ = self._settle_tasks(run)
        left, _ = self._time_left(run)
        self._event(run, "time", "The time for task work is used up, so SASI moves on to the final result"
                    + (f" with {left / 60:.0f} min left before the wrap-up" if left is not None else "")
                    + (f". Unfinished: {', '.join(t.title for t in open_)}; their drafts go to the final step" if open_ else ""))

    @staticmethod
    def _partial_notes(run: Run, kinds: set[str]) -> str:
        notes = [p for p in run.partial_outputs if p.kind in kinds and p.text][-8:]
        if not notes:
            return ""
        return ("\n\nIncomplete model notes, not accepted work or verified evidence. Recheck any useful claim:\n"
                + "\n\n".join(f"[{name(p.provider)} / {p.label}; incomplete: {p.reason}]\n{p.text[:4000]}" for p in notes))

    @staticmethod
    def _draft(task: Task) -> str:
        """The latest text a model wrote for a task, reviewed or not."""
        return task.output or next((a.output for a in reversed(task.attempts) if a.output), "")

    async def _review_call(self, run: Run, reviewer: str, implementer: str, load: dict[str, int], system: str,
                           prompt: str, task: Task, revision: int, files: list[StoredFile] | None = None) -> Review:
        """An independent review of one attempt. If the reviewer fails, another model reviews it."""
        hints = {"request": run.request, "task_id": task.id, "task_title": task.title, "is_revision": revision > 0}
        phase_index = max(0, len(task.attempts) - 1)
        hints["phase"] = str(phase_index) if phase_index else ""
        try:
            rres = await self._call(run, reviewer, ModelCall(system, prompt, "review", 2500, hints, attachments=files or []))
            return self._parse_review(rres.text, reviewer, rres.model, default="pass")
        except ProviderError:
            alt = [p for p in self._eligible(run) if p not in (implementer, reviewer)]
            if not alt:
                return Review(reviewer="none", verdict="revise", findings=["No independent reviewer was available"])
            reviewer = self.router.pick(alt, run.profile, load, "reviewer", kind="review").provider
            task.reviewer = reviewer
            try:
                rres = await self._call(run, reviewer, ModelCall(system, prompt, "review", 2500, hints, attachments=files or []))
            except ProviderError:
                return Review(reviewer="none", verdict="revise", findings=["No independent reviewer was available"])
            return self._parse_review(rres.text, reviewer, rres.model, default="pass")

    @staticmethod
    def _review_context_hash(run: Run, system: str, prompt: str, files: list[StoredFile]) -> str:
        """Exact review inputs, excluding runtime bookkeeping and old opinions.

        Retain full evidence and source identity even when a prompt uses bounded
        excerpts. A changed omitted source must not inherit an old failure stop.
        """
        plan = run.plan
        context = {
            "system": system, "prompt": prompt, "request": run.request, "materials": run.materials,
            "profile": run.profile, "eris": run.eris,
            "rules": eris.RULES if run.eris else "",
            "evidence": [item.model_dump() for item in run.evidence],
            "file_refs": [item.model_dump() for item in run.files],
            "plan": ({"goal": plan.goal, "assumptions": plan.assumptions, "deliverables": plan.deliverables,
                      "success_criteria": plan.success_criteria,
                      "tasks": [{"id": task.id, "title": task.title, "description": task.description,
                                 "depends_on": task.depends_on, "output": task.output} for task in plan.tasks]}
                     if plan else None),
            "attachments": [{"ref": f.ref(), "text": f.text, "object_uri": f.object_uri,
                             "object_generation": f.object_generation,
                             "data_sha256": hashlib.sha256(f.data).hexdigest()} for f in files],
        }
        return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    async def _final_review_context_hash(self, run: Run, plan: Plan, output: str, profile) -> str:
        system, prompt = prompts.final_eval_prompt(run.request, plan, output, profile,
                                                   prompts.materials_block(run.materials) + prompts.evidence_block(
                                                       run.evidence, run.request + "\n" + "\n".join(plan.success_criteria), "final_eval"))
        files = await self.files.get(run.id) if run.files else []
        return self._review_context_hash(run, system, prompt, files)

    async def _run_task(self, run: Run, task: Task, load: dict[str, int], files: list[StoredFile]) -> None:
        plan = run.plan
        assert plan is not None
        profile = get_profile(run.profile)
        shared = (prompts.findings_block(run.analyses, run.cross_checks, limit=4000) + prompts.consensus_block(run.consensus)
                  + prompts.evidence_block(run.evidence, f"{run.request}\n{task.title}\n{task.description}", "task"))
        task_files = self._task_files(run, files)
        deps = [t for t in plan.tasks if t.id in task.depends_on]
        tried_implementers: set[str] = set()
        previous: str | None = self._draft(task) or None
        findings: list[str] = [f for a in task.attempts if a.review for f in a.review.findings][-12:]
        pending_attempt = (task.attempts[-1] if task.attempts and task.attempts[-1].output
                           and task.attempts[-1].review is None else None)
        phase = str(len(task.attempts)) if task.attempts else ""
        resume_implementer = task.implementer if (pending_attempt or (task.implementer and self._can_continue(
            run, task.implementer, "task", task.id, phase))) else None
        resume_reviewer = task.reviewer
        # Reuse only a known failure for exact unchanged review inputs. This is
        # local to the invocation: old checkpoints without context fingerprints
        # are reviewed afresh, and no previous passing opinion is promoted.
        failed_reviews: dict[str, Review] = {}

        # Up to two implementers (original + one reassignment), each with bounded revisions.
        for assignment in range(2):
            eligible = self._eligible(run)
            if assignment and not any(p not in tried_implementers for p in eligible):
                break
            impl = self.router.pick(eligible, run.profile, load, "implementer", exclude=tried_implementers)
            implementer = resume_implementer if assignment == 0 and resume_implementer in eligible else impl.provider
            tried_implementers.add(implementer)
            rev = self.router.pick(eligible, run.profile, load, "reviewer", exclude={implementer}, kind="review")
            reviewer = resume_reviewer if assignment == 0 and resume_reviewer in eligible else rev.provider
            task.implementer, task.reviewer = implementer, reviewer
            task.route_reason = f"{impl.reason}. {rev.reason}"
            if assignment:
                self._event(run, "reassigned", f"'{task.title}' reassigned to {name(implementer)}", implementer, task.id)

            for revision in range(self.settings.max_revisions_per_task + 1):
                if pending_attempt is not None:
                    attempt, pending_attempt = pending_attempt, None
                    implementer = attempt.provider
                    task.implementer = implementer
                    result = ModelResult(provider=implementer, model=attempt.model, text=attempt.output)
                else:
                    task.status = "working" if revision == 0 else "revising"
                    self._event(run, "working", f"{name(implementer)} is {'revising' if revision else 'working on'} '{task.title}'", implementer, task.id)
                    await self._publish(run)
                    system, prompt = prompts.task_prompt(plan, task, deps, profile, run.materials, findings, previous, shared, run.request)
                    phase = str(len(task.attempts)) if task.attempts else ""
                    try:
                        result = await self._call(run, implementer, ModelCall(
                            system, prompt, "task", self._out_cap(run),
                            {"request": run.request, "task_title": task.title, "task_id": task.id,
                             "is_revision": revision > 0, "phase": phase},
                            attachments=task_files), live=(f"task:{task.id}", task.title))
                    except ProviderError as e:
                        task.attempts.append(Attempt(provider=implementer, error=str(e).removeprefix(f"{implementer}: ")))
                        break  # move on to reassignment
                    attempt = Attempt(provider=implementer, model=result.model, output=result.text, is_revision=revision > 0)
                    task.attempts.append(attempt)

                task.status = "reviewing"
                self._event(run, "reviewing", f"{name(reviewer)} is reviewing '{task.title}'", reviewer, task.id)
                await self._publish(run)
                context = (f"Original user request:\n{run.request}" + prompts.materials_block(run.materials)
                           + prompts.evidence_block(run.evidence, f"{run.request}\n{task.title}\n{task.description}", "review")
                           + "\n\nAccepted dependencies:\n" + "\n\n".join(f"{d.id}: {d.output}" for d in deps))
                system, prompt = prompts.review_prompt(plan, task, result.text, profile, context)
                review_context = self._review_context_hash(run, system, prompt, task_files)
                prior_failure = failed_reviews.get(review_context)
                if prior_failure is not None:
                    attempt.review = prior_failure.model_copy(deep=True)
                    previous, findings = result.text, list(prior_failure.findings)
                    self._event(run, "no_progress", f"'{task.title}' returned an identical previously rejected draft with unchanged "
                                "requirements and evidence. Its findings are retained; no duplicate review was requested.",
                                implementer, task.id)
                    await self._publish(run, checkpoint=True)
                    break  # one different implementer may still resolve the findings
                review = await self._review_call(run, reviewer, implementer, load, system, prompt, task, revision, task_files)
                reviewer = task.reviewer or reviewer
                attempt.review = review
                self._event(run, "review", f"{name(review.reviewer)}: {review.verdict} on '{task.title}'", review.reviewer, task.id)

                if review.verdict == "pass":
                    task.status = "accepted"
                    task.output = result.text
                    task.assurance = "self-reviewed" if review.reviewer == implementer else "evidence-reviewed"
                    self.history.record(implementer, run.profile, True)
                    self._event(run, "accepted", f"'{task.title}' accepted ({task.assurance}) and checkpointed", implementer, task.id)
                    await self._publish(run, checkpoint=True)
                    return
                self.history.record(implementer, run.profile, False)
                if review.verdict in ("revise", "block") and review.score is not None:
                    failed_reviews[review_context] = review.model_copy(deep=True)
                previous, findings = result.text, review.findings
                if review.verdict == "block":
                    break

        task.status = "needs_human_review"
        self._event(run, "needs_human_review", f"'{task.title}' did not pass review within the retry limit; its latest draft "
                    "goes to the final step and is marked as unreviewed", task_id=task.id)
        await self._publish(run, checkpoint=True)

    async def _judge(self, run: Run, plan: Plan, output: str, profile, judges: list[str], round_: int) -> list[Judgement]:
        """Each judge tests the final result on its own, at the same time. A judge that fails or runs out of time
        is left out of that round."""
        system, prompt = prompts.final_eval_prompt(run.request, plan, output, profile,
                                                  prompts.materials_block(run.materials) + prompts.evidence_block(
                                                      run.evidence, run.request + "\n" + "\n".join(plan.success_criteria), "final_eval"))
        final = run.final
        assert final is not None
        artifact_hash = hashlib.sha256(output.encode("utf-8")).hexdigest()
        artifact_version = final.artifact_version
        files = await self.files.get(run.id) if run.files else []

        async def one(p: str) -> Judgement | None:
            try:
                res = await self._call(run, p, ModelCall(system, prompt, "final_eval", 3000,
                                                         {"request": run.request, "round": round_}, attachments=files))
            except (ProviderError, OutOfTime):
                return None
            try:
                data = JudgementPayload.model_validate(prompts.parse_json(res.text))
            except (ValidationError, ValueError, TypeError):
                data = None
            j = Judgement(judge=p, model=res.model, round=round_, artifact_hash=artifact_hash,
                          artifact_version=artifact_version, score=data.score if data else None,
                          meets_criteria=data.meets_criteria if data else None,
                          must_fix=data.must_fix[:10] if data else [],
                          gaps=data.gaps[:10] if data else ["The review was invalid or incomplete; it cannot verify this artifact."])
            for x in data.limitations[:10] if data else []:
                if x not in final.limitations:
                    final.limitations.append(x)
            self._event(run, "judged", f"{name(p)} scored the result {j.score if j.score is not None else 'n/a'}/10"
                        + (f" and found {len(j.must_fix)} thing{'s' if len(j.must_fix) != 1 else ''} to fix" if j.must_fix else ""), p)
            final.judges.append(j)
            await self._publish(run, checkpoint=True)
            return j

        results = await asyncio.gather(*(one(p) for p in judges), return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r
        return [r for r in results if r is not None]

    async def _integrate(self, run: Run, load: dict[str, int]) -> None:
        plan = run.plan
        assert plan is not None
        profile = get_profile(run.profile)
        accepted = [t for t in plan.tasks if t.status == "accepted"]
        missing = [t for t in plan.tasks if t.status != "accepted"]
        drafts = [(t.title, self._draft(t)) for t in missing if self._draft(t)]
        if not run.final and not accepted and not drafts and not any(a.output for a in run.analyses):
            if any(p.text for p in run.partial_outputs):
                raise OutOfTime("run")
            run.status = "failed"
            run.error = "No task produced any work, so there is nothing to put together."
            self._event(run, "failed", run.error)
            return

        run.status = "integrating"
        self._event(run, "stage", f"Writing the final result from {len(accepted)} accepted output{'s' if len(accepted) != 1 else ''}"
                    + (f" and {len(drafts)} unreviewed draft{'s' if len(drafts) != 1 else ''}" if drafts else ""))
        await self._publish(run)
        shared = (prompts.findings_block(run.analyses, run.cross_checks, limit=3000) + prompts.consensus_block(run.consensus)
                  + prompts.materials_block(run.materials) + prompts.evidence_block(
                      run.evidence, run.request + "\n" + "\n".join(plan.deliverables), "integrate"))
        system, prompt = prompts.integrate_prompt(run.request, plan, accepted, missing, profile, shared, drafts)
        if run.final and run.final.output:
            final = run.final
            integrator = final.integrator or self._eligible(run)[0]
            took = policy.expected_seconds(run, "integrate")
        else:
            started = time.monotonic()
            integrator, result, _ = await self._call_any(run, "integrator", ModelCall(
                system, prompt, "integrate", self._out_cap(run),
                {"request": run.request, "sections": [(t.title, t.output) for t in accepted] + drafts}), load,
                live=("final", "Final result"), frontier_first=True)
            took = time.monotonic() - started
            final = FinalResult(output=result.text, integrator=integrator,
                                best_approach=run.consensus.best if run.consensus else None)
            run.final = final  # shown while the other models test it
        await self._verify_final(run)
        self._event(run, "integrated", f"{name(integrator)} wrote the single combined result", integrator)
        await self._publish(run, checkpoint=True)

        # Every other model tests the one result; the integrator fixes what they find, then they score it again,
        # as long as the time allows.
        judges = [p for p in self._eligible(run) if p != integrator] or [integrator]
        if run.compare_all is not True and (run.mode == "quick" or
                                            (run.mode == "adaptive" and not policy.complex_request(run))):
            judges = judges[:1]
        sections = [(t.title, t.output) for t in accepted]
        latest: list[Judgement] = []
        tested = True
        final_context = await self._final_review_context_hash(run, plan, final.output, profile)
        context_changed = bool(final.stalled_context_hash and final.stalled_context_hash != final_context)
        if context_changed:
            final.stalled_context_hash = ""
            final.limitations = [note for note in final.limitations if note != STALLED_FINAL_LIMITATION]
        stalled = final.stalled_context_hash == final_context
        if stalled:
            final.limitations.insert(0, STALLED_FINAL_LIMITATION)
            latest = [j for j in final.judges if j.artifact_hash == final.artifact_hash]
            tested = bool(latest)
            self._event(run, "no_progress", "Resumed the unchanged stalled result with its existing review findings; "
                        "no repeated review or fix was started.")
        prior_round = max((j.round for j in final.judges), default=-1)
        current_round = max((j.round for j in final.judges if j.artifact_hash == final.artifact_hash), default=prior_round + 1)
        start_round = prior_round + 1 if context_changed else current_round
        rounds = range(start_round, start_round + max(0, self.settings.max_final_fix_rounds - final.fixes) + 1)
        for round_ in ([] if stalled else rounds):
            reviewed_context = await self._final_review_context_hash(run, plan, final.output, profile)
            judged = [j for j in final.judges if j.round == round_ and j.artifact_hash == final.artifact_hash]
            pending_judges = [p for p in judges if p not in {j.judge for j in judged}]
            can_start = self._has_time(run, 3 * self._min_call(run))
            if not can_start:
                pending_judges = [p for p in pending_judges if self._can_continue(run, p, "final_eval", phase=str(round_))]
            if not can_start and not pending_judges and not judged:
                tested = round_ > 0
                final.limitations.append("The time limit was reached before the other models could "
                                         + ("re-test the fixed result." if round_ else "test the result."))
                break
            self._event(run, "stage", f"{joined([name(p) for p in judges])} {'are' if len(judges) > 1 else 'is'} "
                        f"{'re-testing the fixed' if round_ else 'testing the'} result against the criteria and edge cases")
            await self._publish(run)
            judged += await self._judge(run, plan, final.output, profile, pending_judges, round_)
            if judged:
                latest = judged
            must = list(dict.fromkeys(m for j in judged for m in j.must_fix))
            current_hashes = self._current_artifact_hashes(run, final)
            must.extend(c.summary for c in final.checks if c.artifact_hash in current_hashes
                        and c.kind != "model_review" and c.status == "fail" and c.summary not in must)
            for j in judged:
                if j.meets_criteria is False and not j.must_fix:
                    must.append("The reviewer marked success criteria unmet. Recheck every criterion and correct the unmet requirements.")
            if not must or final.fixes >= self.settings.max_final_fix_rounds:
                break
            fix_phase = f"fix:{round_}"
            if not self._has_time(run, took * 1.3 + 3 * self._min_call(run)) and not self._can_continue(run, integrator, "integrate", phase=fix_phase):
                final.limitations.append("Not fixed because the time limit was reached: " + "; ".join(must[:5]))
                break
            self._event(run, "fixing", f"{name(integrator)} is fixing {len(must)} problem{'s' if len(must) != 1 else ''} "
                        "the other models found", integrator)
            await self._publish(run)
            system, prompt = prompts.fix_prompt(run.request, plan, final.output, must, profile)
            extra = await self._expand_for_gaps(run, load, must)
            prompt += (prompts.materials_block(run.materials) + prompts.evidence_block(run.evidence, run.request + "\n" + "\n".join(must), "fix")
                       + ("\n\nNew independently reviewed gap investigation:\n" + extra if extra else ""))
            try:
                fixed = await self._call(run, integrator, ModelCall(
                    system, prompt, "integrate", self._out_cap(run),
                    {"request": run.request, "task_title": "Final fixes", "sections": sections, "phase": fix_phase}),
                    live=("final", "Fixing the final result"))
            except (ProviderError, OutOfTime) as e:
                why = "the time limit was reached" if isinstance(e, OutOfTime) else "the model failed"
                final.limitations.append(f"These problems found by the other models could not be fixed ({why}): "
                                         + "; ".join(must[:5]))
                break
            if (fixed.text == final.output and reviewed_context ==
                    await self._final_review_context_hash(run, plan, final.output, profile)):
                final.stalled_context_hash = reviewed_context
                final.limitations.insert(0, STALLED_FINAL_LIMITATION)
                self._event(run, "no_progress", "The fix returned identical bytes with unchanged requirements and evidence. "
                            "Retained the existing findings and stopped repeated final reviews and fixes.", integrator)
                self._partial.pop(run.id, None)
                await self._publish(run, checkpoint=True)
                break
            final.stalled_context_hash = ""
            final.output = fixed.text
            final.fixes += 1
            await self._verify_final(run)
            self._partial.pop(run.id, None)
            await self._publish(run, checkpoint=True)

        self._refresh_final_assessment(run, final)
        latest = [j for j in latest if j.artifact_hash == final.artifact_hash]
        if not latest and tested:
            final.limitations.append("No other model was available to test and score the final result.")
        elif latest and judges == [integrator]:
            final.limitations.append("Only one model was available, so it scored its own result.")
        if drafts:
            final.limitations.insert(0, "Built partly on drafts that did not pass review: " + ", ".join(t for t, _ in drafts))
        undone = [t.title for t in missing if not self._draft(t)]
        if undone:
            final.limitations.insert(0, "Unfinished: " + ", ".join(undone))
        if not accepted and not drafts:
            final.limitations.insert(0, "No task finished, so this was written from the models' analyses.")
        if any(t.assurance == "self-reviewed" for t in accepted):
            final.limitations.append("Some work was reviewed by the same model that wrote it, because no other model was available.")
        final.limitations.append("Model reviews are opinions, not executed tests. See the checks for exactly what was verified on this artifact.")
        final.limitations = list(dict.fromkeys(final.limitations))[:14]
        who = joined(list(dict.fromkeys(name(j.judge) for j in latest))) if latest else "no other model"
        self._deliver(run, final, f"Delivered one result, scored {final.score if final.score is not None else 'n/a'}/10 by {who}"
                      + (f" after {final.fixes} round{'s' if final.fixes != 1 else ''} of fixes" if final.fixes else ""))

    def _deliver(self, run: Run, final: FinalResult, message: str) -> None:
        run.final = final
        self._refresh_final_assessment(run, final)
        run.status = "completed"
        if run.time_limit_s:
            message += (f", after {run.elapsed_s / 60:.0f} min (new-work target {run.time_limit_s / 60:.0f} min; "
                        f"total ceiling {run.total_time_limit_s / 60:.0f} min)" if run.timing_policy == "graceful" else
                        f", in {run.elapsed_s / 60:.0f} of {run.time_limit_s / 60:.0f} min")
        self._event(run, "completed", message, final.evaluator or final.integrator)

    async def _expand_for_gaps(self, run: Run, load: dict[str, int], findings: list[str]) -> str:
        """Spend remaining effort on one bounded, independently reviewed investigation."""
        maximum = max(0, min(getattr(self.settings, "max_gap_tasks_per_run", 1),
                             getattr(self.settings, "max_replans_per_run", 1)))
        if (not run.plan or run.mode == "quick" or not policy.complex_request(run)
                or run.gap_tasks_added >= maximum or not findings):
            return ""
        needed = (policy.expected_seconds(run, "task") + policy.expected_seconds(run, "review")
                  + policy.finish_reserve(run))
        if not self._has_time(run, needed):
            return ""
        run.gap_tasks_added += 1
        tid = f"gap{run.gap_tasks_added}"
        while any(t.id == tid for t in run.plan.tasks):
            tid += "x"
        task = Task(id=tid, title="Investigate unresolved verification gaps",
                    description="Resolve these specific findings using the supplied evidence. Distinguish what is supported "
                    "from what remains unknown; do not invent tool results.\n- " + "\n- ".join(findings[:5]))
        run.plan.tasks.append(task)
        run.plan.version += 1
        self._event(run, "replan", "Added one investigation because review found unresolved requirements", task_id=task.id)
        files = await self.files.get(run.id) if run.files else []
        await self._run_task(run, task, load, files)
        return task.output if task.status == "accepted" else ""

    # ------------------------------------------------------------------ wrap-up

    async def _wrap_up(self, run: Run) -> None:
        """The run's working time is used up. Stop new work and deliver the complete result from what is done."""
        run.wrapping_up = True
        if run.timing_policy == "graceful" and run.finalization_deadline_elapsed_s is None:
            run.finalization_deadline_elapsed_s = min(run.total_time_limit_s, run.elapsed_s + policy.finalization_allowance(run))
        self._stage_end.pop(run.id, None)
        run.live = {}
        left, _ = self._time_left(run)
        if run.final and run.final.output:
            final = run.final
            final.limitations.insert(0, "The run reached its time limit, so the result was delivered as it stood"
                                     + (" after the last round of fixes." if final.fixes else "."))
            final.limitations = list(dict.fromkeys(final.limitations))[:14]
            await self._verify_final(run)
            self._deliver(run, final, "Time limit reached: delivered the final result as it stood")
            return

        plan = run.plan
        profile = get_profile(run.profile)
        self._settle_tasks(run)
        tasks = plan.tasks if plan else []
        accepted = [t for t in tasks if t.status == "accepted"]
        drafts = [(t.title, self._draft(t)) for t in tasks if t.status != "accepted" and self._draft(t)]
        drafts.extend((f"{name(p.provider)}: {p.label} (incomplete)", p.text)
                      for p in run.partial_outputs if p.text and p.kind not in {"review", "plan_review", "final_eval"})
        partial = self._partial.pop(run.id, "")
        if partial:
            drafts.insert(0, ("Final result, cut off by the time limit", partial))
        try:
            writers = self._eligible(run)
        except NoProviders:
            writers = []
        writers = [p for p in writers if p in FRONTIER] + [p for p in writers if p not in FRONTIER]
        if run.timing_policy == "graceful":
            # One finalization request only. Resume may deliver its retained text,
            # but must not reset the window or purchase another synthesis attempt.
            already_tried = any(":wrap_up:" in key and elapsed > 0 for key, elapsed in run.call_elapsed_s.items())
            writers = [] if already_tried else writers[:1]
        run.status = "integrating"
        self._event(run, "wrap_up", ("The new-work target was reached; active work has finished or its grace ended. SASI stopped new work"
                                    if run.timing_policy == "graceful" else "Time is nearly up, so SASI stopped new work")
                    + (f" and {name(writers[0])} is writing the complete final result from the work done so far"
                       f" ({left / 60:.1f} min left)" if writers and left is not None else ""))
        await self._publish(run, checkpoint=True)
        system, prompt = prompts.wrap_up_prompt(
            run.request, plan, profile, max(1.0, (left or 60) / 60), "", accepted, drafts,
            run.analyses, prompts.consensus_block(run.consensus))
        prompt += prompts.materials_block(run.materials) + prompts.evidence_block(run.evidence, run.request, "wrap_up")
        for p in writers:
            if not self._has_time(run, self._min_call(run)):
                break
            try:
                res = await self._call(run, p, ModelCall(
                    system, prompt, "wrap_up", self._out_cap(run),
                    {"request": run.request, "sections": [(t.title, t.output) for t in accepted] + drafts}),
                    live=("final", "Final result (time limit)"))
            except (ProviderError, OutOfTime):
                continue
            final = FinalResult(output=res.text, integrator=p, best_approach=run.consensus.best if run.consensus else None)
            final.limitations = [("Written in the last minutes of the run's time limit from the team's work so far; the "
                                  "other models did not test it.")]
            if drafts:
                final.limitations.append("Built partly on drafts that did not pass review: " + ", ".join(t for t, _ in drafts))
            undone = [t.title for t in tasks if t.status != "accepted" and not self._draft(t)]
            if undone:
                final.limitations.append("Not started before the time limit: " + ", ".join(undone))
            final.limitations.append("Assurance level: not tested. Check it before you rely on it.")
            run.final = final
            await self._verify_final(run)
            self._deliver(run, final, f"Time limit reached: {name(p)} wrote the complete final result from the work done")
            return

        # No model could write it in time: deliver the best work there is.
        # A failed final request may have produced the most useful draft after the
        # initial candidates were gathered. Preserve it in the delivered fallback too.
        latest_wrap = next((p for p in reversed(run.partial_outputs) if p.kind == "wrap_up" and p.text), None)
        best = "\n\n".join(f"## {t.title}\n\n{t.output}" for t in accepted)
        if latest_wrap:
            best = "## Incomplete final synthesis (not reviewed)\n\n" + latest_wrap.text + ("\n\n" + best if best else "")
        elif not best and drafts:
            best = "\n\n".join(f"## {title} (draft, not reviewed)\n\n{text}" for title, text in drafts)
        if not best:
            best = "\n\n".join(f"## {name(a.provider)}'s analysis\n\n{a.output}" for a in run.analyses if a.output)
        if not best:
            run.status = "failed"
            run.error = "The time limit was reached before any model produced a result."
            self._event(run, "failed", run.error)
            return
        final = FinalResult(output=best, limitations=[(
            "The time limit was reached before the parts could be merged, so this is the team's work as it stood, "
            "section by section. Nothing here was tested.")])
        run.final = final
        await self._verify_final(run)
        self._deliver(run, final, "Time limit reached: delivered the team's work as it stood")
