"""The run's time limit and wrap-up, the team of models, inputs that fit each model, and answers that are never empty."""
import asyncio
import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.files import StoredFile
from app.gateway import fit
from app.gateway.base import ModelCall, ModelResult, ProviderError, no_text
from app.gateway.catalog import rank_open
from app.main import create_app


def make_client(**overrides) -> TestClient:
    opts = {"allow_simulated": True, "simulated_delay": 0.0, **overrides}
    return TestClient(create_app(Settings(_env_file=None, static_dir="/nonexistent", **opts)))


def wait_for(client: TestClient, run_id: str, statuses: set[str], timeout: float = 15) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run stuck in {run['status']}")


def spy(client: TestClient, change) -> None:
    """Wrap every model so `change(provider, call, original)` decides what each call does."""
    for p, adapter in client.app.state.registry.adapters.items():
        original = adapter.generate

        async def generate(call: ModelCall, p=p, original=original) -> ModelResult:
            return await change(p, call, original)

        adapter.generate = generate


# ---------------------------------------------------------------- inputs that fit each model


def test_large_files_are_trimmed_to_fit_a_small_context_window():
    data = "id,value\n" + "".join(f"{i},{i * 3}\n" for i in range(40_000))
    f = StoredFile(id="f", name="big.csv", mime="text/csv", size=len(data), kind="text", data=b"", text=data)
    call = ModelCall(system="sys", prompt="Analyse it", kind="analysis", max_output_tokens=8000, attachments=[f])
    assert fit.measure(call, {"text"}) > 100_000
    note = fit.fit(call, context=131_072, reads={"text"}, reply=32_000)
    assert note == "big.csv"
    assert fit.measure(call, {"text"}) + 32_000 + fit.MARGIN <= 131_072, "input plus the reply allowance fits"
    kept = call.attachments[0].text
    assert kept.startswith("id,value\n0,0") and kept.rstrip().endswith("39999,119997"), "the start and end are kept"
    assert "left out here so it fits this model's context window" in kept
    assert f.text == data, "the original file is untouched for models with larger windows"

    small = ModelCall(system="sys", prompt="hi", kind="task")
    assert fit.fit(small, context=131_072, reads=set()) == "" and small.prompt == "hi"


def test_too_long_errors_are_recognised_with_the_limit_they_state():
    gpt_oss = ("HTTP 400: This model's maximum context length is 131072 tokens. However, you requested 133401 tokens "
               "(101401 in the messages, 32000 in the completion).")
    assert fit.context_error(gpt_oss) == 131_072
    assert fit.context_error("HTTP 400: prompt is too long: 1050000 tokens > 1000000 maximum") == 0
    assert fit.context_error("HTTP 401: invalid api key") is None


def test_a_model_that_says_the_input_is_too_long_gets_a_trimmed_input_and_tries_again():
    with make_client() as client:
        registry = client.app.state.registry
        sizes = []

        async def change(p, call, original):
            if p == "glm" and call.kind == "analysis":
                sizes.append(fit.measure(call, {"text"}))
                if len(sizes) == 1:
                    raise ProviderError("glm", "HTTP 400: This model's maximum context length is 60000 tokens. "
                                               "However, you requested 90000 tokens.")
            return await original(call)

        spy(client, change)
        up = client.post("/api/files", content=("a,b\n" + "1,2\n" * 60_000).encode(),
                         headers={"X-File-Name": "data.csv", "Content-Type": "text/csv"}).json()
        run = client.post("/api/runs", json={"request": "Analyse the data", "require_plan_approval": False,
                                             "file_ids": [up["id"]]}).json()
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")
        assert registry.context("glm") == 60_000, "the model's stated limit is remembered"
        assert len(sizes) == 2 and sizes[1] < sizes[0]
        assert any("said the input was too long" in e["message"] for e in run["events"])
        assert next(a for a in run["analyses"] if a["provider"] == "glm")["output"], "GLM's analysis arrived"


# ---------------------------------------------------------------- answers are never empty


def test_a_model_that_only_thinks_is_asked_again_with_more_room():
    with make_client() as client:
        seen = []

        async def change(p, call, original):
            if p == "claude" and call.kind == "analysis":
                seen.append(call.max_output_tokens)
                if len(seen) == 1:
                    raise no_text("claude", "max_tokens")
            if p == "gemini" and call.kind == "analysis":
                result = await original(call)
                result.text = "   "  # an adapter that let an empty answer through
                return result
            return await original(call)

        spy(client, change)
        run = client.post("/api/runs", json={"request": "Analyse churn", "require_plan_approval": False}).json()
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")
        assert len(seen) == 2 and seen[1] > seen[0], "the second try has more room"
        by = {a["provider"]: a for a in run["analyses"]}
        assert by["claude"]["output"], "Claude's analysis shows"
        assert not by["gemini"]["output"] and "no answer text" in by["gemini"]["error"], "an empty answer shows its error"
        assert all(a["output"] or a["error"] for a in run["analyses"]), "no panel is ever blank"


# ---------------------------------------------------------------- the team


def test_the_team_is_frontier_first_then_the_open_models_that_suit_the_task():
    opens = ["glm", "deepseek", "qwen", "kimi"]
    assert rank_open(opens, "software", 10_000)[:2] == ["glm", "deepseek"]
    assert rank_open(opens, "research", 10_000)[:3] == ["glm", "deepseek", "kimi"]
    assert rank_open(["qwen", "kimi", "deepseek"], "software", 400_000)[0] == "deepseek", \
        "a huge input goes to the model whose window holds it"
    assert rank_open(opens, "research", 400_000)[:2] == ["glm", "deepseek"], "GLM 5.2 and DeepSeek both read 1M tokens"

    with make_client(team_size=3) as client:
        registry = client.app.state.registry
        assert registry.team("software") == ["claude", "gemini", "glm"]
        assert registry.team("general", 400_000) == ["claude", "gemini", "glm"]
        assert "chatgpt" not in registry.adapters

    with make_client(team_size=3, open_models="qwen,kimi,deepseek") as client:
        registry = client.app.state.registry
        assert registry.team("general", 400_000) == ["claude", "gemini", "deepseek"], "the one whose window holds it"


# ---------------------------------------------------------------- the time limit


def test_when_time_is_nearly_up_the_run_wraps_up_with_a_complete_result():
    """Task work stops at its share of the time, the final step is cut at the wrap-up, and the wrap-up delivers."""
    with make_client(run_time_limit_minutes=0.1, wrap_up_minutes=0.025) as client:  # 6 s, the last 1.5 s to wrap up
        kinds = []

        async def change(p, call, original):
            kinds.append(call.kind)
            if call.kind in ("task", "integrate"):
                await asyncio.sleep(30)  # far longer than the run may take
            return await original(call)

        spy(client, change)
        started = time.time()
        run = client.post("/api/runs", json={"request": "Build a churn model", "require_plan_approval": False}).json()
        assert run["time_limit_s"] == 6 and run["wrap_up_s"] == 1.5
        run = wait_for(client, run["id"], {"completed", "failed"})
        took = time.time() - started

        assert run["status"] == "completed", run.get("error")
        assert took < 6 + 1.5, f"finished within the limit ({took:.1f}s)"
        assert run["final"]["output"].startswith("# Result"), "a complete result is delivered"
        assert "wrap_up" in kinds, "a model wrote the final result in the wrap-up"
        assert run["wrapping_up"] is True and run["finished_at"] and run["deadline_at"] is None
        types = [e["type"] for e in run["events"]]
        assert "time" in types and "wrap_up" in types and types[-1] == "completed"
        assert any("time limit" in x for x in run["final"]["limitations"])
        assert all(t["status"] != "working" for t in run["plan"]["tasks"]), "no task is left looking busy"
        assert run["live"] == {}
        assert 0 < run["used_s"] <= 6 + 1


def test_the_clock_pauses_while_the_plan_waits_for_approval():
    with make_client(run_time_limit_minutes=0.1, wrap_up_minutes=0.025) as client:
        run = client.post("/api/runs", json={"request": "Plan a launch", "require_plan_approval": True}).json()
        run = wait_for(client, run["id"], {"awaiting_approval"})
        assert run["clock_started_at"] is None and run["deadline_at"] is None, "paused"
        used = run["used_s"]
        time.sleep(6.5)  # longer than the whole limit
        client.post(f"/api/runs/{run['id']}/approve", json={})
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")
        assert "wrap_up" not in [e["type"] for e in run["events"]], "the wait did not use up the run's time"
        assert run["used_s"] < 6 and used <= run["used_s"]


def test_no_cap_on_calls_or_tokens_by_default():
    s = Settings(_env_file=None)
    assert s.max_model_calls_per_run == 0 and s.max_tokens_per_run == 0 and s.eris_max_tokens_per_run == 0
    assert s.run_time_limit_minutes == 60 and s.wrap_up_minutes == 5
    with make_client() as client:
        limits = client.get("/api/limits").json()
        assert limits["time_limit_minutes"] == 60 and limits["wrap_up_minutes"] == 5 and limits["team_size"] == 4
