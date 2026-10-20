"""Question-driven orchestration: exact checkpoint reuse, bounded critique, honest gates."""
import asyncio
import json

import pytest

from app.config import Settings
from app.gateway.base import ModelResult, ProviderError
from app.gateway.registry import ModelRegistry
from app.models import BridgeState, CreateRun, Run, SourceEvidence
from app.store.memory import MemoryStore
from app.workflow.controller import BudgetExceeded, Controller, NoProviders
from app.workflow.router import PerformanceHistory
from app.workflow import policy


def review(verdict="pass", quote="41", limitations=None):
    return json.dumps({
        "verdict": verdict, "checks": ["Checked the answer against the requested value."],
        "findings": ([{"kind": "correctness", "severity": "major", "target_quote": quote,
                      "problem": "The required value is 42.", "proposed_fix": "Return 42.", "refs": []}]
                     if verdict == "revise" else []),
        "limitations": limitations or [],
    })


def make_controller():
    settings = Settings(_env_file=None, allow_simulated=True, simulated_delay=0,
                        enable_web_research=False, max_tokens_per_run=5_000_000)
    controller = Controller(settings, ModelRegistry(settings), MemoryStore(), PerformanceHistory())
    controller._eligible = lambda _: ["claude", "gemini"]
    return controller


def new_run(**kwargs):
    return Run(request="Return the value 42.", workflow="bridge", timing_policy="question_driven",
               require_plan_approval=False, research_status="not_needed", **kwargs)


def responses(controller, values):
    calls = []

    async def call(run, provider, request, live=None):
        controller._check_budget(run)
        calls.append((provider, request))
        run.usage.calls += 1
        value = values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return ModelResult(provider, "test-model", value)

    controller._call = call
    return calls


def test_simple_question_uses_one_full_answer_and_independent_compact_review():
    async def scenario():
        c, run = make_controller(), new_run()
        calls = responses(c, ["The value is 42.", review()])
        await c._execute_bridge(run, {}, [])
        assert len(calls) == 2
        assert [p for p, _ in calls] == ["claude", "gemini"]
        assert calls[0][1].max_output_tokens == 4000
        assert calls[1][1].max_output_tokens == 1800
        assert all(call.preserve_prompt for _, call in calls)
        assert json.loads(calls[1][1].prompt)["answer"] == "The value is 42."
        assert run.final.output == "The value is 42."
        assert run.final.verification_status == "reviewed"
        assert run.final.score is None
        assert run.status == "completed" and run.bridge.phase == "complete"
        assert not run.analyses and not run.cross_checks and not run.plan
    asyncio.run(scenario())


def test_one_correction_then_review_never_loops_on_more_findings():
    async def scenario():
        c, run = make_controller(), new_run()
        calls = responses(c, ["The value is 41.", review("revise"), "The value is 43.",
                              review("revise", "43")])
        await c._execute_bridge(run, {}, [])
        assert len(calls) == 4 and run.final.fixes == 1
        assert [p for p, _ in calls] == ["claude", "gemini", "claude", "gemini"]
        assert run.bridge.correction_attempted and run.bridge.correction_completed
        assert run.final.output == "The value is 43."
        assert run.final.verification_status == "failed" and run.final.meets_criteria is False
        assert any("42" in gap for gap in run.final.gaps)
        assert json.loads(calls[2][1].prompt)["answer"] == "The value is 41."
    asyncio.run(scenario())


def test_unchanged_correction_stops_without_duplicate_review_and_resume_is_free():
    async def scenario():
        c, run = make_controller(), new_run()
        calls = responses(c, ["The value is 41.", review("revise"), "The value is 41."])
        await c._execute_bridge(run, {}, [])
        assert len(calls) == 3 and run.final.verification_status == "failed"
        restored = Run.model_validate_json(run.model_dump_json())
        await c._execute_bridge(restored, {}, [])
        assert len(calls) == 3
        assert restored.final.verification_status == "failed"
        assert any("unchanged" in note for note in restored.final.limitations)
        assert len(restored.final.judges) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["request", "materials", "evidence", "freshness", "criteria"])
def test_resumed_review_reuses_only_exact_artifact_and_context(change):
    async def scenario():
        c, run = make_controller(), new_run(evidence=[
            SourceEvidence(id="s1", url="file://facts", excerpt="42", source_kind="file")])
        calls = responses(c, ["The value is 42.", review(), review("uncertain", limitations=["Changed evidence requires checking."])])
        await c._execute_bridge(run, {}, [])
        restored = Run.model_validate_json(run.model_dump_json())
        if change == "request":
            restored.request += " Also explain uncertainty."
        elif change == "materials":
            restored.materials = "New requirement"
        elif change == "evidence":
            restored.evidence[0].excerpt = "43"
        elif change == "freshness":
            restored.evidence[0].retrieved_at += 1
        else:
            restored.profile = "software"
        await c._execute_bridge(restored, {}, [])
        assert len(calls) == 3
        assert restored.final.verification_status == "partial"
        assert restored.final.meets_criteria is None
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["not JSON", '{"verdict": "pass"}', review("pass", limitations=["I could not check."])])
def test_invalid_review_never_passes_or_creates_another_correction(bad):
    async def scenario():
        c, run = make_controller(), new_run()
        calls = responses(c, ["The value is 42.", bad])
        await c._execute_bridge(run, {}, [])
        assert len(calls) == 2 and not run.bridge.correction_attempted
        assert run.final.verification_status == "partial"
        assert run.final.meets_criteria is None
        assert any("invalid" in gap for gap in run.final.gaps)
    asyncio.run(scenario())


def test_missing_independent_provider_does_not_fall_back_to_self_review():
    async def scenario():
        c, run = make_controller(), new_run()
        c._eligible = lambda _: ["claude"]
        calls = responses(c, ["The value is 42."])
        await c._execute_bridge(run, {}, [])
        assert len(calls) == 1 and not run.final.judges
        assert run.final.verification_status == "unverified"
        assert any("No independent model" in note for note in run.final.limitations)
    asyncio.run(scenario())


def test_changed_context_without_provider_invalidates_old_passing_review():
    async def scenario():
        c, run = make_controller(), new_run()
        responses(c, ["The value is 42.", review()])
        await c._execute_bridge(run, {}, [])
        assert run.final.meets_criteria is True
        run.request += " Explain the mechanism."
        c._eligible = lambda _: ["claude"]
        await c._execute_bridge(run, {}, [])
        assert run.final.meets_criteria is None
        assert run.final.verification_status == "unverified"
    asyncio.run(scenario())


def test_optional_plan_is_one_bounded_packet_not_task_pipeline():
    async def scenario():
        c, run = make_controller(), new_run(mode="deep")
        calls = responses(c, [json.dumps({"focus": ["Answer exactly"], "checks": ["Value is 42"], "open_questions": []}),
                              "The value is 42.", review()])
        await c._execute_bridge(run, {}, [])
        assert len(calls) == 3 and calls[0][1].max_output_tokens == 900
        assert run.plan.success_criteria == ["Value is 42"] and not run.plan.tasks
        assert run.bridge.plan["focus"] == ["Answer exactly"]
        assert not run.plan_review
    asyncio.run(scenario())


def test_interrupted_correction_keeps_answer_and_does_not_repeat_on_resume():
    async def scenario():
        c, run = make_controller(), new_run()
        calls = responses(c, ["The value is 41.", review("revise"), asyncio.CancelledError()])
        with pytest.raises(asyncio.CancelledError):
            await c._execute_bridge(run, {}, [])
        assert run.bridge.correction_attempted and not run.bridge.correction_completed
        restored = Run.model_validate_json(run.model_dump_json())
        await c._execute_bridge(restored, {}, [])
        assert len(calls) == 3 and restored.final.output == "The value is 41."
        assert restored.final.verification_status == "failed"
        assert any("interrupted" in note for note in restored.final.limitations)
    asyncio.run(scenario())


def test_new_run_has_no_session_deadline_but_elapsed_and_legacy_limits_survive():
    c, run = make_controller(), new_run(used_s=9000, time_limit_minutes=120)
    c._set_limit(run)
    c._clock_start(run)
    assert run.time_limit_minutes is None
    assert run.time_limit_s == run.total_time_limit_s == run.model_phase_min_s == 0
    assert run.clock_started_at and run.elapsed_s >= 9000
    assert c._time_left(run) == (None, "") and c._has_time(run, 999999)
    c._check_budget(run)
    c._clock_stop(run)
    legacy = Run(request="Legacy saved question")
    c._set_limit(legacy)
    assert legacy.workflow == "legacy" and legacy.timing_policy == "graceful"
    assert legacy.total_time_limit_s == 7200


def test_caps_still_stop_question_driven_run_and_do_not_force_allocation():
    c, run = make_controller(), new_run()
    c.settings.max_model_calls_per_run = 1
    run.usage.calls = 1
    with pytest.raises(BudgetExceeded):
        c._check_budget(run)
    run.usage.calls = 0
    run.usage.input_tokens = 4_999_999
    with pytest.raises(BudgetExceeded):
        c._check_budget(run, 2)
    assert policy.answer_tokens(new_run(), 64000) == 4000
    assert policy.answer_tokens(new_run(profile="software"), 64000) == 16000
    assert CreateRun(request="hello").require_plan_approval is False
    assert len(CreateRun(request="hello", file_ids=["f"] * 20).file_ids) == 20


def test_real_call_usage_transport_timeout_and_cancellation_are_preserved():
    async def scenario():
        c, run = make_controller(), new_run(bridge=BridgeState())
        seen = []
        class Adapter:
            async def generate(self, call):
                seen.append(call)
                await call.on_text("partial answer")
                raise asyncio.CancelledError()
        c.registry.adapters["claude"] = Adapter()
        from app.gateway.base import ModelCall
        with pytest.raises(asyncio.CancelledError):
            await c._call(run, "claude", ModelCall("rules", "request", "task", 4000, preserve_prompt=True))
        assert run.usage.calls == 1 and run.usage.estimated_tokens > 0
        assert run.partial_outputs[-1].text == "partial answer"
        assert not run.bridge.inflight_tokens
        assert seen[0].timeout_seconds == c.settings.provider_request_timeout_seconds + 30
    asyncio.run(scenario())


def test_provider_request_timeout_remains_bounded_without_session_timer():
    async def scenario():
        c, run = make_controller(), new_run()
        async def slow():
            await asyncio.sleep(5)
        with pytest.raises(asyncio.TimeoutError):
            await c._wait_for_request(run, slow(), .01)
    asyncio.run(scenario())


def test_bridge_transport_keeps_one_json_object_and_bounded_packet_output():
    async def scenario():
        c, run = make_controller(), new_run(bridge=BridgeState())
        seen = []
        class Adapter:
            simulated = True
            async def generate(self, call):
                seen.append(call)
                assert json.loads(call.prompt) == {"request": run.request, "materials": ""}
                return ModelResult("gemini", "fake", "incomplete", 100, 200, stop_reason="max_tokens")
        c.registry.adapters["gemini"] = Adapter()
        from app.gateway.base import ModelCall
        with pytest.raises(ProviderError):
            await c._call(run, "gemini", ModelCall("rules", json.dumps({"request": run.request, "materials": ""}),
                                                  "final_eval", 1800, preserve_prompt=True))
        assert len(seen) == 1
        assert seen[0].max_output_tokens == 1800
        assert seen[0].reasoning_effort == "low"
        assert run.usage.calls == 1 and run.usage.total_tokens == 300
        assert not run.bridge.inflight_tokens
    asyncio.run(scenario())


def test_budget_stop_after_answer_retains_complete_draft_without_claiming_review():
    async def scenario():
        c, run = make_controller(), new_run()
        c._set_limit(run)
        c.settings.max_model_calls_per_run = 1
        calls = responses(c, ["The value is 42."])
        await c._execute(run)
        assert len(calls) == 1 and run.status == "budget_stopped"
        assert run.final.output == "The value is 42."
        assert run.final.verification_status == "unverified"
        assert run.clock_started_at is None and run.used_s > 0
    asyncio.run(scenario())


def test_unknown_provider_usage_is_charged_before_restart_work():
    async def scenario():
        c, run = make_controller(), new_run(bridge=BridgeState(inflight_tokens=1234, inflight_provider="claude",
                                                              inflight_stage="task"))
        c._set_limit(run)
        calls = responses(c, ["The value is 42.", review()])
        await c._execute(run)
        assert run.status == "completed" and len(calls) == 2
        assert run.usage.estimated_tokens == 1234 and run.usage.total_tokens == 1234
        assert not run.bridge.inflight_tokens
    asyncio.run(scenario())


def test_api_new_runs_ignore_legacy_timer_and_advertise_question_driven(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app(Settings(_env_file=None, allow_simulated=True, simulated_delay=0, static_dir="/nonexistent"))
    async def save_without_model_calls(run):
        app.state.controller._set_limit(run)
        await app.state.store.save(run)
    monkeypatch.setattr(app.state.controller, "start", save_without_model_calls)
    with TestClient(app) as client:
        response = client.post("/api/runs", json={"request": "Return 42", "time_limit_minutes": 120})
        assert response.status_code == 200
        run = response.json()
        assert run["workflow"] == "bridge" and run["timing_policy"] == "question_driven"
        assert not run["require_plan_approval"] and run["time_limit_minutes"] is None
        assert run["total_time_limit_s"] == run["time_limit_s"] == 0
        limits = client.get("/api/limits").json()
        assert limits["workflow"] == "bridge" and limits["max_total_run_minutes"] == 0
        assert limits["max_tokens_per_run"] == 5_000_000
        from app.files import LOCAL_RUN_BYTES, MAX_RUN_BYTES
        assert limits["max_run_bytes"] == LOCAL_RUN_BYTES  # local fallback; cloud keeps the larger cap
        assert MAX_RUN_BYTES == 10 * 1024**3
        assert limits["max_files_per_run"] == 20
        assert limits["provider_request_timeout_seconds"] == 900
