"""Workflow controller.

Owns run state and decides progression. Application rules, not model opinion,
control what happens next:

  understand -> plan -> independent plan review -> (user approval) ->
  parallel tasks behind dependency gates -> independent cross-review per task
  (bounded revise / reassign) -> integrate accepted work -> independent final
  evaluation -> deliver with evidence, limitations and usage.

Accepted work is checkpointed, so an interrupted run resumes without redoing it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .. import files as files_mod
from ..config import Settings
from ..files import FileStore, StoredFile
from ..gateway.base import ModelCall, ModelResult, ProviderError
from ..gateway.registry import ModelRegistry
from ..models import TERMINAL, Attempt, Event, FinalResult, LiveText, Opinion, Plan, Review, Run, Task, now
from ..store.base import RunStore
from . import eris, prompts
from .profiles import AUTO_FIELDS, get_profile
from .router import PerformanceHistory, Router

log = logging.getLogger("sasi.controller")

NAMES = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini"}


class BudgetExceeded(Exception):
    pass


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
        await self.store.save(run, checkpoint=True)
        self._launch(run)

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
        bad = self._unhealthy.get(run.id, set())
        eligible = [p for p in self.registry.eligible() if p not in bad]
        if not eligible:
            raise NoProviders("no model provider is available. Check your API keys on the Connections page.")
        return eligible

    def _check_budget(self, run: Run) -> None:
        s = self.settings
        if run.usage.calls >= s.max_model_calls_per_run:
            raise BudgetExceeded(f"reached the limit of {s.max_model_calls_per_run} model calls for one run")
        limit = s.eris_max_tokens_per_run if run.eris else s.max_tokens_per_run
        if run.usage.total_tokens >= limit:
            raise BudgetExceeded(f"reached the limit of {limit:,} tokens for one run")

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
        finally:
            if live:
                run.live.pop(live[0], None)

    async def _call_inner(self, run: Run, provider: str, call: ModelCall, live: tuple[str, str] | None) -> ModelResult:
        adapter = self.registry.adapters[provider]
        call.max_output_tokens = min(call.max_output_tokens, self._out_cap(run))
        if run.eris:
            call.rules = eris.RULES  # every model, on every step, follows the Eris guidebook
        timeout = 600 if (call.attachments or run.eris) else 300
        last: Exception | None = None
        for attempt in range(2):
            self._check_budget(run)
            run.usage.calls += 1
            if live:
                self._live(run, call, provider, *live)
            try:
                result = await asyncio.wait_for(adapter.generate(call), timeout=timeout)
            except ProviderError as e:
                last = e
                if e.retryable and attempt == 0:
                    self._event(run, "retry", f"{NAMES[provider]} had a temporary error, retrying once", provider)
                    await asyncio.sleep(2)
                    continue
                break
            except asyncio.TimeoutError as e:
                last = ProviderError(provider, f"timed out after {timeout}s", retryable=True)
                if attempt == 0:
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
            return result
        self._unhealthy.setdefault(run.id, set()).add(provider)
        self._event(run, "provider_error", f"{NAMES[provider]} failed and is skipped for the rest of this run: {last}", provider)
        raise last or ProviderError(provider, "unknown error")

    async def _call_any(self, run: Run, role: str, call: ModelCall, load: dict[str, int],
                        exclude: set[str] | None = None, live: tuple[str, str] | None = None) -> tuple[str, ModelResult, str]:
        """Pick a provider for a role; on provider failure fall back to the next one and record it."""
        tried: set[str] = set()
        while True:
            eligible = [p for p in self._eligible(run) if p not in tried]
            if not eligible:
                raise NoProviders(f"every provider failed for role {role}")
            kind = "review" if "review" in role or "evaluator" in role else "work"
            choice = self.router.pick(eligible, run.profile, load, role, exclude, kind=kind)
            try:
                return choice.provider, await self._call(run, choice.provider, call, live), choice.reason
            except ProviderError:
                tried.add(choice.provider)
                self._event(run, "fallback", f"Reassigning {role} after {NAMES[choice.provider]} failed")

    # ------------------------------------------------------------------ main flow

    async def _execute(self, run: Run) -> None:
        load: dict[str, int] = {}
        try:
            files = await self.files.get(run.id) if run.files else []
            if run.compare_all and not run.analyses:
                await self._analyze(run, files)
            if run.compare_all and not run.cross_checks:
                await self._cross_check(run)
            if not run.plan or not run.plan.tasks:
                await self._plan(run, load, files)
            if run.require_plan_approval and not run.plan_approved:
                run.status = "awaiting_approval"
                self._event(run, "awaiting_approval", "Plan is ready. Review it and approve to start the work.")
                await self._publish(run, checkpoint=True)
                await self._approvals.setdefault(run.id, asyncio.Event()).wait()
            run.status = "running"
            await self._publish(run)
            await self._run_tasks(run, load, files)
            await self._integrate(run, load)
        except asyncio.CancelledError:
            run.status = "cancelled"
            self._event(run, "cancelled", "Run cancelled. Accepted work is kept.")
            await self._publish(run, checkpoint=True)
        except BudgetExceeded as e:
            run.status = "budget_stopped"
            run.error = str(e)
            self._event(run, "budget_stopped", f"Stopped to protect the budget: {e}. Accepted work is kept.")
            await self._publish(run, checkpoint=True)
        except NoProviders as e:
            run.status = "failed"
            run.error = str(e)
            self._event(run, "failed", str(e))
            await self._publish(run, checkpoint=True)
        except Exception as e:  # noqa: BLE001 - never leave a run hanging
            log.exception("run %s failed", run.id)
            run.status = "failed"
            run.error = f"{type(e).__name__}: {e}"
            self._event(run, "failed", f"Unexpected error: {run.error}")
            await self._publish(run, checkpoint=True)
        finally:
            self._unhealthy.pop(run.id, None)
            if run.live:
                run.live = {}
                await self._publish(run)

    # ------------------------------------------------------------------ independent analyses and cross-checks

    async def _gather(self, coros) -> None:
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r

    async def _analyze(self, run: Run, files: list[StoredFile]) -> None:
        """Every connected model studies the request and files on its own, at the same time."""
        providers = self._eligible(run)
        profile = get_profile(run.profile)
        run.status = "analyzing"
        what = "the request and files" if files else "the request"
        self._event(run, "stage", f"{', '.join(NAMES[p] for p in providers)} are analysing {what} independently, at the same time")
        await self._publish(run)
        system, prompt = prompts.analysis_prompt(run.request, run.materials, files_mod.describe(files), profile)

        async def one(p: str) -> None:
            call = ModelCall(system, prompt, "analysis", self._out_cap(run),
                             {"request": run.request, "task_title": "Independent analysis"}, attachments=files)
            try:
                res = await self._call(run, p, call, live=(f"analysis:{p}", "Independent analysis"))
                run.analyses.append(Opinion(provider=p, model=res.model, output=res.text))
                self._event(run, "analysis", f"{NAMES[p]} finished its independent analysis", p)
            except ProviderError as e:
                run.analyses.append(Opinion(provider=p, error=str(e)))
            await self._publish(run)

        await self._gather(one(p) for p in providers)
        if not any(a.output for a in run.analyses):
            raise NoProviders("no model could analyse the request")
        await self._publish(run, checkpoint=True)

    async def _cross_check(self, run: Run) -> None:
        """Each model reads the others' analyses and compares them with its own."""
        ok = [a for a in run.analyses if a.output and a.provider in self._eligible(run)]
        if len(ok) < 2:
            return
        profile = get_profile(run.profile)
        run.status = "cross_checking"
        self._event(run, "stage", "Each model is now reading the others' analyses and comparing them with its own")
        await self._publish(run)

        async def one(a: Opinion) -> None:
            others = [o for o in run.analyses if o.output and o.provider != a.provider]
            system, prompt = prompts.cross_check_prompt(run.request, a, others, profile)
            call = ModelCall(system, prompt, "cross_check", min(5000 if run.eris else 3000, self._out_cap(run)),
                             {"request": run.request, "task_title": "Cross-check"})
            try:
                res = await self._call(run, a.provider, call, live=(f"cross:{a.provider}", "Cross-check"))
                run.cross_checks.append(Opinion(provider=a.provider, model=res.model, output=res.text))
                self._event(run, "cross_check", f"{NAMES[a.provider]} compared the analyses", a.provider)
            except ProviderError as e:
                run.cross_checks.append(Opinion(provider=a.provider, error=str(e)))
            await self._publish(run)

        await self._gather(one(a) for a in ok)
        await self._publish(run, checkpoint=True)

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

        shared = prompts.findings_block(run.analyses, run.cross_checks)
        system, prompt = prompts.plan_prompt(run.request, run.materials, profile, self.settings.max_tasks_per_plan,
                                             shared + ("\n\n" + files_mod.describe(files or []) if files else ""))
        planner, result, reason = await self._call_any(
            run, "planner", ModelCall(system, prompt, "plan", 3000, {"request": run.request},
                                      attachments=[] if run.analyses else (files or [])), load)
        plan = self._parse_plan(result.text, run)
        plan.planner = planner
        run.plan = plan
        field = str((prompts.parse_json(result.text) or {}).get("field") or "").lower()
        if not run.eris and field in AUTO_FIELDS and field != run.profile:
            run.profile = field
            profile = get_profile(field)
            self._event(run, "field", f"{NAMES[planner]} judged this to be {profile.name.lower()} work, so the rest of the run uses that approach", planner)
        self._event(run, "plan", f"{NAMES[planner]} drafted a plan with {len(plan.tasks)} tasks ({reason})", planner)

        # Independent plan review by a different provider, at most one revision.
        run.status = "reviewing_plan"
        await self._publish(run)
        for round_ in range(2):
            system, prompt = prompts.plan_review_prompt(run.request, plan, profile)
            reviewer, rres, _ = await self._call_any(
                run, "plan reviewer", ModelCall(system, prompt, "plan_review", 1500, {"request": run.request}),
                load, exclude={planner})
            review = self._parse_review(rres.text, reviewer, rres.model, default="accept")
            run.plan_review = review
            self._event(run, "plan_review", f"{NAMES[reviewer]} reviewed the plan: {review.verdict}", reviewer)
            if review.verdict != "revise" or round_ == 1:
                break
            system, prompt = prompts.plan_revise_prompt(run.request, plan, review.findings, profile, self.settings.max_tasks_per_plan)
            try:
                result = await self._call(run, planner, ModelCall(system, prompt, "plan", 3000, {"request": run.request}))
            except ProviderError:
                break  # keep the reviewed plan; the user still approves it
            revised = self._parse_plan(result.text, run)
            revised.planner = planner
            revised.version = plan.version + 1
            plan = run.plan = revised
            self._event(run, "plan", f"{NAMES[planner]} revised the plan (v{plan.version})", planner)
        await self._publish(run, checkpoint=True)

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
            if len(ready) > 1:
                self._event(run, "parallel", f"Running {len(ready)} independent tasks in parallel")
            results = await asyncio.gather(*(self._run_task(run, t, load, files or []) for t in ready), return_exceptions=True)
            for r in results:
                if isinstance(r, (BudgetExceeded, NoProviders, asyncio.CancelledError)):
                    raise r
                if isinstance(r, Exception):
                    raise r

    async def _run_task(self, run: Run, task: Task, load: dict[str, int], files: list[StoredFile]) -> None:
        plan = run.plan
        assert plan is not None
        profile = get_profile(run.profile)
        shared = prompts.findings_block(run.analyses, run.cross_checks, limit=4000)
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
                self._event(run, "reassigned", f"'{task.title}' reassigned to {NAMES[implementer]}", implementer, task.id)

            previous: str | None = None
            findings: list[str] = []
            for revision in range(self.settings.max_revisions_per_task + 1):
                task.status = "working" if revision == 0 else "revising"
                self._event(run, "working", f"{NAMES[implementer]} is {'revising' if revision else 'working on'} '{task.title}'", implementer, task.id)
                await self._publish(run)
                system, prompt = prompts.task_prompt(plan, task, deps, profile, run.materials, findings, previous, shared)
                try:
                    result = await self._call(run, implementer, ModelCall(
                        system, prompt, "task", self._out_cap(run),
                        {"request": run.request, "task_title": task.title, "task_id": task.id, "is_revision": revision > 0},
                        attachments=task_files), live=(f"task:{task.id}", task.title))
                except ProviderError as e:
                    task.attempts.append(Attempt(provider=implementer, error=str(e)))
                    break  # move on to reassignment
                attempt = Attempt(provider=implementer, model=result.model, output=result.text, is_revision=revision > 0)
                task.attempts.append(attempt)

                task.status = "reviewing"
                self._event(run, "reviewing", f"{NAMES[reviewer]} is reviewing '{task.title}'", reviewer, task.id)
                await self._publish(run)
                system, prompt = prompts.review_prompt(plan, task, result.text, profile)
                try:
                    rres = await self._call(run, reviewer, ModelCall(
                        system, prompt, "review", 1500, {"request": run.request, "task_id": task.id, "task_title": task.title, "is_revision": revision > 0}))
                    review = self._parse_review(rres.text, reviewer, rres.model, default="pass")
                except ProviderError:
                    # Reviewer down: pick another independent reviewer.
                    alt = [p for p in self._eligible(run) if p != implementer]
                    if not alt:
                        review = Review(reviewer="none", verdict="revise", findings=["No independent reviewer available"])
                    else:
                        reviewer = self.router.pick(alt, run.profile, load, "reviewer", kind="review").provider
                        task.reviewer = reviewer
                        rres = await self._call(run, reviewer, ModelCall(
                            system, prompt, "review", 1500, {"request": run.request, "task_id": task.id, "task_title": task.title, "is_revision": revision > 0}))
                        review = self._parse_review(rres.text, reviewer, rres.model, default="pass")
                attempt.review = review
                self._event(run, "review", f"{NAMES.get(review.reviewer, review.reviewer)}: {review.verdict} on '{task.title}'", review.reviewer, task.id)

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
        self._event(run, "needs_human_review", f"'{task.title}' did not pass review within the retry limit; kept as a draft for a person to check", task_id=task.id)
        await self._publish(run, checkpoint=True)

    async def _integrate(self, run: Run, load: dict[str, int]) -> None:
        plan = run.plan
        assert plan is not None
        profile = get_profile(run.profile)
        accepted = [t for t in plan.tasks if t.status == "accepted"]
        missing = [t for t in plan.tasks if t.status != "accepted"]
        if not accepted:
            run.status = "failed"
            run.error = "No task passed review, so there is nothing reviewed to integrate."
            self._event(run, "failed", run.error)
            await self._publish(run, checkpoint=True)
            return

        run.status = "integrating"
        self._event(run, "stage", f"Integrating {len(accepted)} accepted outputs")
        await self._publish(run)
        shared = prompts.findings_block(run.analyses, run.cross_checks, limit=3000)
        system, prompt = prompts.integrate_prompt(run.request, plan, accepted, missing, profile, shared)
        integrator, result, reason = await self._call_any(run, "integrator", ModelCall(
            system, prompt, "integrate", self._out_cap(run),
            {"request": run.request, "sections": [(t.title, t.output) for t in accepted]}), load,
            live=("final", "Final result"))
        final = FinalResult(output=result.text, integrator=integrator)
        self._event(run, "integrated", f"{NAMES[integrator]} integrated the result", integrator)
        await self._publish(run)

        system, prompt = prompts.final_eval_prompt(run.request, plan, result.text, profile)
        evaluator, eres, _ = await self._call_any(run, "final evaluator", ModelCall(
            system, prompt, "final_eval", 1500, {"request": run.request}), load, exclude={integrator})
        data = prompts.parse_json(eres.text) or {}
        final.evaluator = evaluator
        final.meets_criteria = bool(data.get("meets_criteria")) if "meets_criteria" in data else None
        score = data.get("score")
        final.score = float(score) if isinstance(score, (int, float)) else None
        final.gaps = [str(g) for g in (data.get("gaps") or [])][:12]
        final.limitations = [str(x) for x in (data.get("limitations") or [])][:12]
        if missing:
            final.limitations.insert(0, "Unfinished (did not pass review): " + ", ".join(t.title for t in missing))
        if any(t.assurance == "self-reviewed" for t in accepted):
            final.limitations.append("Some work was reviewed by the same provider that wrote it, because no other provider was available.")
        final.limitations.append("Assurance level: evidence-reviewed by independent models, not objectively tested. Check important results yourself.")
        run.final = final
        run.status = "completed"
        self._event(run, "completed", f"Delivered. {NAMES[evaluator]} evaluated it: {'meets' if final.meets_criteria else 'does not fully meet'} the criteria", evaluator)
        await self._publish(run, checkpoint=True)
