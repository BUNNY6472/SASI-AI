"""Google Cloud Vertex AI: the request each model gets, and how a model falls back between Vertex and its API key."""
import asyncio
import json
import time

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.gateway.base import ModelCall, ModelResult, ProviderError
from app.gateway.registry import ModelRegistry
from app.gateway.routes import RoutedAdapter, Route
from app.gateway.simulated import SimulatedAdapter
from app.main import create_app


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the requests sent to Vertex AI


def test_claude_on_vertex_finds_a_usable_model_and_sends_no_fallback_beta():
    import anthropic

    from app.gateway.vertex import VertexClaudeAdapter

    seen = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content or b"{}")
        seen.append((req.url.path, body, dict(req.headers)))
        if req.url.path.endswith("count-tokens:rawPredict"):
            if body["model"] == "claude-opus-5-5":
                return httpx2.Response(404, json={"error": {"type": "not_found_error", "message": "not enabled"}})
            return httpx2.Response(200, json={"input_tokens": 3})
        if "/claude-opus-5-5:" in req.url.path:
            return httpx2.Response(404, json={"error": {"type": "not_found_error", "message": "not enabled"}})
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": "Hello from Vertex"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 5}})

    adapter = VertexClaudeAdapter("sasi-511906", "global", ["claude-opus-5-5", "claude-opus-5"])
    adapter._client = anthropic.AsyncAnthropicVertex(
        region="global", project_id="sasi-511906", access_token="token",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))

    assert run(adapter.resolve_model()) == "claude-opus-5", "the first model this project can call is used"
    result = run(adapter.generate(ModelCall(system="sys", prompt="hi", kind="task", rules="RULES")))
    assert result.text == "Hello from Vertex" and result.input_tokens == 15

    path, body, headers = seen[-1]
    assert path == "/v1/projects/sasi-511906/locations/global/publishers/anthropic/models/claude-opus-5:rawPredict"
    assert headers["authorization"] == "Bearer token"
    assert body["output_config"] == {"effort": "medium"}
    assert "fallbacks" not in body and "server-side-fallback" not in headers.get("anthropic-beta", "")
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}, "the shared rules are still cached"


def test_claude_on_vertex_says_how_to_enable_it():
    import anthropic

    from app.gateway.vertex import VertexClaudeAdapter

    def handler(req):
        return httpx2.Response(404, json={"error": {"type": "not_found_error", "message": "Publisher model not found"}})

    adapter = VertexClaudeAdapter("p", "global", ["claude-opus-5-5"])
    adapter._client = anthropic.AsyncAnthropicVertex(region="global", project_id="p", access_token="t",
                                                     http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    with pytest.raises(ProviderError) as e:
        run(adapter.list_models())
    assert "Model Garden" in str(e.value) and "404" in str(e.value)


def test_openai_on_vertex_uses_the_openai_compatible_endpoint(monkeypatch):
    import google.auth
    import openai

    from app.gateway import vertex

    class Creds:
        valid, token = True, "google-token"

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (Creds(), "sasi-511906"))
    seen = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        seen.append((str(req.url), body, req.headers.get("authorization")))
        if body.get("stream"):
            chunks = [
                {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                 "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                for piece in ("Open", "-weight")
            ] + [{"id": "c", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx2.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx2.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}})

    real = openai.AsyncOpenAI
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: real(
        **kw, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))))

    adapter = vertex.VertexOpenAIAdapter("", "us-central1", ["openai/gpt-oss-120b-maas"])
    assert run(adapter.resolve_model()) == "openai/gpt-oss-120b-maas"
    url, body, auth = seen[0]
    assert url == ("https://us-central1-aiplatform.googleapis.com/v1/projects/sasi-511906/locations/us-central1"
                   "/endpoints/openapi/chat/completions")
    assert auth == "Bearer google-token", "signed with the service account's token, not an API key"

    streamed = []

    async def on_text(piece):
        streamed.append(piece)

    from app.files import StoredFile

    image = StoredFile(id="f1", name="chart.png", mime="image/png", size=10, kind="image", data=b"0123456789")
    call = ModelCall(system="sys", prompt="Describe it", kind="task", on_text=on_text, attachments=[image])
    result = run(adapter.generate(call))
    assert result.text == "Open-weight" and streamed == ["Open", "-weight"]
    assert result.output_tokens > 0, "usage is estimated when the stream does not report it"
    _, body, _ = seen[-1]
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert "chart.png" in body["messages"][1]["content"], "a text-only model is told about files it cannot open"


def test_gemini_on_vertex_counts_tokens_to_check_and_sends_files_inline():
    import google.auth.credentials
    from google import genai
    from google.genai import types

    from app.gateway.vertex import VertexGeminiAdapter

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, json.loads(req.content or b"{}")))
        if req.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 1})
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": "Gemini on Vertex"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}, "modelVersion": "gemini-4-argon"})

    class Creds(google.auth.credentials.Credentials):
        def __init__(self):
            super().__init__()
            self.token = "t"
            self.expiry = None

        def refresh(self, request):
            self.token = "t"

        @property
        def valid(self):
            return True

    adapter = VertexGeminiAdapter("sasi-511906", "global", ["gemini-4-argon"])
    adapter._client = genai.Client(
        vertexai=True, project="sasi-511906", location="global", credentials=Creds(),
        http_options=types.HttpOptions(httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))

    assert run(adapter.resolve_model()) == "gemini-4-argon"
    assert "publishers/google/models/gemini-4-argon:countTokens" in seen[0][0]
    assert "/locations/global/" in seen[0][0]

    from app.files import StoredFile

    big = StoredFile(id="v", name="talk.mp4", mime="video/mp4", size=20 * 1024 * 1024, kind="video", data=b"x")
    small = StoredFile(id="i", name="a.png", mime="image/png", size=1000, kind="image", data=b"img")
    result = run(adapter.generate(ModelCall(system="sys", prompt="Look", kind="task", attachments=[small, big])))
    assert result.text == "Gemini on Vertex" and result.input_tokens == 7
    parts = seen[-1][1]["contents"][0]["parts"]
    assert any("inlineData" in p or "inline_data" in p for p in parts), "the small file goes inline"
    assert "talk.mp4 is too large" in parts[-1]["text"], "Vertex has no Files API, so a large file gets a note"


# ---------------------------------------------------------------- choosing between Vertex and the API key


class Fake:
    """A model route that answers, or fails with a given error."""

    simulated = False

    def __init__(self, name, error=None, write_first=False):
        self.provider, self.display_name, self.name = "claude", "Claude", name
        self.error, self.write_first, self.calls = error, write_first, 0

    async def list_models(self):
        if self.error:
            raise ProviderError("claude", self.error)
        return [self.name]

    async def resolve_model(self):
        return self.name

    async def generate(self, call):
        self.calls += 1
        if self.write_first and call.on_text:
            await call.on_text("partial ")
        if self.error:
            raise ProviderError("claude", self.error)
        if call.on_text:
            await call.on_text(f"answer from {self.name}")
        return ModelResult(provider="claude", model=self.name, text=f"answer from {self.name}")


def test_a_failing_vertex_route_hands_the_call_to_the_api_key():
    vertex = Fake("vertex-model", error="HTTP 403: Permission denied on resource project")
    key = Fake("key-model")
    adapter = RoutedAdapter("claude", "Claude", [Route("vertex", vertex), Route("key", key)])
    written = []

    async def on_text(piece):
        written.append(piece)

    result = run(adapter.generate(ModelCall(system="s", prompt="p", kind="task", on_text=on_text)))
    assert result.text == "answer from key-model" and result.via == "key"
    assert written == ["answer from key-model"]
    assert adapter.routes[0].ok is False and "403" in adapter.routes[0].error

    run(adapter.generate(ModelCall(system="s", prompt="p", kind="task")))
    assert vertex.calls == 1, "a route that failed for a lasting reason is skipped until the next check"
    assert adapter.status()["via"] == "key"


def test_no_hand_over_once_the_model_has_started_writing():
    vertex = Fake("vertex-model", error="HTTP 500: stream broke", write_first=True)
    key = Fake("key-model")
    adapter = RoutedAdapter("claude", "Claude", [Route("vertex", vertex), Route("key", key)])

    async def on_text(piece):
        pass

    with pytest.raises(ProviderError):
        run(adapter.generate(ModelCall(system="s", prompt="p", kind="task", on_text=on_text)))
    assert key.calls == 0, "the controller retries instead, so the screen never shows two half answers"
    assert adapter.routes[0].ok is not False, "a passing error does not switch routes for later calls"


def test_check_reports_every_route():
    adapter = RoutedAdapter("claude", "Claude", [Route("vertex", Fake("v", error="HTTP 404: not enabled in Model Garden")),
                                                 Route("key", Fake("k"))])
    st = run(adapter.check())
    assert st["ok"] is True and st["via"] == "key" and st["model"] == "k"
    assert [r["ok"] for r in st["routes"]] == [False, True]
    assert "Model Garden" in st["routes"][0]["error"]

    both_down = RoutedAdapter("claude", "Claude", [Route("vertex", Fake("v", error="HTTP 403: denied")),
                                                   Route("key", Fake("k", error="HTTP 401: invalid x-api-key"))])
    st = run(both_down.check())
    assert st["ok"] is False and "Google Cloud: " in st["error"] and "API key: " in st["error"]


def vertex_app(monkeypatch, **overrides) -> TestClient:
    """The app with Vertex AI on, where each Vertex route is a stand-in model."""

    def fake_vertex(self, provider):
        a = SimulatedAdapter(provider, 0.0)
        a.simulated = False
        return a

    monkeypatch.setattr(ModelRegistry, "_make_vertex", fake_vertex)
    settings = Settings(_env_file=None, static_dir="/nonexistent", vertex_ai=True, **overrides)
    return TestClient(create_app(settings))


def test_registry_orders_routes_per_model(monkeypatch):
    monkeypatch.setattr(ModelRegistry, "_make_vertex", lambda self, p: Fake(f"vertex-{p}"))
    registry = ModelRegistry(Settings(_env_file=None, vertex_ai=True))
    registry.apply_keys({"claude": "sk-ant-1234567890", "chatgpt": "sk-openai-1234567890"})
    order = {p: [r.via for r in a.routes] for p, a in registry.adapters.items()}
    assert order == {"claude": ["vertex", "key"], "chatgpt": ["key", "vertex"], "gemini": ["vertex"]}, \
        "Claude and Gemini go through Google Cloud first; ChatGPT uses the OpenAI key first for the newest GPT model"
    registry.remove("claude")
    assert [r.via for r in registry.adapters["claude"].routes] == ["vertex"], "removing a key keeps the Google Cloud route"


def test_with_vertex_on_runs_need_no_keys(monkeypatch):
    with vertex_app(monkeypatch, allow_simulated=False, simulated_delay=0.0) as client:
        providers = client.get("/api/providers?refresh=true").json()
        assert providers["needs_keys"] is False and providers["vertex"]["on"] is True
        for p in providers["providers"]:
            assert p["connected"] and p["has_key"] is False and p["via"] == "vertex"
        assert not any(k["configured"] for k in client.get("/api/keys").json()), "no API key was needed"

        r = client.post("/api/runs", json={"request": "Plan a product launch", "require_plan_approval": False}).json()
        end = time.time() + 15
        while time.time() < end:
            r = client.get(f"/api/runs/{r['id']}").json()
            if r["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert r["status"] == "completed", r.get("error")
        team = next(e["message"] for e in r["events"] if e["type"] == "team")
        assert "Claude (Google Cloud)" in team and "Gemini (Google Cloud)" in team

        assert client.delete("/api/keys").status_code == 200
        assert client.get("/api/providers").json()["needs_keys"] is False, "removing keys leaves Vertex working"
