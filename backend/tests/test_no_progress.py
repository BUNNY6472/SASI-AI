"""Do not pay to re-review unchanged rejected work; keep its failing evidence."""
import asyncio
import json

import pytest

from app.config import Settings
from app.files import StoredFile
from app.gateway.base import ModelResult
from app.gateway.registry import ModelRegistry
from app.models import Attempt, FileRef, Plan, Review, Run, SourceEvidence, Task
from app.store.memory import MemoryStore
from app.workflow.controller import Controller
from app.workflow.router import PerformanceHistory


def controller():
    settings = Settings(_env_file=None, allow_simulated=True, simulated_delay=0, enable_web_research=False,
                        max_revisions_per_task=3, max_final_fix_rounds=3, max_gap_tasks_per_run=0)
    c = Controller(settings, ModelRegistry(settings), MemoryStore(), PerformanceHistory())
    c._eligible = lambda _run: ["claude", "gemini"]
    return c


def task_run():
    task = Task(id="t1", title="Compute the value", description="Return the exact supported value")
    run = Run(request="Use the supplied source", materials="The required value is 42.",
              plan=Plan(goal="Return the value", success_criteria=["Return 42"], deliverables=["One answer"], tasks=[task]),
              evidence=[SourceEvidence(id="s1", url="https://example.test/source", excerpt="The value is 42.")])
    return run, task


def review(verdict="revise"):
    return json.dumps({"verdict": verdict, "score": 3 if verdict != "pass" else 9,
                       "findings": ["The required value is 42, not 41."] if verdict != "pass" else []})


def judgement(passes=False):
    return json.dumps({"meets_criteria": passes, "score": 9 if passes else 3,
                       "must_fix": [] if passes else ["Return the required value 42."], "gaps": [], "limitations": []})


def test_identical_failed_task_skips_duplicate_review_and_only_tries_one_different_model():
    async def scenario():
        c = controller()
        run, task = task_run()
        calls = []
        async def reply(run_arg, provider, call, live=None):
            calls.append((provider, call.kind))
            return ModelResult(provider, "fake", "The value is 41." if call.kind == "task" else review())
        c._call = reply
        await c._run_task(run, task, {}, [])
        assert [kind for _, kind in calls].count("task") == 3
        assert [kind for _, kind in calls].count("review") == 1
        assert len({provider for provider, kind in calls if kind == "task"}) == 2
        assert task.status == "needs_human_review" and not task.output and not task.assurance
        assert all(attempt.review.findings == ["The required value is 42, not 41."] for attempt in task.attempts)
        assert sum(event.type == "no_progress" for event in run.events) == 2
    asyncio.run(scenario())


def test_different_implementer_can_fix_stalled_task_and_receives_fresh_review():
    async def scenario():
        c = controller()
        run, task = task_run()
        work, reviews = [], []
        async def reply(run_arg, provider, call, live=None):
            if call.kind == "task":
                work.append(call)
                text = "The value is 42." if len(work) == 3 else "The value is 41."
            else:
                reviews.append(call)
                text = review("pass" if len(work) == 3 else "revise")
            return ModelResult(provider, "fake", text)
        c._call = reply
        await c._run_task(run, task, {}, [])
        assert len(work) == 3 and len(reviews) == 2
        assert task.status == "accepted" and task.output == "The value is 42."
        assert "required value is 42" in work[-1].prompt
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["request", "materials", "evidence", "criteria", "attachment_bytes", "attachment_generation"])
def test_same_task_artifact_is_reviewed_again_when_context_changes(changed):
    async def scenario():
        c = controller()
        run, task = task_run()
        f = StoredFile("f1", "value.txt", "text/plain", "text", 2, b"42", text="42")
        work, reviews = [], []
        async def reply(run_arg, provider, call, live=None):
            if call.kind == "task":
                work.append(call)
                if len(work) == 2:
                    if changed == "request": run.request += " Recheck the alternative."
                    elif changed == "materials": run.materials += " New source correction."
                    elif changed == "evidence": run.evidence[0].excerpt = "Corrected source: 41."
                    elif changed == "criteria": run.plan.success_criteria = ["Return 41"]
                    elif changed == "attachment_bytes": f.data = b"41"
                    elif changed == "attachment_generation": f.object_generation = "2"
                text = "The value is 41."
            else:
                reviews.append(call)
                text = review("pass" if len(reviews) == 2 else "revise")
            return ModelResult(provider, "fake", text)
        c._call = reply
        await c._run_task(run, task, {}, [f])
        assert len(reviews) == 2 and task.status == "accepted"
        assert not any(event.type == "no_progress" for event in run.events)
    asyncio.run(scenario())


def test_invalid_review_is_not_reused_as_evidence_of_stalled_work():
    async def scenario():
        c = controller()
        run, task = task_run()
        reviews = []
        async def reply(run_arg, provider, call, live=None):
            if call.kind == "review":
                reviews.append(call)
                text = "invalid JSON" if len(reviews) == 1 else review("pass")
            else:
                text = "The value is 42."
            return ModelResult(provider, "fake", text)
        c._call = reply
        await c._run_task(run, task, {}, [])
        assert len(reviews) == 2 and task.status == "accepted"
    asyncio.run(scenario())


def test_old_task_checkpoint_without_context_hash_is_reviewed_afresh():
    async def scenario():
        c = controller()
        run, task = task_run()
        task.attempts = [Attempt(provider="claude", output="The value is 42.",
                                 review=Review(reviewer="gemini", verdict="revise", score=3, findings=["Old finding"]))]
        calls = []
        async def reply(run_arg, provider, call, live=None):
            calls.append(call.kind)
            return ModelResult(provider, "fake", "The value is 42." if call.kind == "task" else review("pass"))
        c._call = reply
        await c._run_task(run, task, {}, [])
        assert calls == ["task", "review"] and task.status == "accepted"
    asyncio.run(scenario())


async def stalled_final(c):
    run, task = task_run()
    task.status, task.output = "accepted", "The value is 41."
    calls = []
    async def reply(run_arg, provider, call, live=None):
        calls.append(call.kind)
        return ModelResult(provider, "fake", judgement() if call.kind == "final_eval" else "The value is 41.")
    c._call = reply
    await c._integrate(run, {})
    return run, calls


def test_identical_final_fix_stops_without_rejudging_or_claiming_success():
    async def scenario():
        run, calls = await stalled_final(controller())
        assert calls == ["integrate", "final_eval", "integrate"]
        assert run.final.fixes == 0 and run.final.artifact_version == 1
        assert run.final.stalled_context_hash and run.final.meets_criteria is False
        assert run.final.verification_status == "failed"
        assert any("unchanged result" in note for note in run.final.limitations)
        assert any(check.status == "fail" for check in run.final.checks)
    asyncio.run(scenario())


def test_stalled_final_survives_serialization_and_runtime_changes_without_new_calls():
    async def scenario():
        run, _ = await stalled_final(controller())
        restored = Run.model_validate_json(run.model_dump_json())
        restored.updated_at += 100
        restored.plan.version += 1  # bookkeeping without any changed criteria/content
        c = controller()
        async def unexpected(*args, **kwargs):
            pytest.fail("A resumed unchanged stalled artifact must not buy another review or fix")
        c._call = unexpected
        await c._integrate(restored, {})
        assert restored.final.meets_criteria is False and restored.final.verification_status == "failed"
        assert len(restored.final.judges) == 1 and restored.final.stalled_context_hash
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["request", "evidence", "retrieved_at", "criteria", "output", "file_ref"])
def test_changed_stalled_context_gets_a_new_independent_review_round(changed):
    async def scenario():
        run, _ = await stalled_final(controller())
        restored = Run.model_validate_json(run.model_dump_json())
        if changed == "request": restored.request += " Confirm against the revised requirement."
        elif changed == "evidence": restored.evidence[0].excerpt = "Revised source now reports 41."
        elif changed == "retrieved_at": restored.evidence[0].retrieved_at += 100
        elif changed == "criteria": restored.plan.success_criteria = ["Use corrected value 41"]
        elif changed == "output": restored.final.output = "The value is 42."
        elif changed == "file_ref": restored.files = [FileRef(id="f2", name="new.txt", mime="text/plain", kind="text", size=1)]
        c = controller()
        calls = []
        async def reply(run_arg, provider, call, live=None):
            calls.append(call.kind)
            assert call.kind == "final_eval"
            return ModelResult(provider, "fake", judgement(True))
        c._call = reply
        await c._integrate(restored, {})
        assert calls == ["final_eval"] and not restored.final.stalled_context_hash
        assert restored.final.judges[-1].round > restored.final.judges[0].round
        assert restored.final.meets_criteria is True
        assert not any("unchanged result" in note for note in restored.final.limitations)
    asyncio.run(scenario())


def test_new_evidence_during_a_fix_does_not_trigger_unchanged_context_stop():
    async def scenario():
        c = controller()
        run, task = task_run()
        task.status, task.output = "accepted", "The value is 41."
        judged = []
        async def reply(run_arg, provider, call, live=None):
            if call.kind == "final_eval":
                judged.append(call)
                text = judgement(len(judged) == 2)
            else:
                if call.hints.get("phase", "").startswith("fix:"):
                    run.evidence[0].excerpt = "Updated evidence reports 41."
                text = "The value is 41."
            return ModelResult(provider, "fake", text)
        c._call = reply
        await c._integrate(run, {})
        assert len(judged) == 2 and not run.final.stalled_context_hash
        assert run.final.meets_criteria is True
    asyncio.run(scenario())


def test_minority_blocking_finding_is_preserved_despite_other_judges_agreeing():
    async def scenario():
        c = controller()
        c._eligible = lambda _run: ["claude", "gemini", "deepseek", "qwen"]
        run, task = task_run()
        run.compare_all = True
        task.status, task.output = "accepted", "The value is 41."
        async def reply(run_arg, provider, call, live=None):
            return ModelResult(provider, "fake", judgement(provider != "gemini")
                               if call.kind == "final_eval" else "The value is 41.")
        c._call = reply
        await c._integrate(run, {})
        assert len(run.final.judges) == 3
        assert run.final.stalled_context_hash and run.final.meets_criteria is False
        assert any("42" in gap for gap in run.final.gaps)
    asyncio.run(scenario())


def test_stall_limitation_remains_visible_when_prior_limitations_fill_display_limit():
    async def scenario():
        c = controller()
        run, _ = await stalled_final(c)
        run.final.limitations = [f"Prior limitation {index}" for index in range(20)]
        c._call = lambda *args, **kwargs: pytest.fail("No repeated call expected")
        await c._integrate(run, {})
        assert len(run.final.limitations) == 14
        assert "unchanged result" in run.final.limitations[0]
        assert run.final.meets_criteria is False
    asyncio.run(scenario())


def test_passing_candidate_is_never_marked_stalled_or_given_a_fix_request():
    async def scenario():
        c = controller()
        run, task = task_run()
        task.status, task.output = "accepted", "The value is 42."
        calls = []
        async def reply(run_arg, provider, call, live=None):
            calls.append(call.kind)
            return ModelResult(provider, "fake", judgement(True) if call.kind == "final_eval" else "The value is 42.")
        c._call = reply
        await c._integrate(run, {})
        assert calls == ["integrate", "final_eval"]
        assert not run.final.stalled_context_hash and run.final.meets_criteria is True
        assert not any(event.type == "no_progress" for event in run.events)
    asyncio.run(scenario())
