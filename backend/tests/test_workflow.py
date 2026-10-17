import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.gateway.base import ModelCall, ModelResult, ProviderError
from app.main import create_app
from app.workflow.controller import Controller
from app.workflow.prompts import parse_json


def make_client(**overrides) -> TestClient:
    opts = {"allow_simulated": True, **overrides}
    settings = Settings(_env_file=None, simulated_delay=0.0, static_dir="/nonexistent", **opts)
    return TestClient(create_app(settings))


def wait_for(client: TestClient, run_id: str, statuses: set[str], timeout: float = 10) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run stuck in {run['status']}")


def test_parse_json_handles_fences_and_prose():
    assert parse_json('Sure!\n```json\n{"a": {"b": "}"}}\n```') == {"a": {"b": "}"}}
    assert parse_json("no json here") is None


def test_full_run_in_demo_mode_with_plan_approval():
    with make_client() as client:
        providers = client.get("/api/providers").json()
        assert providers["demo_mode"] is True
        run = client.post("/api/runs", json={"request": "Design a todo app API", "profile": "software"}).json()
        run = wait_for(client, run["id"], {"awaiting_approval"})
        assert run["plan"]["tasks"], "plan has tasks"
        assert run["plan_review"]["reviewer"] != run["plan"]["planner"], "plan reviewed by a different model"

        client.post(f"/api/runs/{run['id']}/approve", json={"note": "go"})
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")
        for task in run["plan"]["tasks"]:
            assert task["status"] == "accepted"
            assert task["reviewer"] != task["implementer"], "independent cross-review"
            assert task["assurance"] == "evidence-reviewed"
        assert run["final"]["output"]
        assert run["final"]["evaluator"] != run["final"]["integrator"]
        assert run["usage"]["calls"] > 0
        assert run["simulated"] is True


def test_budget_limit_stops_run_and_keeps_state():
    with make_client(max_model_calls_per_run=3) as client:
        run = client.post("/api/runs", json={"request": "Write a report", "require_plan_approval": False}).json()
        run = wait_for(client, run["id"], {"budget_stopped", "completed", "failed"})
        assert run["status"] == "budget_stopped"
        assert run["plan"] is not None


def test_access_code_gate():
    with make_client(app_access_code="secret-code") as client:
        assert client.get("/api/runs").status_code == 401
        assert client.post("/api/login", json={"code": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"code": "secret-code"}).status_code == 200
        assert client.get("/api/runs").status_code == 200


def test_dependency_cycles_are_broken():
    from app.models import Task

    tasks = [Task(id="a", title="a", depends_on=["b"]), Task(id="b", title="b", depends_on=["a"])]
    Controller._break_cycles(tasks)
    assert not (tasks[0].depends_on and tasks[1].depends_on)


def test_failing_provider_is_replaced():
    """A provider that errors is skipped and its work is reassigned; the run still completes."""
    with make_client() as client:
        registry = client.app.state.registry

        class Broken:
            provider = "chatgpt"
            display_name = "ChatGPT"
            simulated = True

            async def list_models(self):
                return ["broken"]

            async def resolve_model(self):
                return "broken"

            async def generate(self, call: ModelCall) -> ModelResult:
                raise ProviderError("chatgpt", "boom")

        registry.adapters["chatgpt"] = Broken()
        run = client.post("/api/runs", json={"request": "Plan a launch", "require_plan_approval": False}).json()
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")
        assert any(e["type"] == "provider_error" for e in run["events"])


def test_real_mode_needs_keys_before_running():
    with make_client(allow_simulated=False) as client:
        p = client.get("/api/providers").json()
        assert p["needs_keys"] is True
        assert all(not x["connected"] for x in p["providers"])
        r = client.post("/api/runs", json={"request": "Hello there"})
        assert r.status_code == 400
        assert "API key" in r.json()["detail"]


def test_keys_saved_from_app_are_never_returned(monkeypatch):
    with make_client(allow_simulated=False) as client:
        registry = client.app.state.registry

        async def fake_test(provider, key):
            if key.endswith("bad-key-0000"):
                raise RuntimeError(f"401 invalid key {key}")
            return "model-x"

        monkeypatch.setattr(registry, "test_key", fake_test)
        secret = "sk-test-1234567890abcdWXYZ"
        r = client.put("/api/keys/claude", json={"key": secret})
        assert r.status_code == 200, r.text
        assert secret not in r.text and r.json()["hint"] == "…WXYZ"

        bad = client.put("/api/keys/gemini", json={"key": "sk-bad-key-0000"})
        assert bad.status_code == 400 and "sk-bad-key-0000" not in bad.text

        assert client.put("/api/keys/chatgpt", json={"key": "short"}).status_code == 400

        for path in ("/api/keys", "/api/providers"):
            assert secret not in client.get(path).text
        provs = {p["provider"]: p for p in client.get("/api/providers").json()["providers"]}
        assert provs["claude"]["connected"] and provs["claude"]["model"] == "model-x"
        assert not provs["gemini"]["connected"]

        assert client.delete("/api/keys/claude").status_code == 200
        assert not any(p["configured"] for p in client.get("/api/keys").json())
