"""Regression checks for truthful verification and adaptive, bounded effort. No live providers."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.files import StoredFile
from app.gateway.base import ModelCall, ModelResult, ProviderError
from app.gateway.registry import ModelRegistry
from app.main import create_app
from app.models import CheckResult, CreateRun, FinalResult, Judgement, Plan, Review, Run, SourceEvidence, Task
from app.store.memory import MemoryStore
from app.workflow import policy
from app.workflow.controller import BudgetExceeded, Controller
from app.workflow.profiles import get_profile
from app.workflow.router import PerformanceHistory


def controller(**overrides):
    settings = Settings(_env_file=None, allow_simulated=True, simulated_delay=0, **overrides)
    return Controller(settings, ModelRegistry(settings), MemoryStore(), PerformanceHistory())


@pytest.mark.parametrize("text,default", [
    ('{"score": 0, "findings": ["broken"]}', "pass"),
    ('{"verdict":"banana","score":0,"findings":[]}', "pass"),
    ('{"verdict":"accept","score":8,"findings":[]}', "pass"),
    ('{"verdict":"pass","score":true,"findings":[]}', "pass"),
    ('{"verdict":"pass","score":9,"findings":"not a list"}', "pass"),
    ('{"verdict":"pass","score":9,"findings":[]}', "accept"),
    ("not JSON", "accept"),
])
def test_invalid_reviews_cannot_approve_work(text, default):
    result = controller()._parse_review(text, "judge", "model", default)
    assert result.verdict == "revise" and result.score is None


def test_string_false_cannot_pass_final_review():
    async def scenario():
        c = controller()
        run = Run(request="Check a report", plan=Plan(), final=FinalResult(output="draft"))
        c._stamp_final(run.final)

        async def reply(*args, **kwargs):
            return ModelResult(provider="gemini", model="fake", text=json.dumps({
                "meets_criteria": "false", "score": 9, "must_fix": [], "gaps": [], "limitations": []}))

        c._call = reply
        judged = await c._judge(run, run.plan, run.final.output, get_profile("general"), ["gemini"], 0)
        assert judged[0].meets_criteria is None and judged[0].score is None
        assert judged[0].artifact_hash == run.final.artifact_hash
    asyncio.run(scenario())


def test_editing_artifact_invalidates_old_score_but_keeps_audit_history():
    c = controller()
    final = FinalResult(output="original", integrator="claude")
    run = Run(request="Write a result", final=final)
    c._stamp_final(final)
    old_hash = final.artifact_hash
    final.judges = [Judgement(judge="gemini", score=9, meets_criteria=True, artifact_hash=old_hash, artifact_version=1)]
    c._refresh_final_assessment(run, final)
    assert final.score == 9 and final.verification_status == "reviewed"
    final.output = "changed after review"
    c._refresh_final_assessment(run, final)
    assert final.artifact_hash != old_hash and final.artifact_version == 2
    assert final.score is None and final.meets_criteria is None
    assert final.verification_status == "unverified" and len(final.judges) == 1


def test_current_code_failure_overrides_positive_model_review():
    c = controller()
    final = FinalResult(output="```python solution.py\ndef broken(:\n    x = 1\n    return x\n```", integrator="claude")
    run = Run(request="Write Python", profile="software", final=final)
    c._stamp_final(final)
    code_hash = hashlib.sha256(run.solution["code"].encode()).hexdigest()
    final.checks = [CheckResult(id="syntax", kind="python_syntax", status="fail", summary="Invalid syntax", artifact_hash=code_hash)]
    final.judges = [Judgement(judge="gemini", score=9, meets_criteria=True, artifact_hash=final.artifact_hash)]
    c._refresh_final_assessment(run, final)
    assert final.verification_status == "failed"
    assert final.meets_criteria is False
    assert final.checks[0].artifact_hash == code_hash


def test_latest_review_round_supersedes_older_opinion_on_identical_bytes():
    c = controller()
    final = FinalResult(output="Same bytes", integrator="claude")
    run = Run(request="Review again", final=final)
    c._stamp_final(final)
    final.judges = [Judgement(judge="gemini", score=4, meets_criteria=False, round=0, artifact_hash=final.artifact_hash)]
    c._refresh_final_assessment(run, final)
    final.judges.append(Judgement(judge="gemini", score=8, meets_criteria=True, round=1, artifact_hash=final.artifact_hash))
    c._refresh_final_assessment(run, final)
    assert final.verification_status == "reviewed" and final.score == 8 and final.meets_criteria is True
    assert len(final.judges) == 2 and len(final.checks) == 2, "older opinions remain auditable"


def test_truncated_nonempty_response_is_retried_before_acceptance():
    async def scenario():
        c = controller()
        sizes = []

        async def reply(call):
            sizes.append(call.max_output_tokens)
            return ModelResult(provider="claude", model="fake", text="unfinished" if len(sizes) == 1 else "complete",
                               stop_reason="max_tokens" if len(sizes) == 1 else "end_turn", output_tokens=10)

        c.registry.adapters["claude"].generate = reply
        run = Run(request="Complete it")
        result = await c._call(run, "claude", ModelCall("system", "prompt", "task", 1000))
        assert result.text == "complete" and sizes[1] > sizes[0]
        assert run.usage.output_tokens == 20
        rows = c.store.outputs[run.id]
        assert len(rows) == 2
        assert rows[0]["verdict"] == "incomplete" and rows[0]["score"] is None
        assert rows[0]["content"] == "unfinished" and rows[1]["content"] == "complete"
        assert len(run.partial_outputs) == 1 and not run.partial_outputs[0].usage_estimated
    asyncio.run(scenario())


def test_reassignment_preserves_failed_draft_feedback_and_review_evidence():
    async def scenario():
        c = controller(max_revisions_per_task=0)
        task = Task(id="t1", title="Compute", description="Use the exact source value")
        run = Run(request="The original requirement", materials="Value must be 42", plan=Plan(goal="Compute", tasks=[task],
                  success_criteria=["Use 42 exactly"]), evidence=[SourceEvidence(id="s1", url="https://example.test/source", excerpt="42")])
        f = StoredFile(id="f1", name="values.csv", mime="text/csv", kind="text", data=b"value\n42", size=8, text="value\n42")
        calls = []
        work = []

        async def reply(run_arg, provider, call, live=None):
            calls.append(call)
            if call.kind == "task":
                work.append(call.prompt)
                text = "First draft uses 41" if len(work) == 1 else "Corrected draft uses 42"
            else:
                text = json.dumps({"verdict": "block" if len(work) == 1 else "pass", "score": 1 if len(work) == 1 else 9,
                                   "findings": ["Use exact source value 42"]})
            return ModelResult(provider=provider, model="fake", text=text)

        c._call = reply
        await c._run_task(run, task, {}, [f])
        assert task.status == "accepted" and len(work) == 2
        assert "First draft uses 41" in work[1] and "Use exact source value 42" in work[1]
        review = next(x for x in calls if x.kind == "review")
        assert "The original requirement" in review.prompt and "Use 42 exactly" in review.prompt
        assert "https://example.test/source" in review.prompt and review.attachments == [f]
    asyncio.run(scenario())


def test_policy_allocates_work_and_preserves_explicit_legacy_settings():
    simple = Run(request="Write a short thank-you note")
    assert policy.allocate(simple, 4, 6).analysts == 1
    research = Run(request="Research the evidence", profile="research")
    assert policy.allocate(research, 4, 6).analysts == 3
    assert policy.allocate(research.model_copy(update={"mode": "quick"}), 4, 6).analysts == 1
    assert policy.allocate(simple.model_copy(update={"mode": "deep"}), 4, 6).analysts == 4
    assert policy.allocate(simple.model_copy(update={"compare_all": True}), 4, 6).cross_check
    assert policy.allocate(simple.model_copy(update={"compare_all": False}), 4, 6).analysts == 0
    initial = policy.finish_reserve(research)
    policy.observe(research, "integrate", 12)
    policy.observe(research, "final_eval", 4)
    assert policy.finish_reserve(research) < initial
    assert policy.reasoning_effort(research, "task", 3600) == "high"
    assert policy.reasoning_effort(research, "task", 1) == "medium"


@pytest.mark.parametrize("mode,kind,expected", [("quick", "analysis", "low"), ("quick", "integrate", "medium"),
                                               ("deep", "task", "high"), ("adaptive", "task", "medium")])
def test_provider_receives_mode_appropriate_reasoning_effort(mode, kind, expected):
    async def scenario():
        c = controller()
        seen = []

        async def reply(call):
            seen.append(call.reasoning_effort)
            return ModelResult(provider="claude", model="fake", text="Done")

        c.registry.adapters["claude"].generate = reply
        await c._call(Run(request="Write a note", mode=mode), "claude", ModelCall("s", "p", kind, 1000))
        assert seen == [expected]
    asyncio.run(scenario())


@pytest.mark.parametrize("minutes", [0, 4, 5, 15, 19, 121, -1])
def test_requested_deadline_is_bounded(minutes):
    with pytest.raises(ValidationError):
        CreateRun(request="A request", time_limit_minutes=minutes)


@pytest.mark.parametrize("minutes", [20, 30, 60, 120])
def test_new_research_targets_allow_twenty_minutes_or_more(minutes):
    assert CreateRun(request="A request", time_limit_minutes=minutes).time_limit_minutes == minutes


def test_admitted_graceful_call_keeps_minimum_even_near_research_target():
    run = Run(request="Research a complex problem", timing_policy="graceful")
    assert policy.stage_allowance(run, "analysis", 1) == 1200
    assert policy.call_allowance(run, "task", 0) == 1200
    assert policy.finalization_allowance(run) == 1500
    policy.observe(run, "analysis", 900)
    assert policy.call_allowance(run, "analysis") == 1800
    policy.observe(run, "analysis", 10_000)
    assert policy.call_allowance(run, "analysis") == 2400


def test_phase_configuration_cannot_shorten_the_twenty_minute_minimum():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_phase_min_minutes=19)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_total_run_minutes=121)


def test_resume_does_not_grant_extra_time_beyond_requested_deadline():
    c = controller()
    run = Run(request="Finish", time_limit_s=300, used_s=300, wrap_up_s=30)
    c._clock_start(run)
    assert run.deadline_at - run.clock_started_at == 0


def test_inflight_token_reservations_prevent_parallel_oversubscription():
    async def scenario():
        c = controller(max_tokens_per_run=15_000)
        started, release = asyncio.Event(), asyncio.Event()

        async def reply(call):
            started.set()
            await release.wait()
            return ModelResult(provider="claude", model="fake", text="done", input_tokens=10, output_tokens=10)

        c.registry.adapters["claude"].generate = reply
        run = Run(request="Parallel work")
        first = asyncio.create_task(c._call(run, "claude", ModelCall("s", "p", "task", 1000)))
        await started.wait()
        with pytest.raises(BudgetExceeded):
            await c._call(run, "gemini", ModelCall("s", "p", "task", 1000))
        release.set()
        await first
        assert c._token_reservations[run.id] == 0
        assert run.usage.calls == 1
    asyncio.run(scenario())


def test_gap_task_expansion_is_bounded():
    async def scenario():
        c = controller(max_gap_tasks_per_run=1, max_replans_per_run=1)
        run = Run(request="Research an unresolved claim", profile="research", mode="deep", plan=Plan(tasks=[]))

        async def work(run, task, load, files):
            task.status, task.output = "accepted", "Evidence still leaves uncertainty."

        c._run_task = work
        assert await c._expand_for_gaps(run, {}, ["Missing evidence"])
        assert not await c._expand_for_gaps(run, {}, ["Another gap"])
        assert len(run.plan.tasks) == 1 and run.gap_tasks_added == 1
    asyncio.run(scenario())


def test_shutdown_saves_interrupted_state_and_freezes_clock():
    async def scenario():
        c = controller()
        started = asyncio.Event()

        async def analyze(run, files):
            started.set()
            await asyncio.Event().wait()

        async def gather(*args):
            pass

        c._analyze, c._gather_evidence = analyze, gather
        run = Run(request="Work until shutdown")
        await c.start(run)
        await started.wait()
        await c.shutdown()
        assert run.status == "interrupted" and run.clock_started_at is None
        assert not c.is_active(run.id)
    asyncio.run(scenario())


def test_checkpoint_failure_is_explicit_and_not_an_unhandled_task_error():
    async def scenario():
        c = controller()

        async def broken(*args, **kwargs):
            raise OSError("storage unavailable")

        async def gather(*args):
            pass

        c.store.save, c._gather_evidence = broken, gather
        run = Run(request="Save a result", compare_all=False)
        await c._execute(run)
        assert run.status == "failed" and "Durable checkpoint failed" in run.error
    asyncio.run(scenario())


def test_resume_cannot_bypass_a_rejected_plan():
    async def scenario():
        c = controller()
        run = Run(request="Continue", compare_all=False, require_plan_approval=False,
                  plan=Plan(tasks=[Task(id="t1", title="Work")]),
                  plan_review=Review(reviewer="gemini", verdict="revise"))

        async def unexpected(*args):
            pytest.fail("A rejected plan must not start work on resume")

        async def gather(*args):
            pass

        c._run_tasks, c._gather_evidence = unexpected, gather
        await c._execute(run)
        assert run.status == "failed" and "resume cannot bypass" in run.error
    asyncio.run(scenario())


def test_syntax_failure_triggers_fix_even_when_model_judges_pass():
    async def scenario():
        c = controller(max_final_fix_rounds=1)
        original = "```python solution.py\ndef main(:\n    answer = 42\n    return answer\n```"
        corrected = "```python solution.py\ndef main():\n    answer = 42\n    return answer\n```"
        run = Run(request="Write a Python function", profile="software", plan=Plan(
            goal="Write a function", deliverables=["solution.py"], success_criteria=["Valid Python"],
            tasks=[Task(id="t1", title="Implement", status="accepted", output=original)]))
        fixes = []

        async def reply(run_arg, provider, call, live=None):
            if call.kind == "final_eval":
                text = json.dumps({"meets_criteria": True, "score": 9, "must_fix": [], "gaps": [], "limitations": []})
            elif call.hints.get("task_title") == "Final fixes":
                fixes.append(call.prompt)
                text = corrected
            else:
                text = original
            return ModelResult(provider=provider, model="fake", text=text)

        c._call = reply
        await c._integrate(run, {})
        assert run.final.fixes == 1 and len(fixes) == 1 and "syntax error" in fixes[0]
        assert run.final.output == corrected and run.final.artifact_version == 2
        assert any(ch.kind == "python_syntax" and ch.status == "pass" for ch in run.final.checks)
        assert run.final.verification_status == "partial", "syntax passes, execution remains explicitly unknown"
    asyncio.run(scenario())


def test_invalid_source_record_does_not_discard_valid_evidence(monkeypatch):
    async def scenario():
        c = controller()

        async def supplied(*args, **kwargs):
            return {"items": [{"id": "invalid-without-url"}, {"id": "good", "url": "https://example.test/source",
                    "excerpt": "A source excerpt", "truncated": True}], "issues": [], "searched": False}

        monkeypatch.setattr("app.tools.collect_evidence", supplied)
        run = Run(request="Summarize the supplied material")
        await c._gather_evidence(run, [])
        assert [e.id for e in run.evidence] == ["good"]
        assert run.evidence[0].truncated is True and run.research_status == "supplied_only"
        assert any("invalid" in e.message for e in run.events)
    asyncio.run(scenario())


def test_connected_event_stream_receives_checkpoint_failure_and_closes():
    async def scenario():
        app = create_app(Settings(_env_file=None, allow_simulated=True, simulated_delay=0))
        c = app.state.controller
        run = Run(request="Save a result", compare_all=False)
        await c.store.save(run)

        async def disconnected():
            return False

        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/runs/{run_id}/events")
        response = await endpoint(run.id, SimpleNamespace(is_disconnected=disconnected))
        stream = response.body_iterator
        first = await anext(stream)
        assert json.loads(first.removeprefix("data: "))["status"] == "planning"

        async def broken(*args, **kwargs):
            raise OSError("storage unavailable")

        async def gather(*args):
            pass

        c.store.save, c._gather_evidence = broken, gather
        await c._execute(run)
        final = await asyncio.wait_for(anext(stream), timeout=1)
        payload = json.loads(final.removeprefix("data: "))
        assert payload["status"] == "failed" and "Durable checkpoint failed" in payload["error"]
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)
        assert not c._subscribers.get(run.id)
    asyncio.run(scenario())


def test_general_code_example_is_not_a_phantom_downloadable_artifact():
    async def scenario():
        c = controller()
        run = Run(request="Explain an example", profile="general", final=FinalResult(
            output="```python\ndef intentionally_invalid(:\n    a = 1\n    return a\n```"))
        assert run.solution is None
        await c._verify_final(run)
        assert not any(ch.kind in ("python_syntax", "execution") for ch in run.final.checks)
        assert all(ch.artifact_hash == run.final.artifact_hash for ch in run.final.checks)
    asyncio.run(scenario())


@pytest.mark.parametrize("profile,source_kind,output,expected", [
    ("general", "attachment", "Summary of the supplied data", False),
    ("research", "attachment", "Research conclusion", True),
    ("general", "web", "Summary of retrieved data", True),
    ("general", "google_search", "Summary of search results", True),
    ("general", "attachment", "See https://example.org/source", True),
])
def test_web_citation_check_requires_relevant_scope(profile, source_kind, output, expected):
    async def scenario():
        c = controller()
        run = Run(request="Summarize", profile=profile, final=FinalResult(output=output),
                  evidence=[SourceEvidence(id="s1", url="attachment:file1", source_kind=source_kind)])
        await c._verify_final(run)
        assert any(check.kind == "citations" for check in run.final.checks) is expected
    asyncio.run(scenario())
