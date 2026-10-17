"""Offline timing regressions. Tiny snapshot windows exercise real controller waits."""
import asyncio
import json
import time

import pytest

from app.config import Settings
from app.gateway.base import ModelCall, ModelResult
from app.gateway.registry import ModelRegistry
from types import SimpleNamespace

from app.models import Attempt, FinalResult, Opinion, PartialOutput, Plan, Review, Run, Task
from app.store.memory import MemoryStore
from app.workflow.controller import BudgetExceeded, CallWindowExpired, Controller, OutOfTime
from app.workflow.router import PerformanceHistory


def controller():
    settings = Settings(_env_file=None, allow_simulated=True, simulated_delay=0, enable_web_research=False)
    c = Controller(settings, ModelRegistry(settings), MemoryStore(), PerformanceHistory())
    c._min_call = lambda run: .001
    return c


def run_state(**overrides):
    values = dict(request="A small bounded task", timing_policy="graceful", time_limit_s=10,
                  total_time_limit_s=100, model_phase_min_s=.04, model_phase_grace_s=.04,
                  team=["claude", "gemini"], research_status="not_needed", require_plan_approval=False)
    values.update(overrides)
    return Run(**values)


def answer(provider="claude", text="Complete answer"):
    return ModelResult(provider=provider, model="offline-fake", text=text, input_tokens=2, output_tokens=3)


def call(kind="analysis", **hints):
    return ModelCall("system", "prompt", kind, 1000, hints)


def test_new_runs_snapshot_minimum_and_inclusive_total_cap():
    c = controller()
    run = Run(request="Do the work", time_limit_minutes=120)
    c._set_limit(run)
    assert run.timing_policy == "graceful"
    assert run.model_phase_min_s == 1200 and run.model_phase_grace_s == 300
    assert run.time_limit_s == run.total_time_limit_s == 7200
    c.settings.run_time_limit_minutes = .1
    c._set_limit(run)
    assert run.time_limit_s == 1200


def test_transport_allowance_does_not_cut_off_twenty_minute_phase():
    async def scenario():
        c = controller()
        run = Run(request="Analyze the task")
        c._set_limit(run)
        seen = []
        async def generate(request):
            seen.append(request.timeout_seconds)
            return answer()
        c.registry.adapters["claude"].generate = generate
        await c._call(run, "claude", call())
        assert 1529 < seen[0] <= 1530
        assert run.call_allowance_s["claude:analysis::"] == 1200
    asyncio.run(scenario())


def test_slow_peer_finishes_after_target_without_being_cancelled():
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=.02, compare_all=True)
        c._clock_start(run)
        async def fast(request):
            await asyncio.sleep(.002)
            return answer()
        async def slow(request):
            await asyncio.sleep(.05)
            return answer("gemini")
        c.registry.adapters["claude"].generate = fast
        c.registry.adapters["gemini"].generate = slow
        await c._analyze(run, [])
        assert {a.provider for a in run.analyses if a.output} == {"claude", "gemini"}
        assert not run.partial_outputs
        with pytest.raises(OutOfTime):
            await c._call(run, "claude", call("plan"))
        assert run.usage.calls == 2
    asyncio.run(scenario())


def test_soft_warning_keeps_same_request_running_through_grace():
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=.01, model_phase_min_s=.02, model_phase_grace_s=.08)
        c._clock_start(run)
        async def generate(request):
            await asyncio.sleep(.04)
            return answer()
        c.registry.adapters["claude"].generate = generate
        result = await c._call(run, "claude", call())
        assert result.text == "Complete answer" and run.usage.calls == 1
        assert any(e.type == "finishing" and "No new instruction" in e.message for e in run.events)
        assert not run.active_calls
    asyncio.run(scenario())


@pytest.mark.parametrize("visible", [True, False])
def test_hard_phase_expiry_preserves_incomplete_text_and_unknown_usage(visible):
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=.01, model_phase_min_s=.02, model_phase_grace_s=.02)
        c._clock_start(run)
        async def generate(request):
            if visible:
                await request.on_text('{"verdict":"pass","score":10,"findings":[]}')
            await asyncio.Event().wait()
        c.registry.adapters["claude"].generate = generate
        with pytest.raises(CallWindowExpired):
            await c._call(run, "claude", call("review", task_id="t1"))
        assert len(run.partial_outputs) == 1 and run.partial_outputs[0].usage_estimated
        assert bool(run.partial_outputs[0].text) is visible
        assert run.usage.total_tokens > 0 and run.usage.calls == 1
        assert c.store.outputs[run.id][0]["verdict"] == "incomplete"
        assert c.store.outputs[run.id][0]["score"] is None
        assert not run.call_completed_keys and not run.active_calls
        assert c._token_reservations[run.id] == 0
        with pytest.raises(CallWindowExpired):
            await c._call(run, "claude", call("review", task_id="t1"))
        assert run.usage.calls == 1
    asyncio.run(scenario())


def test_cancel_preserves_remaining_allowance_and_resume_notes():
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=1, model_phase_min_s=1, model_phase_grace_s=.2)
        started = asyncio.Event()
        async def generate(request):
            await request.on_text("Useful unfinished work")
            started.set()
            await asyncio.Event().wait()
        c.registry.adapters["claude"].generate = generate
        pending = asyncio.create_task(c._call(run, "claude", call()))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        saved_allowance = run.call_allowance_s.copy()
        saved_elapsed = run.call_elapsed_s["claude:analysis::"]
        run.used_s = 2  # research target has passed; an already admitted phase can continue
        async def finish(request):
            assert "Useful unfinished work" in request.prompt
            return answer()
        c.registry.adapters["claude"].generate = finish
        await c._call(run, "claude", call())
        assert run.call_allowance_s == saved_allowance
        assert run.call_elapsed_s["claude:analysis::"] >= saved_elapsed
        assert "claude:analysis::" in run.call_completed_keys
    asyncio.run(scenario())


def test_full_resume_continues_task_review_without_regenerating_saved_draft():
    async def scenario():
        c = controller()
        task = Task(id="t1", title="Exact result", status="reviewing", implementer="claude", reviewer="gemini",
                    attempts=[Attempt(provider="claude", model="saved", output="42")])
        run = run_state(status="interrupted", time_limit_s=1, used_s=2, model_phase_min_s=1,
                        plan=Plan(tasks=[task]), plan_review=Review(reviewer="gemini", verdict="accept"),
                        call_allowance_s={"gemini:review:t1:": 1}, call_elapsed_s={"gemini:review:t1:": .2})
        seen = []
        async def generate(request):
            seen.append(request.kind)
            if request.kind == "review":
                return answer("gemini", json.dumps({"verdict": "pass", "score": 9, "findings": []}))
            return answer("gemini", "Final answer")
        for adapter in c.registry.adapters.values():
            adapter.generate = generate
        await c.resume(run)
        await c._tasks[run.id]
        assert run.status == "completed", run.error
        assert task.status == "accepted" and task.output == "42"
        assert seen == ["review", "wrap_up"]
        assert len(task.attempts) == 1 and run.used_s >= 2
        assert run.call_allowance_s["gemini:review:t1:"] == 1
    asyncio.run(scenario())


def test_full_resume_continues_admitted_draft_after_target():
    async def scenario():
        c = controller()
        task = Task(id="t1", title="Draft", implementer="claude", reviewer="gemini")
        run = run_state(status="interrupted", time_limit_s=1, used_s=2, model_phase_min_s=1,
                        plan=Plan(tasks=[task]), plan_review=Review(reviewer="gemini", verdict="accept"),
                        call_allowance_s={"claude:task:t1:": 1}, call_elapsed_s={"claude:task:t1:": .2},
                        partial_outputs=[PartialOutput(provider="claude", kind="task", task_id="t1", text="Prior work", reason="cancel")])
        seen = []
        async def generate(request):
            seen.append(request.kind)
            if request.kind == "task":
                assert "Prior work" in request.prompt
            return answer()
        for adapter in c.registry.adapters.values():
            adapter.generate = generate
        await c.resume(run)
        await c._tasks[run.id]
        assert run.status == "completed", run.error
        assert seen == ["task", "wrap_up"], "new review cannot start after the target"
        assert task.attempts[0].output and task.status != "accepted"
    asyncio.run(scenario())


def test_cross_check_resume_keeps_finished_peer_and_continues_missing_peer():
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=1, used_s=2,
                        analyses=[Opinion(provider="claude", output="A"), Opinion(provider="gemini", output="B")],
                        cross_checks=[Opinion(provider="claude", output="already complete")],
                        call_allowance_s={"gemini:cross_check::": 1})
        async def generate(request):
            return answer("gemini", "New cross-check")
        c.registry.adapters["gemini"].generate = generate
        await c._cross_check(run)
        assert len(run.cross_checks) == 2 and run.usage.calls == 1
        assert run.cross_checks[0].output == "already complete"
    asyncio.run(scenario())


def test_resume_plan_review_does_not_redraft_or_bypass_gate():
    async def scenario():
        c = controller()
        plan = Plan(planner="claude", tasks=[Task(id="t1", title="One task")])
        run = run_state(status="interrupted", used_s=2, time_limit_s=1, plan=plan,
                        call_allowance_s={"gemini:plan_review::": 1})
        seen = []
        async def generate(request):
            seen.append(request.kind)
            return answer("gemini", json.dumps({"verdict": "accept", "score": 9, "findings": []}))
        c.registry.adapters["gemini"].generate = generate
        await c._plan(run, {}, [])
        assert seen == ["plan_review"] and run.plan is plan
        assert run.plan_review.verdict == "accept"
    asyncio.run(scenario())


def test_finalization_partial_is_delivered_and_window_is_never_reset():
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=.01, used_s=.02, model_phase_min_s=.015, model_phase_grace_s=.015,
                        analyses=[Opinion(provider="claude", output="Saved analysis")])
        c._clock_start(run)
        async def generate(request):
            await request.on_text("Substantial final synthesis, still incomplete")
            await asyncio.Event().wait()
        c.registry.adapters["claude"].generate = generate
        await c._wrap_up(run)
        deadline = run.finalization_deadline_elapsed_s
        assert run.status == "completed" and "Substantial final synthesis" in run.final.output
        assert "Incomplete final synthesis" in run.final.output and run.final.score is None
        assert run.usage.calls == 1
        run.final = None
        await c._wrap_up(run)
        assert run.usage.calls == 1 and run.finalization_deadline_elapsed_s == deadline
    asyncio.run(scenario())


def test_new_call_requires_whole_minimum_grace_and_finalization_headroom():
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=7200, total_time_limit_s=7200, model_phase_min_s=1200, model_phase_grace_s=300,
                        used_s=4201)
        with pytest.raises(OutOfTime):
            await c._call(run, "claude", call())
        assert run.usage.calls == 0 and not run.call_allowance_s
        # An adaptive 40-minute window is shortened above its 20-minute minimum
        # to preserve both 5-minute grace and a complete finalization window.
        run.used_s = 4000
        run.call_duration_s["analysis"] = 1800
        async def generate(request):
            return answer()
        c.registry.adapters["claude"].generate = generate
        await c._call(run, "claude", call())
        assert run.call_allowance_s["claude:analysis::"] == 1400
    asyncio.run(scenario())


def test_hard_total_includes_finalization_and_stops_existing_phase():
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=.01, total_time_limit_s=.04, model_phase_min_s=1, model_phase_grace_s=1,
                        call_allowance_s={"claude:analysis::": 1},
                        analyses=[Opinion(provider="claude", output="Saved result")])
        c._clock_start(run)
        async def generate(request):
            await request.on_text("Partial before hard cap")
            await asyncio.Event().wait()
        c.registry.adapters["claude"].generate = generate
        with pytest.raises((OutOfTime, CallWindowExpired)):
            await c._call(run, "claude", call())
        await c._wrap_up(run)
        assert run.finalization_deadline_elapsed_s == run.total_time_limit_s
        assert run.usage.calls == 1, "no model call may start after the inclusive hard cap"
        assert run.status == "completed" and run.final.output
        assert run.partial_outputs[0].text == "Partial before hard cap"
    asyncio.run(scenario())


@pytest.mark.parametrize("during_checkpoint", [False, True])
def test_completed_result_wins_boundary_race_before_timeout(during_checkpoint):
    async def scenario():
        c = controller()
        run = run_state()
        future = asyncio.get_running_loop().create_future()
        if during_checkpoint:
            def checkpoint(state):
                future.set_result(answer())
                return None
            c._scheduled_checkpoint = checkpoint
        else:
            future.set_result(answer())
        result = await c._wait_for_model(run, "claude", future, ("missing", time.monotonic()-1, time.monotonic()-1))
        assert result.text == "Complete answer"
    asyncio.run(scenario())


def test_trial_expiry_during_reasoning_charges_unknown_usage_and_stops():
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=.02, model_phase_min_s=.02, model_phase_grace_s=.02)
        async def generate(request):
            c.registry.trial_credit_window_error = lambda: "trial window ended"
            await asyncio.Event().wait()
        c.registry.adapters["claude"].generate = generate
        with pytest.raises(BudgetExceeded, match="trial window ended"):
            await c._call(run, "claude", call())
        assert run.usage.calls == 1 and run.usage.total_tokens > 0
        assert not run.partial_outputs[0].text and run.partial_outputs[0].usage_estimated
    asyncio.run(scenario())


def test_call_any_prefers_existing_admission_when_new_phase_cannot_fit():
    async def scenario():
        c = controller()
        run = run_state(time_limit_s=7200, total_time_limit_s=7200, used_s=4300,
                        model_phase_min_s=1200, model_phase_grace_s=300,
                        call_allowance_s={"claude:plan::": 1200}, call_elapsed_s={"claude:plan::": 100})
        seen = []
        def pick(eligible, *args, **kwargs):
            seen.append(eligible)
            return SimpleNamespace(provider=eligible[-1], reason="test routing")
        c.router.pick = pick
        async def generate(request):
            return answer()
        c.registry.adapters["claude"].generate = generate
        provider, _, _ = await c._call_any(run, "planner", call("plan"), {})
        assert seen == [["claude"]] and provider == "claude"
        assert run.call_allowance_s == {"claude:plan::": 1200}
        assert run.call_elapsed_s["claude:plan::"] >= 100
    asyncio.run(scenario())


def test_full_resume_reuses_final_artifact_and_finishes_admitted_judge():
    async def scenario():
        c = controller()
        task = Task(id="t1", title="Result", status="accepted", output="42")
        final = FinalResult(output="The result is 42.", integrator="claude")
        c._stamp_final(final)
        original_hash = final.artifact_hash
        run = run_state(status="interrupted", time_limit_s=1, used_s=2, plan=Plan(tasks=[task]), final=final,
                        plan_review=Review(reviewer="gemini", verdict="accept"),
                        call_allowance_s={"gemini:final_eval::0": 1}, call_elapsed_s={"gemini:final_eval::0": .2})
        seen = []
        async def generate(request):
            seen.append(request.kind)
            return answer("gemini", json.dumps({"meets_criteria": True, "score": 9, "must_fix": [],
                                                 "gaps": [], "limitations": []}))
        c.registry.adapters["gemini"].generate = generate
        await c.resume(run)
        await c._tasks[run.id]
        assert run.status == "completed", run.error
        assert seen == ["final_eval"]
        assert run.final.artifact_hash == original_hash and run.final.output == "The result is 42."
        assert run.final.score == 9 and len(run.final.judges) == 1
    asyncio.run(scenario())


def test_resume_at_inclusive_total_cap_skips_evidence_and_all_model_calls():
    async def scenario():
        c = controller()
        run = run_state(status="interrupted", time_limit_s=7200, used_s=7200, total_time_limit_s=7200,
                        research_status="not_started", analyses=[Opinion(provider="claude", output="Saved work")])
        async def forbidden(*args, **kwargs):
            pytest.fail("No new evidence or model work may begin at the hard total cap")
        c._gather_evidence = forbidden
        for adapter in c.registry.adapters.values():
            adapter.generate = forbidden
        await c.resume(run)
        await c._tasks[run.id]
        assert run.status == "completed" and "Saved work" in run.final.output
        assert run.usage.calls == 0
        assert run.finalization_deadline_elapsed_s == 7200
    asyncio.run(scenario())


def test_reasoning_only_call_checkpoints_elapsed_clock_with_shared_throttle():
    async def scenario():
        c = controller()
        c._heartbeat_seconds = .01
        run = run_state(model_phase_min_s=1, model_phase_grace_s=1)
        c._clock_start(run)
        checkpoints = []
        original_save = c.store.save
        async def save(state, checkpoint=False):
            if checkpoint:
                checkpoints.append(state.model_copy(deep=True))
            await original_save(state, checkpoint)
        c.store.save = save
        async def generate(request):
            await asyncio.sleep(.045)
            return answer()
        c.registry.adapters["claude"].generate = generate
        await c._call(run, "claude", call())
        assert len(checkpoints) >= 3
        assert checkpoints[-1].updated_at - checkpoints[0].updated_at >= .02
        assert checkpoints[-1].active_calls["claude:analysis::"].started_at <= checkpoints[0].updated_at
        before = len(checkpoints)
        await asyncio.gather(c._checkpoint_active(run), c._checkpoint_active(run))
        assert len(checkpoints) <= before + 1, "parallel calls share the run's checkpoint throttle"
        from app.store.recovery import restore_interrupted
        restored = restore_interrupted(checkpoints[-1])
        assert restored.call_elapsed_s["claude:analysis::"] >= .02
        assert restored.used_s >= .02 and not restored.active_calls
    asyncio.run(scenario())


def test_slow_durable_heartbeat_never_delays_provider_cancellation():
    async def scenario():
        c = controller()
        c._heartbeat_seconds = .01
        run = run_state(time_limit_s=.01, model_phase_min_s=.02, model_phase_grace_s=.04)
        c._clock_start(run)
        saving, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        saves = 0
        original_save = c.store.save
        async def save(state, checkpoint=False):
            nonlocal saves
            if checkpoint:
                saves += 1
                if saves == 2:
                    saving.set()
                    await release.wait()
            await original_save(state, checkpoint)
        c.store.save = save
        async def generate(request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        c.registry.adapters["claude"].generate = generate
        pending = asyncio.create_task(c._call(run, "claude", call()))
        await asyncio.wait_for(saving.wait(), .3)
        await asyncio.wait_for(cancelled.wait(), .3)
        assert not release.is_set(), "model is stopped while the durable save is still blocked"
        assert not pending.done(), "finishing persistence may outlast provider work"
        release.set()
        with pytest.raises(CallWindowExpired):
            await pending
        assert not c._heartbeat_tasks
        assert run.partial_outputs[0].usage_estimated
    asyncio.run(scenario())
