"""Owner Claude opt-in is direct API only, isolated from legacy keys; all calls are fake."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound

from app.config import Settings
from app.gateway.base import ModelCall, ModelResult, ProviderError
from app.gateway.registry import ModelRegistry
from app.gateway.routes import Route, RoutedAdapter
from app.keys import OWNER_CLAUDE_SECRET, SECRET_NAMES, EnvKeyStore, build_key_store
from app.main import create_app
from app.models import Run
from app.store.memory import MemoryStore
from app.workflow.controller import BudgetExceeded, Controller
from app.workflow.router import PerformanceHistory


class FakeAdapter:
    simulated = False

    def __init__(self, provider, via, key=""):
        self.provider = self.display_name = provider
        self.via, self.key = via, key
        self.probes = self.calls = 0
        self._model = f"{via}-{provider}"

    async def list_models(self):
        self.probes += 1
        return [self._model]

    async def resolve_model(self):
        return self._model

    async def generate(self, call):
        self.calls += 1
        return ModelResult(provider=self.provider, model=self._model, text="A fake answer")


def install_adapters(monkeypatch):
    made = []

    def make(provider, via, key=""):
        adapter = FakeAdapter(provider, via, key)
        made.append(adapter)
        return adapter

    def forbidden(*args, **kwargs):
        pytest.fail("Blocked API or Vertex partner adapter must never be constructed")

    monkeypatch.setattr("app.gateway.registry.ClaudeAdapter", lambda key, model: make("claude", "key", key))
    monkeypatch.setattr("app.gateway.registry.GeminiAdapter", forbidden)
    monkeypatch.setattr("app.gateway.vertex.VertexGeminiAdapter", lambda *args: make("gemini", "vertex"))
    monkeypatch.setattr("app.gateway.vertex.VertexClaudeAdapter", forbidden)
    monkeypatch.setattr("app.gateway.vertex.VertexOpenModelAdapter", forbidden)
    return made


def owner_settings(**overrides):
    values = dict(_env_file=None, trial_credits_only=True, allow_claude_api_key=True,
                  vertex_ai=True, vertex_claude_enabled=True, static_dir="/nonexistent",
                  trial_credit_expires_at="2026-11-18T00:00:00Z")
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def before_trial_expiry(monkeypatch):
    monkeypatch.setattr("app.gateway.registry._utc_now", lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))


def test_owner_opt_in_allows_only_direct_claude_and_vertex_gemini(monkeypatch):
    made = install_adapters(monkeypatch)
    registry = ModelRegistry(owner_settings())
    registry.apply_keys({"claude": "fresh-owner-key", "gemini": "legacy-gemini-key"})
    asyncio.run(registry.refresh())
    assert registry.eligible() == ["claude", "gemini"]
    assert [(a.provider, a.via) for a in made] == [("gemini", "vertex"), ("claude", "key")]
    assert set(registry.vertex) == {"gemini"}
    cards = {card["provider"]: card for card in registry.describe()}
    assert cards["claude"]["key_option"] and not cards["claude"]["policy_blocked"]
    assert cards["claude"]["via"] == "key" and not cards["gemini"]["key_option"]
    assert all(card["policy_blocked"] and not card["connected"] for p, card in cards.items()
               if p not in {"claude", "gemini"})
    answer = asyncio.run(registry.adapters["claude"].generate(ModelCall(system="s", prompt="p", kind="task")))
    assert answer.via == "key" and made[-1].calls == 1
    assert asyncio.run(registry.test_key("claude", "replacement-owner-key")) == "key-claude"
    with pytest.raises(ProviderError, match="trial-credit-only policy"):
        asyncio.run(registry.test_key("gemini", "cannot-use-this-key"))
    with pytest.raises(ProviderError, match="disabled"):
        registry._make_vertex("claude")
    registry.set_adapter("claude", "replacement-owner-key", "key-claude")
    assert [r.via for r in registry.adapters["claude"].routes] == ["key"]
    registry.remove("claude")
    assert registry.eligible() == ["gemini"] and "claude" not in registry.vertex


def test_owner_opt_in_never_reuses_legacy_environment_key(monkeypatch):
    made = install_adapters(monkeypatch)
    settings = owner_settings(anthropic_api_key="legacy-claude-key", gemini_api_key="legacy-gemini-key")
    store = EnvKeyStore(settings)
    registry = ModelRegistry(settings)
    registry.apply_keys(asyncio.run(store.load()))
    assert "claude" not in registry.keys and all(a.provider != "claude" for a in made)
    assert asyncio.run(store.load()) == {"gemini": "legacy-gemini-key"}
    asyncio.run(store.save("claude", "fresh-owner-key"))
    registry.apply_keys(asyncio.run(store.load()))
    assert registry.keys["claude"] == "fresh-owner-key"
    asyncio.run(store.delete("claude"))
    registry.apply_keys(asyncio.run(store.load()))
    assert "claude" not in registry.keys and settings.anthropic_api_key == "legacy-claude-key"
    supplied = EnvKeyStore(owner_settings(anthropic_api_key="legacy-claude-key",
                                          sasi_owner_anthropic_api_key="new-explicit-owner-key"))
    assert asyncio.run(supplied.load())["claude"] == "new-explicit-owner-key"


def test_owner_api_metadata_save_delete_and_reload_preserve_route_policy(monkeypatch):
    made = install_adapters(monkeypatch)
    app = create_app(owner_settings(anthropic_api_key="legacy-must-not-be-activated"))
    new_key = "fresh-owner-key-for-test"
    with TestClient(app) as client:
        providers = client.get("/api/providers?refresh=true").json()
        assert providers["claude_api_key_allowed"] and providers["trial_credits_only"]
        claude = next(p for p in providers["providers"] if p["provider"] == "claude")
        assert claude["key_option"] and not claude["has_key"] and not claude["connected"]
        saved = client.put("/api/keys/claude", json={"key": new_key})
        assert saved.status_code == 200 and saved.json()["saved"] and saved.json()["via"] == "key"
        assert new_key not in saved.text and "legacy-must-not-be-activated" not in saved.text
        blocked = client.put("/api/keys/gemini", json={"key": "blocked-gemini-key"})
        assert blocked.status_code == 403
        refreshed = client.get("/api/providers?refresh=true").json()
        claude = next(p for p in refreshed["providers"] if p["provider"] == "claude")
        assert claude["ok"] and claude["via"] == "key" and new_key not in str(refreshed)
        removed = client.delete("/api/keys/claude")
        assert removed.status_code == 200 and removed.json()["saved"]
        assert "claude" not in app.state.registry.adapters
        assert all(a.via == ("key" if a.provider == "claude" else "vertex") for a in made)
        assert not any(a.calls for a in made), "checking/saving a key never generates paid answers"


@pytest.mark.parametrize("deadline", ["2026-10-20T00:00:00Z", "invalid"])
def test_expiry_blocks_owner_key_construction_checks_and_api_saves(monkeypatch, deadline):
    made = install_adapters(monkeypatch)
    settings = owner_settings(trial_credit_expires_at=deadline, sasi_owner_anthropic_api_key="fresh-owner-key")
    app = create_app(settings)
    with TestClient(app) as client:
        providers = client.get("/api/providers?refresh=true").json()
        assert not providers["claude_api_key_allowed"] and providers["trial_credit_window_error"]
        assert all(p["policy_blocked"] and not p["key_option"] for p in providers["providers"])
        assert client.put("/api/keys/claude", json={"key": "fresh-owner-key"}).status_code == 403
        assert client.post("/api/runs", json={"request": "A new request"}).status_code == 403
        assert not made


def test_expiry_stops_existing_owner_claude_at_next_dispatch(monkeypatch):
    made = install_adapters(monkeypatch)
    settings = owner_settings()
    registry = ModelRegistry(settings)
    registry.apply_keys({"claude": "fresh-owner-key"})
    controller = Controller(settings, registry, MemoryStore(), PerformanceHistory())
    monkeypatch.setattr("app.gateway.registry._utc_now", lambda: datetime(2026, 11, 18, tzinfo=timezone.utc))
    with pytest.raises(BudgetExceeded, match="Trial credit window ended"):
        asyncio.run(controller._call_inner(Run(request="A request"), "claude",
                                          ModelCall(system="s", prompt="p", kind="task"), None))
    assert not any(a.calls for a in made)
    assert registry.eligible() == []


def test_blocked_cached_routes_cannot_be_checked_or_selected(monkeypatch):
    install_adapters(monkeypatch)
    registry = ModelRegistry(owner_settings())
    disallowed = FakeAdapter("claude", "vertex")
    bypass = RoutedAdapter("claude", "Claude", [Route("vertex", disallowed)])
    _, status = asyncio.run(registry._check("claude", bypass))
    assert status["ok"] is False and disallowed.probes == 0
    registry.vertex["claude"] = bypass.routes[0]
    registry.adapters["claude"] = bypass
    assert "claude" not in registry.eligible() and "claude" not in registry.vertex


def test_owner_key_works_without_vertex_and_opt_in_still_disables_vertex_claude_outside_trial(monkeypatch):
    made = install_adapters(monkeypatch)
    registry = ModelRegistry(owner_settings(vertex_ai=False))
    registry.apply_keys({"claude": "fresh-owner-key", "gemini": "blocked-gemini-key"})
    assert registry.eligible() == ["claude"] and not registry.uses_vertex
    normal = ModelRegistry(owner_settings(trial_credits_only=False, open_models=""))
    normal.apply_keys({"claude": "fresh-owner-key"})
    assert set(normal.vertex) == {"gemini"}
    assert all(a.provider != "claude" or a.via == "key" for a in made)


def test_secret_manager_opt_in_never_reads_writes_or_removes_legacy_claude(monkeypatch):
    class FakeSecrets:
        def __init__(self):
            self.versions = {"projects/p/secrets/ANTHROPIC_API_KEY/versions/1": "legacy-key"}
            self.touched = []

        def access_secret_version(self, name):
            self.touched.append(name)
            parent = name.rsplit("/versions/", 1)[0]
            values = [v for n, v in self.versions.items() if n.startswith(parent + "/versions/")]
            if not values:
                raise NotFound("No version yet")
            return SimpleNamespace(payload=SimpleNamespace(data=values[-1].encode()))

        def add_secret_version(self, parent, payload):
            self.touched.append(parent)
            name = f"{parent}/versions/{len(self.versions) + 1}"
            self.versions[name] = payload["data"].decode()
            return SimpleNamespace(name=name)

        def list_secret_versions(self, parent, filter):
            self.touched.append(parent)
            return [SimpleNamespace(name=n) for n in self.versions if n.startswith(parent + "/versions/")]

        def destroy_secret_version(self, name):
            self.touched.append(name)
            self.versions.pop(name)

    fake = FakeSecrets()
    monkeypatch.setattr("google.cloud.secretmanager.SecretManagerServiceClient", lambda: fake)
    store = build_key_store(owner_settings(key_store="secretmanager", gcp_project="p"))
    assert asyncio.run(store.load()) == {}, "empty dedicated owner secret must not fall back to legacy"
    asyncio.run(store.save("claude", "new-owner-key"))
    assert asyncio.run(store.load()) == {"claude": "new-owner-key"}
    asyncio.run(store.save("claude", "replacement-owner-key"))
    assert asyncio.run(store.load()) == {"claude": "replacement-owner-key"}
    asyncio.run(store.delete("claude"))
    assert asyncio.run(store.load()) == {}
    assert fake.versions["projects/p/secrets/ANTHROPIC_API_KEY/versions/1"] == "legacy-key"
    assert not any("/secrets/ANTHROPIC_API_KEY" in path for path in fake.touched)
    assert any(f"/secrets/{OWNER_CLAUDE_SECRET}" in path for path in fake.touched)
    assert SECRET_NAMES["claude"] == "ANTHROPIC_API_KEY", "the default mapping is not mutated"
