"""Workflow controller.

Owns run state and decides progression. Application rules, not model opinion,
control what happens next:

  pick the team -> independent analyses -> cross-checks and scores -> plan ->
  independent plan review -> (user approval) -> parallel tasks behind dependency
  gates -> independent cross-review per task (bounded revise / reassign) ->
  integrate -> the other models test the result and it is fixed -> deliver with
  evidence, limitations and usage.

Time is a run's limit, not tokens or calls. Every call is bounded by the time
left, each stage gets a share so one slow model cannot hold the run up, and in
the last minutes (the wrap-up) SASI stops new work and delivers the complete
result from what is done. The clock pauses while a plan waits for approval.

Accepted work is checkpointed, so an interrupted run resumes without redoing it.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from collections.abc import Awaitable, Callable

from .. import files as files_mod
from ..config import Settings
from ..files import FileStore, StoredFile
from ..gateway import fit as fitting
from ..gateway.base import ModelCall, ModelResult, ProviderError, reply_room
from ..gateway.catalog import name, reads, seat
from ..gateway.registry import FRONTIER, ModelRegistry
from ..gateway.routes import VIA, lasting_problem
from ..models import (TERMINAL, Attempt, Consensus, Event, FinalResult, Judgement, LiveText, Opinion, Plan, Review, Run,
                      Task, now)
from ..store.base import RunStore
from . import eris, prompts
from .profiles import AUTO_FIELDS, get_profile
from .router import PerformanceHistory, Router

log = logging.getLogger("sasi.controller")

# Shares of a run's time limit. Analyses and cross-checks stop waiting for a slow model after their share, and
# task work stops early enough to leave FINISH_SHARE for writing and testing the final result.
ANALYSIS_SHARE = 0.20
CROSS_CHECK_SHARE = 0.10
FINISH_SHARE = 0.25


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


class Controller:
    def __init__(self, settings: Settings, registry: ModelRegistry, store: RunStore, history: PerformanceHistory,
                 files: FileStore | None = None):
        self.settings = settings
        self.registry = registry
        self.store = store
        self.files = files or FileStore()
        self._last_publish: dict[str, float] = {}
        self.history = history
        self.router = Router(history)
        self._tasks: dict[str, asyncio.Task] = {}
        self._approvals: dict[str, asyncio.Event] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._unhealthy: dict[str, set[str]] = {}  # run id -> providers that failed this run
        self._stage_end: dict[str, float] = {}  # run id -> when the current stage's share of time ends
        self._partial: dict[str, str] = {}  # run id -> a final result that was cut off by the time limit
        self._trim_noted: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

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
        await self.files.delete(run.id)
        return await self.store.delete_run(run.id)

    async def start(self, run: Run) -> None:
        run.simulated = self.registry.live_count == 0
        self._set_limit(run)
        await self.store.save(run, checkpoint=True)
        self._launch(run)

    def _set_limit(self, run: Run) -> None:
        limit = max(0.0, self.settings.run_time_limit_minutes * 60)
        run.time_limit_s = limit
        run.wrap_up_s = min(max(0.0, self.settings.wrap_up_minutes * 60), limit / 4) if limit else 0.0

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
        if not run.time_limit_s:  # a run from before time limits
            self._set_limit(run)
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

    def _check_budget(self, run: Run) -> None:
        """Optional caps on calls and tokens (0 means no cap). Time is the main limit."""
        s = self.settings
        if s.max_model_calls_per_run and run.usage.calls >= s.max_model_calls_per_run:
            raise BudgetExceeded(f"reached the limit of {s.max_model_calls_per_run} model calls for one run")
        limit = s.eris_max_tokens_per_run if run.eris else s.max_tokens_per_run
        if limit and run.usage.total_tokens >= limit:
            raise BudgetExceeded(f"reached the limit of {limit:,} tokens for one run")

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
            return
        t = now()
        run.clock_started_at = t
        run.deadline_at = t + max(run.time_limit_s - run.used_s, run.wrap_up_s + 2 * self._min_call(run))

    @staticmethod
    def _clock_stop(run: Run) -> None:
        if run.clock_started_at:
            run.used_s += max(0.0, now() - run.clock_started_at)
            run.clock_started_at = None
        run.deadline_at = None

    def _time_left(self, run: Run) -> tuple[float | None, str]:
        """Seconds a call may still use, and which limit ends it: the stage's share ("stage") or the run ("run")."""
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
        left, _ = self._time_left(run)
        return left is None or left >= seconds

    @staticmethod
    def _share(run: Run, fraction: float) -> float | None:
        return run.time_limit_s * fraction if run.time_limit_s else None

    def _out_cap(self, run: Run) -> int:
        """Longest answer one call may write. Eris runs need room for a whole training script."""
        s = self.settings
        return s.eris_max_output_tokens_per_call if run.eris else s.max_output_tokens_per_call

    def _live(self, run: Run, call: ModelCall, provider: str, key: str, label: str) -> None:
        """Stream this call's text to the screen while the model writes it."""
        live = run.live[key] = LiveText(provider=provider, label=label)

        async def on_text(piece: str) -> None:
            live.text += piece
            t = time.monotonic()
            if t - self._last_publish.get(run.id, 0) >= 0.5:
                self._last_publish[run.id] = t
                await self._publish(run)

        call.on_text = on_text

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
        if note and (run.id, f"{provider}:{call.kind}") not in self._trim_noted:
            self._trim_noted.add((run.id, f"{provider}:{call.kind}"))
            self._event(run, "trimmed", f"Trimmed {note} so the input fits {name(provider)}'s context window "
                        f"({context:,} tokens). Models with larger windows read all of it.", provider)

    async def _call_inner(self, run: Run, provider: str, call: ModelCall, live: tuple[str, str] | None) -> ModelResult:
        adapter = self.registry.adapters.get(provider)
        if adapter is None:  # its key was removed while the run was going
            self._unhealthy.setdefault(run.id, set()).add(provider)
            run.skipped[provider] = "its key was removed during the run"
            raise ProviderError(provider, "its key was removed")
        call = dataclasses.replace(call)  # this model's own copy: trimming for one model never reaches another
        info = seat(provider)
        model_max = info.max_output if info else 32_000
        # Half the model's reply limit for the answer leaves the other half for its thinking.
        call.max_output_tokens = max(500, min(call.max_output_tokens, self._out_cap(run), model_max // 2))
        if run.eris:
            call.rules = eris.RULES  # every model, on every step, follows the Eris guidebook
        base_timeout = 1200 if (call.attachments or run.eris or call.max_output_tokens > 8000) else 600
        last: Exception | None = None
        again = shrunk = roomier = 0
        while True:
            left, scope = self._time_left(run)
            timeout: float = base_timeout
            if left is not None:
                if left < self._min_call(run):
                    raise OutOfTime(scope)
                timeout = min(timeout, left)
            self._check_budget(run)
            self._fit(run, provider, call, model_max)
            run.usage.calls += 1
            if live:
                self._live(run, call, provider, *live)
            try:
                result = await asyncio.wait_for(adapter.generate(call), timeout=timeout)
                if not result.text.strip():  # adapters raise for this already; an empty answer is never recorded
                    raise ProviderError(provider, "the model returned no answer text", retryable=True)
            except ProviderError as e:
                last = e
                stated = fitting.context_error(str(e))
                if stated is not None and shrunk < 2:
                    shrunk += 1
                    limit = self.registry.learn_context(provider, stated)
                    self._event(run, "retry", f"{name(provider)} said the input was too long, so SASI trimmed it to fit "
                                f"{limit:,} tokens and is trying again", provider)
                    continue
                if "no answer text" in str(e) and roomier < 1:
                    roomier += 1
                    call.max_output_tokens = min(int(call.max_output_tokens * 1.5) + 2_000, model_max)
                    self._event(run, "retry", f"{name(provider)} wrote no answer (it can spend its whole allowance "
                                "thinking), so SASI is asking again with more room", provider)
                    continue
                if e.retryable and again < 1:
                    again += 1
                    self._event(run, "retry", f"{name(provider)} had a temporary error, retrying once", provider)
                    await asyncio.sleep(2)
                    continue
                break
            except asyncio.TimeoutError:
                if left is not None and timeout >= left - 0.5:
                    raise OutOfTime(scope) from None  # the time limit ended this call, not a fault of the model
                last = ProviderError(provider, f"timed out after {timeout:.0f}s", retryable=True)
                if again < 1:
                    again += 1
                    continue
                break
            u = run.usage
            u.input_tokens += result.input_tokens
            u.output_tokens += result.output_tokens
            bp = u.by_provider.setdefault(provider, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            bp["calls"] += 1
            bp["input_tokens"] += result.input_tokens
            bp["output_tokens"] += result.output_tokens
            self._record(run, call, result)
            self.registry.sync(provider)  # the route that answered may have changed
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
                        frontier_first: bool = False) -> tuple[str, ModelResult, str]:
        """Pick a provider for a role; on provider failure fall back to the next one and record it.
        Planning and writing the final result go to a frontier model when one works."""
        tried: set[str] = set()
        while True:
            eligible = [p for p in self._eligible(run) if p not in tried]
            if not eligible:
                raise NoProviders(f"every model failed as {role}")
            if frontier_first:
                eligible = [p for p in eligible if p in FRONTIER] or eligible
            kind = "review" if "review" in role or "evaluator" in role else "work"
            choice = self.router.pick(eligible, run.profile, load, role, exclude, kind=kind)
            try:
                return choice.provider, await self._call(run, choice.provider, call, live), choice.reason
            except ProviderError:
                tried.add(choice.provider)
                self._event(run, "fallback", f"Reassigning {role} after {name(choice.provider)} failed")

    # ------------------------------------------------------------------ main flow

    @staticmethod
    def _estimate(run: Run, files: list[StoredFile]) -> int:
        """Rough size of the run's input in tokens, for choosing models whose context window holds it."""
        texts = sum(fitting.tokens(f.text) for f in files if f.text)
        pdfs = sum(f.size // fitting.PDF_BYTES_PER_TOKEN for f in files if f.kind == "pdf")
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
            msg += (f". Time limit {left / 60:.0f} min; the last {run.wrap_up_s / 60:.0f} min are kept for delivering the "
                    "complete result")
        if run.skipped:
            msg += ". Not available: " + "; ".join(f"{name(p)} ({why[:160]})" for p, why in run.skipped.items())
        self._event(run, "team", msg)

    async def _execute(self, run: Run) -> None:
        load: dict[str, int] = {}
        files: list[StoredFile] = []
        self._clock_start(run)
        try:
            try:
                files = await self.files.get(run.id) if run.files else []
                self._note_team(run, self._estimate(run, files))
                await self._publish(run)
                if run.compare_all and not any(a.output for a in run.analyses):
                    run.analyses = []
                    await self._analyze(run, files)
                if run.compare_all and not run.cross_checks:
                    await self._cross_check(run)
                if not run.plan or not run.plan.tasks:
                    await self._plan(run, load, files)
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
            run.status = "cancelled"
            self._event(run, "cancelled", "Run cancelled. Accepted work is kept.")
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
            self._clock_stop(run)
            if run.status in TERMINAL:
                run.finished_at = now()
            run.live = {}
            await self._publish(run, checkpoint=True)

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
        profile = get_profile(run.profile)
        run.status = "analyzing"
        what = "the request and files" if files else "the request"
        self._event(run, "stage", f"{joined([name(p) for p in providers])} "
                    f"{'are' if len(providers) > 1 else 'is'} analysing {what} independently, at the same time")
        await self._publish(run)
        system, prompt = prompts.analysis_prompt(run.request, run.materials, files_mod.describe(files), profile)

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

        cap = self._share(run, ANALYSIS_SHARE)
        stopped = await self._together({p: one(p) for p in providers}, cap, lambda: any(a.output for a in run.analyses))
        for p in stopped:
            run.analyses.append(Opinion(provider=p, error=f"still writing after {cap / 60:.0f} minutes, the time set for "
                                        "analysis, so the team went on with the other analyses"))
            self._event(run, "slow", f"{name(p)} was still analysing after {cap / 60:.0f} minutes; the team goes on without "
                        "its analysis to stay within the time limit", p)
        if not any(a.output for a in run.analyses):
            raise NoProviders("no model could analyse the request: " + "; ".join(
                f"{name(a.provider)}: {a.error}" for a in run.analyses if a.error)[:600])
        await self._publish(run, checkpoint=True)

    async def _cross_check(self, run: Run) -> None:
        """Each model reads the others' analyses and compares them with its own."""
        working = self._eligible(run)
        ok = [a for a in run.analyses if a.output and a.provider in working]
        if len(ok) < 2:
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

        cap = self._share(run, CROSS_CHECK_SHARE)
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

        shared = prompts.findings_block(run.analyses, run.cross_checks) + prompts.consensus_block(run.consensus)
        system, prompt = prompts.plan_prompt(run.request, run.materials, profile, self.settings.max_tasks_per_plan,
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

        # Independent plan review by a different provider, at most one revision.
        run.status = "reviewing_plan"
        await self._publish(run)
        for round_ in range(2):
            system, prompt = prompts.plan_review_prompt(run.request, plan, profile)
            reviewer, rres, _ = await self._call_any(
                run, "plan reviewer", ModelCall(system, prompt, "plan_review", 2500, {"request": run.request}),
                load, exclude={planner})
            review = self._parse_review(rres.text, reviewer, rres.model, default="accept")
            run.plan_review = review
            self._event(run, "plan_review", f"{name(reviewer)} reviewed the plan: {review.verdict}", reviewer)
            if review.verdict != "revise" or round_ == 1:
                break
            system, prompt = prompts.plan_revise_prompt(run.request, plan, review.findings, profile, self.settings.max_tasks_per_plan,
                                                        self._task_minutes(run))
            try:
                result = await self._call(run, planner, ModelCall(system, prompt, "plan", 4000, {"request": run.request}))
            except ProviderError:
                break  # keep the reviewed plan; the user still approves it
            revised = self._parse_plan(result.text, run)
            revised.planner = planner
            revised.version = plan.version + 1
            plan = run.plan = revised
            self._event(run, "plan", f"{name(planner)} revised the plan (v{plan.version})", planner)
        await self._publish(run, checkpoint=True)

    def _task_minutes(self, run: Run) -> float | None:
        """About how many minutes the tasks will have, so the plan can be sized to finish in time."""
        if not run.time_limit_s:
            return None
        left = run.time_limit_s - run.elapsed_s - run.wrap_up_s
        return max(1.0, (left - run.time_limit_s * FINISH_SHARE) / 60)

    def _parse_plan(self, text: str, run: Run) -> Plan:
        data = prompts.parse_json(text) or {}
        tasks_raw = data.get("tasks") or []
        tasks: list[Task] = []
        seen: set[str] = set()
        for i, t in enumerate(tasks_raw[: self.settings.max_tasks_per_plan]):
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
        for t in tasks:
            t.depends_on = [d for d in t.depends_on if d in ids and d != t.id]
        self._break_cycles(tasks)
        if not tasks:
            self._event(run, "plan_fallback", "The plan could not be read; using a single-task plan")
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
        data = prompts.parse_json(text)
        if not data:
            # An unreadable review is not a pass.
            return Review(reviewer=reviewer, model=model, verdict="revise" if default == "pass" else default,
                          findings=["Reviewer reply was not valid JSON: " + text[:300]])
        verdict = str(data.get("verdict", default)).lower().strip()
        if verdict not in ("pass", "revise", "block", "accept"):
            verdict = default
        score = data.get("score")
        return Review(reviewer=reviewer, model=model, verdict=verdict,
                      score=float(score) if isinstance(score, (int, float)) else None,
                      findings=[str(f) for f in (data.get("findings") or [])][:12])

    async def _run_tasks(self, run: Run, load: dict[str, int], files: list[StoredFile] | None = None) -> None:
        plan = run.plan
        assert plan is not None
        if run.deadline_at:
            # Task work stops early enough to leave time for writing and testing the final result.
            work_end = run.deadline_at - run.wrap_up_s
            reserve = self._share(run, FINISH_SHARE) or 0.0
            left = work_end - now()
            self._stage_end[run.id] = work_end - min(reserve, left / 2)
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
    def _draft(task: Task) -> str:
        """The latest text a model wrote for a task, reviewed or not."""
        return task.output or next((a.output for a in reversed(task.attempts) if a.output), "")

    async def _review_call(self, run: Run, reviewer: str, implementer: str, load: dict[str, int], system: str,
                           prompt: str, task: Task, revision: int) -> Review:
        """An independent review of one attempt. If the reviewer fails, another model reviews it."""
        hints = {"request": run.request, "task_id": task.id, "task_title": task.title, "is_revision": revision > 0}
        try:
            rres = await self._call(run, reviewer, ModelCall(system, prompt, "review", 2500, hints))
            return self._parse_review(rres.text, reviewer, rres.model, default="pass")
        except ProviderError:
            alt = [p for p in self._eligible(run) if p not in (implementer, reviewer)]
            if not alt:
                return Review(reviewer="none", verdict="revise", findings=["No independent reviewer was available"])
            reviewer = self.router.pick(alt, run.profile, load, "reviewer", kind="review").provider
            task.reviewer = reviewer
            try:
                rres = await self._call(run, reviewer, ModelCall(system, prompt, "review", 2500, hints))
            except ProviderError:
                return Review(reviewer="none", verdict="revise", findings=["No independent reviewer was available"])
            return self._parse_review(rres.text, reviewer, rres.model, default="pass")

    async def _run_task(self, run: Run, task: Task, load: dict[str, int], files: list[StoredFile]) -> None:
        plan = run.plan
        assert plan is not None
        profile = get_profile(run.profile)
        shared = prompts.findings_block(run.analyses, run.cross_checks, limit=4000) + prompts.consensus_block(run.consensus)
        task_files = self._task_files(run, files)
        deps = [t for t in plan.tasks if t.id in task.depends_on]
        tried_implementers: set[str] = set()

        # Up to two implementers (original + one reassignment), each with bounded revisions.
        for assignment in range(2):
            eligible = self._eligible(run)
            impl = self.router.pick(eligible, run.profile, load, "implementer", exclude=tried_implementers)
            implementer = impl.provider
            tried_implementers.add(implementer)
            rev = self.router.pick(eligible, run.profile, load, "reviewer", exclude={implementer}, kind="review")
            reviewer = rev.provider
            task.implementer, task.reviewer = implementer, reviewer
            task.route_reason = f"{impl.reason}. {rev.reason}"
            if assignment:
                self._event(run, "reassigned", f"'{task.title}' reassigned to {name(implementer)}", implementer, task.id)

            previous: str | None = None
            findings: list[str] = []
            for revision in range(self.settings.max_revisions_per_task + 1):
                task.status = "working" if revision == 0 else "revising"
                self._event(run, "working", f"{name(implementer)} is {'revising' if revision else 'working on'} '{task.title}'", implementer, task.id)
                await self._publish(run)
                system, prompt = prompts.task_prompt(plan, task, deps, profile, run.materials, findings, previous, shared)
                try:
                    result = await self._call(run, implementer, ModelCall(
                        system, prompt, "task", self._out_cap(run),
                        {"request": run.request, "task_title": task.title, "task_id": task.id, "is_revision": revision > 0},
                        attachments=task_files), live=(f"task:{task.id}", task.title))
                except ProviderError as e:
                    task.attempts.append(Attempt(provider=implementer, error=str(e).removeprefix(f"{implementer}: ")))
                    break  # move on to reassignment
                attempt = Attempt(provider=implementer, model=result.model, output=result.text, is_revision=revision > 0)
                task.attempts.append(attempt)

                task.status = "reviewing"
                self._event(run, "reviewing", f"{name(reviewer)} is reviewing '{task.title}'", reviewer, task.id)
                await self._publish(run)
                system, prompt = prompts.review_prompt(plan, task, result.text, profile)
                review = await self._review_call(run, reviewer, implementer, load, system, prompt, task, revision)
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
                if review.verdict == "block":
                    break
                previous, findings = result.text, review.findings

        task.status = "needs_human_review"
        self._event(run, "needs_human_review", f"'{task.title}' did not pass review within the retry limit; its latest draft "
                    "goes to the final step and is marked as unreviewed", task_id=task.id)
        await self._publish(run, checkpoint=True)

    async def _judge(self, run: Run, plan: Plan, output: str, profile, judges: list[str], round_: int) -> list[Judgement]:
        """Each judge tests the final result on its own, at the same time. A judge that fails or runs out of time
        is left out of that round."""
        system, prompt = prompts.final_eval_prompt(run.request, plan, output, profile)
        final = run.final
        assert final is not None

        async def one(p: str) -> Judgement | None:
            try:
                res = await self._call(run, p, ModelCall(system, prompt, "final_eval", 3000,
                                                         {"request": run.request, "round": round_}))
            except (ProviderError, OutOfTime):
                return None
            data = prompts.parse_json(res.text) or {}
            score = data.get("score")

            def strs(key: str) -> list[str]:
                return [str(x) for x in (data.get(key) or []) if isinstance(x, (str, int, float))][:10]

            j = Judgement(judge=p, model=res.model, round=round_,
                          score=max(0.0, min(10.0, float(score))) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
                          meets_criteria=bool(data["meets_criteria"]) if "meets_criteria" in data else None,
                          must_fix=strs("must_fix"), gaps=strs("gaps") or ([] if data else ["Its test reply could not be read."]))
            for x in strs("limitations"):
                if x not in final.limitations:
                    final.limitations.append(x)
            self._event(run, "judged", f"{name(p)} scored the result {j.score if j.score is not None else 'n/a'}/10"
                        + (f" and found {len(j.must_fix)} thing{'s' if len(j.must_fix) != 1 else ''} to fix" if j.must_fix else ""), p)
            await self._publish(run)
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
        if not accepted and not drafts and not any(a.output for a in run.analyses):
            run.status = "failed"
            run.error = "No task produced any work, so there is nothing to put together."
            self._event(run, "failed", run.error)
            return

        run.status = "integrating"
        self._event(run, "stage", f"Writing the final result from {len(accepted)} accepted output{'s' if len(accepted) != 1 else ''}"
                    + (f" and {len(drafts)} unreviewed draft{'s' if len(drafts) != 1 else ''}" if drafts else ""))
        await self._publish(run)
        shared = prompts.findings_block(run.analyses, run.cross_checks, limit=3000) + prompts.consensus_block(run.consensus)
        system, prompt = prompts.integrate_prompt(run.request, plan, accepted, missing, profile, shared, drafts)
        started = time.monotonic()
        integrator, result, _ = await self._call_any(run, "integrator", ModelCall(
            system, prompt, "integrate", self._out_cap(run),
            {"request": run.request, "sections": [(t.title, t.output) for t in accepted] + drafts}), load,
            live=("final", "Final result"), frontier_first=True)
        took = time.monotonic() - started
        final = FinalResult(output=result.text, integrator=integrator,
                            best_approach=run.consensus.best if run.consensus else None)
        run.final = final  # shown while the other models test it
        self._event(run, "integrated", f"{name(integrator)} wrote the single combined result", integrator)
        await self._publish(run, checkpoint=True)

        # Every other model tests the one result; the integrator fixes what they find, then they score it again,
        # as long as the time allows.
        judges = [p for p in self._eligible(run) if p != integrator] or [integrator]
        sections = [(t.title, t.output) for t in accepted]
        latest: list[Judgement] = []
        tested = True
        for round_ in range(self.settings.max_final_fix_rounds + 1):
            if not self._has_time(run, 3 * self._min_call(run)):
                tested = round_ > 0
                final.limitations.append("The time limit was reached before the other models could "
                                         + ("re-test the fixed result." if round_ else "test the result."))
                break
            self._event(run, "stage", f"{joined([name(p) for p in judges])} {'are' if len(judges) > 1 else 'is'} "
                        f"{'re-testing the fixed' if round_ else 'testing the'} result against the criteria and edge cases")
            await self._publish(run)
            judged = await self._judge(run, plan, final.output, profile, judges, round_)
            if judged:
                latest = judged
                final.judges.extend(judged)
            must = list(dict.fromkeys(m for j in judged for m in j.must_fix))
            if not must or round_ == self.settings.max_final_fix_rounds:
                break
            if not self._has_time(run, took * 1.3 + 3 * self._min_call(run)):
                final.limitations.append("Not fixed because the time limit was reached: " + "; ".join(must[:5]))
                break
            self._event(run, "fixing", f"{name(integrator)} is fixing {len(must)} problem{'s' if len(must) != 1 else ''} "
                        "the other models found", integrator)
            await self._publish(run)
            system, prompt = prompts.fix_prompt(run.request, plan, final.output, must, profile)
            try:
                fixed = await self._call(run, integrator, ModelCall(
                    system, prompt, "integrate", self._out_cap(run),
                    {"request": run.request, "task_title": "Final fixes", "sections": sections}),
                    live=("final", "Fixing the final result"))
            except (ProviderError, OutOfTime) as e:
                why = "the time limit was reached" if isinstance(e, OutOfTime) else "the model failed"
                final.limitations.append(f"These problems found by the other models could not be fixed ({why}): "
                                         + "; ".join(must[:5]))
                break
            final.output = fixed.text
            final.fixes += 1
            self._partial.pop(run.id, None)
            await self._publish(run, checkpoint=True)

        scores = [j.score for j in latest if j.score is not None]
        final.score = round(sum(scores) / len(scores), 1) if scores else None
        verdicts = [j.meets_criteria for j in latest if j.meets_criteria is not None]
        final.meets_criteria = all(verdicts) if verdicts else None
        final.evaluator = latest[0].judge if latest else None
        still = list(dict.fromkeys(m for j in latest for m in j.must_fix))
        final.gaps = (["Still to fix: " + m for m in still] + list(dict.fromkeys(g for j in latest for g in j.gaps)))[:12]
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
        final.limitations.append("Assurance level: evidence-reviewed by independent models, not objectively tested. Check important results yourself.")
        final.limitations = list(dict.fromkeys(final.limitations))[:14]
        who = joined(list(dict.fromkeys(name(j.judge) for j in latest))) if latest else "no other model"
        self._deliver(run, final, f"Delivered one result, scored {final.score if final.score is not None else 'n/a'}/10 by {who}"
                      + (f" after {final.fixes} round{'s' if final.fixes != 1 else ''} of fixes" if final.fixes else ""))

    def _deliver(self, run: Run, final: FinalResult, message: str) -> None:
        run.final = final
        run.status = "completed"
        if run.time_limit_s:
            message += f", in {run.elapsed_s / 60:.0f} of {run.time_limit_s / 60:.0f} min"
        self._event(run, "completed", message, final.evaluator or final.integrator)

    # ------------------------------------------------------------------ wrap-up

    async def _wrap_up(self, run: Run) -> None:
        """The run's working time is used up. Stop new work and deliver the complete result from what is done."""
        run.wrapping_up = True
        self._stage_end.pop(run.id, None)
        run.live = {}
        left, _ = self._time_left(run)
        if run.final and run.final.output:
            final = run.final
            final.limitations.insert(0, "The run reached its time limit, so the result was delivered as it stood"
                                     + (" after the last round of fixes." if final.fixes else "."))
            final.limitations = list(dict.fromkeys(final.limitations))[:14]
            scores = [j.score for j in final.judges if j.score is not None]
            final.score = final.score if final.score is not None else (round(sum(scores) / len(scores), 1) if scores else None)
            self._deliver(run, final, "Time limit reached: delivered the final result as it stood")
            return

        plan = run.plan
        profile = get_profile(run.profile)
        self._settle_tasks(run)
        tasks = plan.tasks if plan else []
        accepted = [t for t in tasks if t.status == "accepted"]
        drafts = [(t.title, self._draft(t)) for t in tasks if t.status != "accepted" and self._draft(t)]
        partial = self._partial.pop(run.id, "")
        if partial:
            drafts.insert(0, ("Final result, cut off by the time limit", partial))
        try:
            writers = self._eligible(run)
        except NoProviders:
            writers = []
        writers = [p for p in writers if p in FRONTIER] + [p for p in writers if p not in FRONTIER]
        run.status = "integrating"
        self._event(run, "wrap_up", "Time is nearly up, so SASI stopped new work"
                    + (f" and {name(writers[0])} is writing the complete final result from the work done so far"
                       f" ({left / 60:.1f} min left)" if writers and left is not None else ""))
        await self._publish(run)
        system, prompt = prompts.wrap_up_prompt(
            run.request, plan, profile, max(1.0, (left or 60) / 60), "", accepted, drafts,
            run.analyses, prompts.consensus_block(run.consensus))
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
            self._deliver(run, final, f"Time limit reached: {name(p)} wrote the complete final result from the work done")
            return

        # No model could write it in time: deliver the best work there is.
        best = "\n\n".join(f"## {t.title}\n\n{t.output}" for t in accepted)
        if not best and drafts:
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
        self._deliver(run, final, "Time limit reached: delivered the team's work as it stood")
