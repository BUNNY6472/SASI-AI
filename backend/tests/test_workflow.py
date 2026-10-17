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
    opts = {"allow_simulated": True, "simulated_delay": 0.0, **overrides}
    settings = Settings(_env_file=None, static_dir="/nonexistent", **opts)
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
    # 4 analyses + 4 cross-checks + plan + plan review = 10 calls; the cap stops the run at the first task.
    with make_client(max_model_calls_per_run=10) as client:
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
    with make_client(open_models="glm,deepseek,qwen") as client:
        registry = client.app.state.registry

        class Broken:
            provider = "glm"
            display_name = "GLM"
            simulated = True

            async def list_models(self):
                return ["broken"]

            async def resolve_model(self):
                return "broken"

            async def generate(self, call: ModelCall) -> ModelResult:
                raise ProviderError("glm", "boom")

        registry.adapters["glm"] = Broken()
        run = client.post("/api/runs", json={"request": "Plan a launch", "require_plan_approval": False}).json()
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")
        failed = next(e["message"] for e in run["events"] if e["type"] == "provider_error")
        assert failed.startswith("GLM failed") and "joins the team" in failed, "a working model takes the failed one's seat"
        assert len(run["team"]) == 5 and "glm" in run["skipped"]


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

        assert client.put("/api/keys/gemini", json={"key": "short"}).status_code == 400
        assert client.put("/api/keys/chatgpt", json={"key": "sk-openai-1234567890"}).status_code == 404, "no OpenAI key option"

        for path in ("/api/keys", "/api/providers"):
            assert secret not in client.get(path).text
        provs = {p["provider"]: p for p in client.get("/api/providers").json()["providers"]}
        assert provs["claude"]["connected"] and provs["claude"]["model"] == "model-x"
        assert not provs["gemini"]["connected"]

        assert client.delete("/api/keys/claude").status_code == 200
        assert not any(p["configured"] for p in client.get("/api/keys").json())


def test_working_key_is_used_even_if_saving_fails(monkeypatch):
    with make_client(allow_simulated=False) as client:
        registry = client.app.state.registry

        async def fake_test(provider, key):
            return "model-x"

        async def broken_save(provider, key):
            raise PermissionError(f"403 denied while saving {key}")

        monkeypatch.setattr(registry, "test_key", fake_test)
        monkeypatch.setattr(client.app.state.key_store, "save", broken_save)
        secret = "sk-test-1234567890abcdWXYZ"
        r = client.put("/api/keys/claude", json={"key": secret})
        assert r.status_code == 200 and secret not in r.text
        body = r.json()
        assert body["saved"] is False and "PermissionError" in body["warning"]
        assert client.get("/api/providers").json()["needs_keys"] is False, "key is usable right away"


def test_files_reach_the_whole_team_who_analyse_compare_and_delete():
    """Files reach every model on the team, each analyses on its own, they cross-check, and delete removes everything."""
    with make_client() as client:
        csv_body = b"region,sales\nnorth,120\nsouth,80\n"
        up = client.post("/api/files", content=csv_body, headers={"X-File-Name": "sales.csv", "Content-Type": "text/csv"})
        assert up.status_code == 200, up.text
        f = up.json()
        assert f["kind"] == "text"
        img = client.post("/api/files", content=b"\x89PNG\r\n\x1a\nfake", headers={"X-File-Name": "chart.png", "Content-Type": "image/png"}).json()
        empty = client.post("/api/files", content=b"", headers={"X-File-Name": "x.csv", "Content-Type": "text/csv"})
        assert empty.status_code == 400

        run = client.post("/api/runs", json={"request": "Analyse the sales data", "profile": "research", "compare_all": True,
                                             "require_plan_approval": False, "file_ids": [f["id"], img["id"]]}).json()
        assert [x["name"] for x in run["files"]] == ["sales.csv", "chart.png"]
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")
        assert run["team"] == ["claude", "gemini", "glm", "deepseek"], "frontier models first, then the best open models"
        assert {a["provider"] for a in run["analyses"]} == set(run["team"])
        assert all("sales.csv" in a["output"] for a in run["analyses"]), "every model saw the files"
        assert len(run["cross_checks"]) == 4
        assert run["live"] == {}, "live text is cleared when the run ends"

        store = client.app.state.store
        stages = {o["stage"] for o in store.outputs.get(run["id"], [])}
        assert {"analysis", "cross_check", "task", "review", "integrate"} <= stages, stages

        got = client.get(f"/api/runs/{run['id']}/files/{f['id']}")
        assert got.status_code == 200 and got.content == csv_body

        # A staged file can only be used once.
        again = client.post("/api/runs", json={"request": "again", "file_ids": [f["id"]]})
        assert again.status_code == 400

        d = client.delete(f"/api/runs/{run['id']}")
        assert d.status_code == 200 and d.json()["deleted"] is True
        assert client.get(f"/api/runs/{run['id']}").status_code == 404
        assert run["id"] not in store.outputs


def test_csv_profile_and_type_checks():
    from app.files import FileRejected, prepare

    f = prepare("data.csv", "", b"a,b\n1,x\n3,y\n")
    assert "Rows profiled: 2" in f.text and "min 1, max 3" in f.text
    assert "All data rows in the supplied file were inspected" in f.text
    assert f.data == b"a,b\n1,x\n3,y\n", "the bounded model report does not replace original data"
    with pytest.raises(FileRejected):
        prepare("empty.csv", "", b"")
    exe = prepare("tool.exe", "application/octet-stream", b"MZ\x00\x01\x02")
    assert exe.kind == "other", "any type is accepted and kept"
    assert prepare("clip.mov", "", b"0000").kind == "video"
    assert prepare("logo.svg", "image/svg+xml", b"<svg/>").kind == "text"


def test_data_and_document_files_become_readable_text():
    import io
    import json
    import zipfile

    import openpyxl
    import pyarrow as pa
    import pyarrow.parquet as pq

    from app.files import prepare

    buf = io.BytesIO()
    pq.write_table(pa.table({"city": ["Hyd", "Vizag", "Hyd"], "sales": [10.5, 20.0, 30.0]}), buf)
    f = prepare("sales.parquet", "", buf.getvalue())
    assert f.kind == "text" and "Rows: 3" in f.text and "max 30" in f.text and "Vizag" in f.text

    wb = openpyxl.Workbook()
    wb.active.append(["item", "qty"])
    wb.active.append(["pen", 4])
    xbuf = io.BytesIO()
    wb.save(xbuf)
    f = prepare("stock.xlsx", "", xbuf.getvalue())
    assert "Sheet" in f.text and "pen" in f.text and "max 4" in f.text

    f = prepare("data.json", "application/json", json.dumps([{"a": 1, "b": "x"}, {"a": 5, "b": "y"}]).encode())
    assert "2 items" in f.text and "Complete content:" in f.text
    assert json.loads(f.text.split("Complete content:\n", 1)[1]) == [{"a": 1, "b": "x"}, {"a": 5, "b": "y"}]

    dbuf = io.BytesIO()
    with zipfile.ZipFile(dbuf, "w") as z:
        z.writestr("word/document.xml", '<w:document><w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t xml:space="preserve"> SASI</w:t></w:r></w:p></w:document>')
    assert "Hello SASI" in prepare("note.docx", "", dbuf.getvalue()).text

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("inner/a.csv", "x,y\n1,2\n")
        z.writestr("inner/b.parquet", buf.getvalue())
    f = prepare("bundle.zip", "", zbuf.getvalue())
    assert "inner/a.csv" in f.text and "Vizag" in f.text


def test_cannot_delete_a_running_run():
    with make_client(simulated_delay=0.3) as client:
        run = client.post("/api/runs", json={"request": "Long task", "require_plan_approval": True}).json()
        assert client.delete(f"/api/runs/{run['id']}").status_code == 409
        client.post(f"/api/runs/{run['id']}/cancel")


def test_bigquery_file_chunks_round_trip(monkeypatch):
    """Large files are split into chunks for BigQuery and joined back in order; delete clears every table."""
    import base64 as b64
    import datetime

    from app.files import StoredFile
    from app.store import bigquery as bqmod

    class FakeDB:
        def __init__(self):
            self.tables = {bqmod.CHECKPOINTS: [], bqmod.OUTPUTS: [], bqmod.FILES: []}

        def ensure(self):
            pass

        def table(self, name):
            return name

        def param(self, name, value):
            return value

        def load_rows(self, name, rows):
            self.tables[name].extend(rows)

        def query(self, sql, params=None):
            run_id = params[0]
            if sql.startswith("DELETE"):
                name = sql.split("`")[1]
                self.tables[name] = [r for r in self.tables[name] if r["run_id"] != run_id]
                return []
            rows = [dict(r, data=b64.b64decode(r["data"]), created_at=datetime.datetime.now()) for r in self.tables[bqmod.FILES] if r["run_id"] == run_id]
            return sorted(rows, key=lambda r: (r["file_id"], r["chunk"]))

    monkeypatch.setattr(bqmod, "CHUNK", 10)
    db = FakeDB()
    files = bqmod.BigQueryFileStore(db)
    store = bqmod.BigQueryStore("p", "d", db)
    data = bytes(range(256)) * 2
    f = StoredFile(id="f1", name="v.mp4", mime="video/mp4", kind="video", size=len(data), data=data)

    async def scenario():
        await files.attach("r1", [f])
        assert len(db.tables[bqmod.FILES]) == -(-len(data) // 10)
        files.forget("r1")
        back = await files.get("r1")
        assert back[0].data == data and back[0].name == "v.mp4"
        store.record_output("r1", {"run_id": "r1", "stage": "analysis"})
        assert await store.delete_run("r1") == "removed"
        assert not any(db.tables.values())

    asyncio.run(scenario())


def test_field_is_detected_from_the_request():
    from app.workflow.profiles import detect_profile

    assert detect_profile("Fix the bug in my Python API endpoint") == "software"
    assert detect_profile("Analyse this sales data and compare the trends", file_names=["sales.csv"]) == "research"
    assert detect_profile("Plan a birthday party for 20 people") == "general"
    with make_client() as client:
        run = client.post("/api/runs", json={"request": "Write a FastAPI backend with a database schema", "require_plan_approval": True}).json()
        assert run["profile"] == "software"
        client.post(f"/api/runs/{run['id']}/cancel")


def test_project_eris_mode_gives_every_model_the_guidebook_and_dry_runs_only():
    """With the Eris switch on, every call carries the guidebook, the comparison checks the rules, and the planner can't change the field."""
    import json as _json

    from app.workflow import eris

    assert "A10G" in eris.GUIDEBOOK and "1.5 hours" in eris.GUIDEBOOK and "Passing the Determinism Check" in eris.GUIDEBOOK
    assert "DRY RUNS ONLY" in eris.RULES and eris.GUIDEBOOK in eris.RULES

    with make_client() as client:
        registry = client.app.state.registry
        seen: list[ModelCall] = []
        for adapter in registry.adapters.values():
            original = adapter.generate

            async def spy(call: ModelCall, original=original) -> ModelResult:
                seen.append(call)
                result = await original(call)
                if call.kind == "plan":  # a planner that tries to re-label the job must not switch Eris off
                    data = _json.loads(result.text)
                    data["field"] = "software"
                    result.text = _json.dumps(data)
                return result

            adapter.generate = spy

        up = client.post("/api/files", content=b"id,feature,target\n1,0.5,1\n2,0.1,0\n",
                         headers={"X-File-Name": "train.csv", "Content-Type": "text/csv"}).json()
        run = client.post("/api/runs", json={"request": "Build a solution for this tabular Eris challenge", "eris": True, "mode": "deep",
                                             "require_plan_approval": False, "file_ids": [up["id"]]}).json()
        assert run["eris"] is True and run["profile"] == "eris"
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")
        assert run["profile"] == "eris", "the planner cannot switch Eris mode off"

        assert seen and all(c.rules == eris.RULES for c in seen), "every model call carries the guidebook"
        kinds = {c.kind for c in seen}
        assert {"analysis", "cross_check", "plan", "plan_review", "task", "review", "integrate", "final_eval"} <= kinds, kinds
        assert all("## Eris rule check" in c.prompt for c in seen if c.kind == "cross_check")
        assert all("Eris rule" in c.prompt for c in seen if c.kind == "review")
        assert all("exactly ONE complete solution.py" in c.prompt and "## Dry run" in c.prompt for c in seen if c.kind == "integrate")
        assert all(c.max_output_tokens > 4000 for c in seen if c.kind in ("task", "integrate")), "room for a whole script"
        assert client.get("/api/runs").json()[0]["eris"] is True

        seen.clear()
        normal = client.post("/api/runs", json={"request": "Plan a launch", "require_plan_approval": False}).json()
        assert normal["eris"] is False and normal["profile"] != "eris"
        wait_for(client, normal["id"], {"completed", "failed"})
        assert seen and not any(c.rules for c in seen), "normal runs are unchanged"


def test_scores_are_read_from_a_cross_check():
    from app.workflow.prompts import parse_scores

    text = 'Table {not json}\n## Scores\n```json\n{"scores": {"Claude (you)": 9, "glm": 11, "Gemini": "x", "DeepSeek": 7, "Other": 5}, "best": "Claude"}\n```'
    assert parse_scores(text, ["claude", "glm", "gemini", "deepseek"]) == {"claude": 9.0, "glm": 10.0, "deepseek": 7.0}
    assert parse_scores("no scores here", ["claude"]) == {}


def test_models_build_one_answer_together_test_it_and_fix_it():
    """They score each other's approaches, build one answer on the best, every other model tests it, and it is fixed once."""
    with make_client() as client:
        registry = client.app.state.registry
        seen: list[ModelCall] = []
        for adapter in registry.adapters.values():
            original = adapter.generate

            async def spy(call: ModelCall, original=original) -> ModelResult:
                seen.append(call)
                result = await original(call)
                if call.kind == "final_eval" and call.hints.get("round") == 0:
                    import json as _json
                    data = _json.loads(result.text)
                    data["must_fix"], data["score"] = ["Handle an empty input file"], 5
                    result.text = _json.dumps(data)
                return result

            adapter.generate = spy

        run = client.post("/api/runs", json={"request": "Analyse churn and recommend one plan", "require_plan_approval": False, "compare_all": True}).json()
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")

        c = run["consensus"]
        assert c["best"] == "claude" and c["average"] == {"claude": 8.0, "gemini": 7.0, "glm": 6.0, "deepseek": 5.0}
        assert all(cc["scores"] for cc in run["cross_checks"])
        assert all("<team_decision>" in x.prompt and "Claude's approach as the initial candidate" in x.prompt
                   for x in seen if x.kind in ("plan", "task", "integrate") and x.hints.get("task_title") != "Final fixes")

        final = run["final"]
        assert final["best_approach"] == "claude"
        assert final["fixes"] == 1, "the integrator fixed what the other models found"
        fix_calls = [x for x in seen if x.hints.get("task_title") == "Final fixes"]
        assert len(fix_calls) == 1 and "Handle an empty input file" in fix_calls[0].prompt
        judges = {j["judge"] for j in final["judges"]}
        assert final["integrator"] in ("claude", "gemini"), "a frontier model writes the final result"
        assert final["integrator"] not in judges and len(judges) == 3, "every other model tested the result"
        assert len(final["judges"]) == 6 and {j["round"] for j in final["judges"]} == {0, 1}
        assert final["score"] == 8.0, "the score is from the re-test after the fixes"
        assert not any(g.startswith("Still to fix") for g in final["gaps"])
        assert client.get("/api/runs").json()[0]["score"] == 8.0


def test_keys_can_always_be_removed_or_replaced(monkeypatch):
    """Removing stops using a key at once, even if the stored copy can't be deleted; replacing needs no removal first."""
    with make_client(allow_simulated=False) as client:
        registry = client.app.state.registry

        async def fake_test(provider, key):
            return "model-x"

        monkeypatch.setattr(registry, "test_key", fake_test)
        for p in ("claude", "gemini"):
            assert client.put(f"/api/keys/{p}", json={"key": f"sk-{p}-old-1234567890"}).status_code == 200
        r = client.put("/api/keys/claude", json={"key": "sk-claude-new-1234567890NEW1"})
        assert r.status_code == 200 and r.json()["hint"] == "…NEW1", "replacing works without removing first"

        async def broken_delete(provider):
            raise PermissionError("403 denied")

        monkeypatch.setattr(client.app.state.key_store, "delete", broken_delete)
        r = client.delete("/api/keys/claude")
        assert r.status_code == 200 and r.json()["saved"] is False and "PermissionError" in r.json()["warning"]
        keys = {k["provider"]: k["configured"] for k in client.get("/api/keys").json()}
        assert keys == {"claude": False, "gemini": True}, "the app stopped using it anyway"

        monkeypatch.undo()
        monkeypatch.setattr(registry, "test_key", fake_test)
        r = client.delete("/api/keys")
        assert r.status_code == 200 and r.json()["saved"] is True
        assert not any(k["configured"] for k in client.get("/api/keys").json())
        assert client.get("/api/providers").json()["needs_keys"] is True


def test_secret_manager_remove_writes_a_marker_and_destroys_old_versions():
    from types import SimpleNamespace

    from app.keys import REMOVED, SecretManagerKeyStore

    class FakeClient:
        def __init__(self):
            self.versions: dict[str, tuple[str, str]] = {}  # name -> (state, data)

        def add_secret_version(self, parent, payload):
            name = f"{parent}/versions/{len(self.versions) + 1}"
            self.versions[name] = ("ENABLED", payload["data"].decode())
            return SimpleNamespace(name=name)

        def list_secret_versions(self, parent, filter):
            return [SimpleNamespace(name=n) for n, (st, _) in self.versions.items() if n.startswith(parent) and st == "ENABLED"]

        def destroy_secret_version(self, name):
            self.versions[name] = ("DESTROYED", "")

        def access_secret_version(self, name):
            parent = name.rsplit("/versions/", 1)[0]
            live = [d for n, (st, d) in self.versions.items() if n.startswith(parent) and st == "ENABLED"]
            return SimpleNamespace(payload=SimpleNamespace(data=live[-1].encode()))

    store = SecretManagerKeyStore.__new__(SecretManagerKeyStore)
    store._client, store._project = FakeClient(), "p"
    store._write("claude", "sk-old-key-123456")
    store._write("claude", "sk-new-key-123456")
    assert store._read("claude") == "sk-new-key-123456"
    assert sum(st == "ENABLED" for st, _ in store._client.versions.values()) == 1, "old key destroyed"
    store._remove("claude")
    assert store._read("claude") == ""
    enabled = [d for st, d in store._client.versions.values() if st == "ENABLED"]
    assert enabled == [REMOVED], "only the removal marker is left"


def test_runs_go_ahead_with_whichever_keys_work():
    """With two failing keys, the run finishes on the one that works; failing keys are skipped next time and named."""
    from app.gateway.simulated import SimulatedAdapter

    class BadKey:
        simulated = False

        def __init__(self, provider):
            self.provider = provider
            self.display_name = provider

        async def list_models(self):
            raise ProviderError(self.provider, "HTTP 401: invalid api key")

        async def resolve_model(self):
            return "x"

        async def generate(self, call):
            raise ProviderError(self.provider, "HTTP 401: invalid api key")

    with make_client(allow_simulated=False) as client:
        registry = client.app.state.registry
        good = SimulatedAdapter("claude", 0.0)
        good.simulated = False
        registry.adapters = {"claude": good, "gemini": BadKey("gemini"), "glm": BadKey("glm")}
        registry.hints = {"claude": "…AAAA", "gemini": "…CCCC"}
        registry.status = {p: {"ok": True} for p in registry.adapters}

        run = client.post("/api/runs", json={"request": "Plan a product launch", "require_plan_approval": False}).json()
        run = wait_for(client, run["id"], {"completed", "failed"})
        assert run["status"] == "completed", run.get("error")
        assert set(run["skipped"]) == {"gemini", "glm"}
        assert any("Only one model was available" in x for x in run["final"]["limitations"])
        provs = {p["provider"]: p for p in client.get("/api/providers").json()["providers"]}
        assert provs["gemini"]["ok"] is False and "401" in provs["gemini"]["error"], "the failing key shows on Connections"

        calls_before = run["usage"]["by_provider"].keys()
        assert set(calls_before) <= {"claude", "gemini", "glm"}
        second = client.post("/api/runs", json={"request": "Plan a second launch", "require_plan_approval": False}).json()
        second = wait_for(client, second["id"], {"completed", "failed"})
        assert second["status"] == "completed"
        assert set(second["usage"]["by_provider"]) == {"claude"}, "failing keys are not even tried on the next run"
        assert "no key" not in " ".join(second["skipped"].values())
        assert any(e["type"] == "team" and e["message"].startswith("Team for this run: Claude.") for e in second["events"])

        registry.adapters = {"gemini": BadKey("gemini")}
        registry.status = {"gemini": {"ok": False, "error": "HTTP 401: invalid api key"}}
        r = client.post("/api/runs", json={"request": "Anything at all"})
        assert r.status_code == 400 and "None of your API keys is working" in r.text
