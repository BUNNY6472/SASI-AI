import importlib.util
import json
from pathlib import Path

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "evals" / "run_eval.py"
spec = importlib.util.spec_from_file_location("sasi_eval_harness", SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_dry_run_never_constructs_a_network_client(monkeypatch, capsys):
    monkeypatch.setattr(evaluation.httpx, "Client", lambda *a, **k: pytest.fail("dry run made an API request"))
    assert evaluation.main(["--validate", "--max-cases", "6"]) == 0
    assert "No API requests" in capsys.readouterr().out


def test_all_objective_fixture_oracles_accept_their_exact_expected_output():
    cases = evaluation.load_cases(evaluation.DEFAULT_CASES)
    assert any(case["kind"] == "human_review" for case in cases)
    for case in cases:
        if case["kind"] == "objective":
            expected = case["oracle"]["expected"]
            output = expected if case["oracle"]["kind"] == "exact_text" else json.dumps(expected)
            assert evaluation.grade(case, output)["verdict"] == "pass"


def test_objective_oracle_rejects_prose_duplicate_keys_and_wrong_types():
    case = {"kind": "objective", "oracle": {"kind": "json_equals", "expected": {"answer": 12}}}
    for output in ['The answer is 12.', '```json\n{"answer":12}\n```', '{"answer":12,"answer":12}', '{"answer":"12"}', '{"answer":true}']:
        assert evaluation.grade(case, output)["verdict"] == "fail"
    assert evaluation.grade({"kind": "human_review"}, "Looks excellent, score 10/10")["verdict"] == "unscored"


def test_live_run_records_external_outcome_actual_model_checks_and_hash():
    requests = []
    case = evaluation.load_cases(evaluation.DEFAULT_CASES)[0]
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["mode"] == "quick" and body["time_limit_minutes"] == 20
            assert body["require_plan_approval"] is False
            return httpx.Response(200, json={"id": "run-one", "status": "running"})
        return httpx.Response(200, json={"id": "run-one", "status": "completed", "simulated": False,
                                        "usage": {"calls": 3, "input_tokens": 100, "output_tokens": 20},
                                        "analyses": [{"model": "pinned-model"}],
                                        "final": {"output": '{"total":"417.57"}', "score": 1, "checks": [{"kind": "citations", "status": "unknown"}]}})
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        result = evaluation.run_case(client, case, "quick", 20, poll_s=0)
    assert result["external_outcome"]["verdict"] == "pass", "peer score is not the objective label"
    assert result["model_ids"] == ["pinned-model"] and len(result["output_sha256"]) == 64
    assert result["usage"]["calls"] == 3 and result["checks"][0]["status"] == "unknown"


def test_timed_out_run_is_cancelled_and_not_scored(monkeypatch):
    now = iter([0, 2000, 2001])
    monkeypatch.setattr(evaluation.time, "monotonic", lambda: next(now))
    cancelled = []
    def handler(request):
        if request.url.path.endswith("/cancel"):
            cancelled.append(True)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"id": "run-one", "status": "running"})
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        result = evaluation.run_case(client, evaluation.load_cases(evaluation.DEFAULT_CASES)[0], "deep", 20)
    assert cancelled and result["external_outcome"]["verdict"] == "unscored"
    assert result["cancellation"] == "requested" and result["error_type"] == "harness_timeout"


def test_unknown_create_outcome_is_not_retried():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout")
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(evaluation.EvaluationError, match="unknown"):
            evaluation.run_case(client, evaluation.load_cases(evaluation.DEFAULT_CASES)[0], "adaptive", 20)
    assert len(calls) == 1


def test_graceful_run_can_complete_after_its_research_target(monkeypatch):
    now = iter([0, 1300, 1301])
    monkeypatch.setattr(evaluation.time, "monotonic", lambda: next(now))
    def handler(request):
        assert not request.url.path.endswith("/cancel"), "the research target must not cancel admitted work"
        if request.method == "POST":
            return httpx.Response(200, json={"id": "run-one", "status": "analyzing", "timing_policy": "graceful"})
        return httpx.Response(200, json={"id": "run-one", "status": "completed", "timing_policy": "graceful",
                                         "final": {"output": '{"total":"417.57"}'}})
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        result = evaluation.run_case(client, evaluation.load_cases(evaluation.DEFAULT_CASES)[0], "deep", 20, poll_s=0)
    assert result["external_outcome"]["verdict"] == "pass"
    assert result["harness_extra_wait_s"] == 5460


def test_graceful_harness_still_cancels_a_stuck_run(monkeypatch):
    now = iter([0, 7000, 7001])
    monkeypatch.setattr(evaluation.time, "monotonic", lambda: next(now))
    cancelled = []
    def handler(request):
        if request.url.path.endswith("/cancel"):
            cancelled.append(True)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"id": "run-one", "status": "analyzing", "timing_policy": "graceful"})
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        result = evaluation.run_case(client, evaluation.load_cases(evaluation.DEFAULT_CASES)[0], "deep", 20)
    assert cancelled and result["error_type"] == "harness_timeout"


def test_harness_watchdog_respects_total_cap_with_network_margin(monkeypatch):
    now = iter([0, 7300, 7301])
    monkeypatch.setattr(evaluation.time, "monotonic", lambda: next(now))
    def handler(request):
        return httpx.Response(200, json={"id": "run-one", "status": "analyzing", "timing_policy": "graceful", "total_time_limit_s": 7200})
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        result = evaluation.run_case(client, evaluation.load_cases(evaluation.DEFAULT_CASES)[0], "deep", 120)
    assert result["harness_watchdog_s"] == 7260
    assert result["error_type"] == "harness_timeout" and result["cancellation"] == "requested"


def test_invalid_fixture_cannot_introduce_executable_oracle(tmp_path):
    fixture = tmp_path / "cases.json"
    fixture.write_text(json.dumps([{"id": "bad", "kind": "objective", "request": "Do work", "oracle": {"kind": "python", "expected": "exec('bad')"}}]))
    with pytest.raises(evaluation.EvaluationError, match="Unsupported oracle"):
        evaluation.load_cases(fixture)
