"""Trial-credit routing policy; no real provider or billing calls."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.gateway.base import ModelCall, ModelResult, ProviderError
from app.gateway.registry import ModelRegistry
from app.main import create_app


class GeminiProbe:
    provider = display_name = "gemini"
    simulated = False

    def __init__(self):
        self.probes = self.calls = 0

    async def list_models(self):
        self.probes += 1
        return ["vertex-gemini"]

    async def resolve_model(self):
        return "vertex-gemini"

    async def generate(self, call):
        self.calls += 1
        return ModelResult(provider="gemini", model="vertex-gemini", text="Vertex response")


def install_factories(monkeypatch):
    made = []
    probe = GeminiProbe()

    def vertex(self, provider):
        made.append(provider)
        assert provider == "gemini", "trial mode must not even construct partner adapters"
        return probe

    def key(self, provider, key):
        pytest.fail("trial mode must not construct or test any API-key adapter")

    monkeypatch.setattr(ModelRegistry, "_make_vertex", vertex)
    monkeypatch.setattr(ModelRegistry, "_make", key)
    return made, probe


@pytest.mark.parametrize("today", ["2026-10-20", "2026-10-21"])
def test_trial_policy_allows_only_vertex_gemini_with_existing_keys_and_retired_models(monkeypatch, today):
    monkeypatch.setattr("app.gateway.registry._utc_date", lambda: today)
    made, probe = install_factories(monkeypatch)
    settings = Settings(_env_file=None, trial_credits_only=True, vertex_ai=True, vertex_claude_enabled=True,
                        open_models="glm,deepseek,qwen,kimi,minimax,gptoss", allow_simulated=True)
    registry = ModelRegistry(settings)
    stored = {"claude": "existing-claude-key", "gemini": "existing-gemini-key"}
    registry.apply_keys(stored)
    assert registry.keys == stored and made == ["gemini"]
    asyncio.run(registry.refresh())
    asyncio.run(registry.recheck_failed())
    assert registry.eligible() == registry.team("research") == ["gemini"]
    assert registry.live_count == 1 and probe.probes == 1
    adapter = registry.adapters["gemini"]
    assert [route.via for route in adapter.routes] == ["vertex"]
    answer = asyncio.run(adapter.generate(ModelCall(system="s", prompt="p", kind="task")))
    assert answer.via == "vertex" and probe.calls == 1
    cards = {card["provider"]: card for card in registry.describe()}
    assert len(cards) == 8
    for provider, card in cards.items():
        assert card["key_option"] is False
        assert card["policy_blocked"] is (provider != "gemini")
        if provider != "gemini":
            assert card["connected"] is False and card["ok"] is False and card["routes"] == []
            assert "trial-credit-only policy" in card["error"]
            assert "no key" not in registry.unavailable()[provider]
    assert cards["claude"]["has_key"] and cards["gemini"]["has_key"]
    if today == "2026-10-21":
        assert "2026-10-21" in cards["qwen"]["error"]
    for provider in stored:
        with pytest.raises(ProviderError, match="trial-credit-only policy"):
            asyncio.run(registry.test_key(provider, "new-unused-key"))
        registry.set_adapter(provider, "new-unused-key", "unused-model")
        assert set(registry.adapters) == {"gemini"}
        registry.remove(provider)
        assert set(registry.adapters) == {"gemini"}
        assert [route.via for route in registry.adapters["gemini"].routes] == ["vertex"]


def test_trial_policy_with_vertex_off_fails_closed_and_never_uses_keys_or_simulation(monkeypatch):
    made, probe = install_factories(monkeypatch)
    registry = ModelRegistry(Settings(_env_file=None, trial_credits_only=True, vertex_ai=False, allow_simulated=True))
    registry.apply_keys({"claude": "stored-claude", "gemini": "stored-gemini"})
    asyncio.run(registry.refresh())
    assert not made and not probe.probes and not registry.adapters and not registry.eligible()
    assert all(card["policy_blocked"] and card["ok"] is False for card in registry.describe())


def test_trial_policy_does_not_clear_failed_vertex_health(monkeypatch):
    install_factories(monkeypatch)
    registry = ModelRegistry(Settings(_env_file=None, trial_credits_only=True, vertex_ai=True))
    asyncio.run(registry.refresh())
    registry.mark_failed("gemini", "quota exhausted")
    assert registry.eligible() == []
    assert next(card for card in registry.describe() if card["provider"] == "gemini")["ok"] is False


def test_trial_policy_is_exposed_by_api_and_key_save_cannot_enable_routes(monkeypatch):
    made, probe = install_factories(monkeypatch)
    settings = Settings(_env_file=None, trial_credits_only=True, vertex_ai=True, static_dir="/nonexistent",
                        anthropic_api_key="existing-claude-key", gemini_api_key="existing-gemini-key")
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/providers?refresh=true").json()
        assert response["trial_credits_only"] is True and response["needs_keys"] is False
        assert response["vertex"]["on"] is True and response["demo_mode"] is False
        for provider in ("claude", "gemini"):
            saved = client.put(f"/api/keys/{provider}", json={"key": "new-unused-key"})
            assert saved.status_code == 403 and "trial-credit-only policy" in saved.json()["detail"]
        assert all(row["configured"] for row in client.get("/api/keys").json())
        assert set(app.state.registry.adapters) == {"gemini"} and made == ["gemini"]
        assert probe.calls == 0


def test_trial_policy_without_vertex_reports_connection_policy_instead_of_missing_keys(monkeypatch):
    made, probe = install_factories(monkeypatch)
    app = create_app(Settings(_env_file=None, trial_credits_only=True, vertex_ai=False,
                              allow_simulated=True, static_dir="/nonexistent"))
    with TestClient(app) as client:
        providers = client.get("/api/providers").json()
        assert providers["needs_keys"] is False and providers["demo_mode"] is False
        result = client.post("/api/runs", json={"request": "A simple request"})
        assert result.status_code == 400
        assert "Trial-credit-only policy" in result.json()["detail"]
        assert "Vertex Gemini" in result.json()["detail"]
        assert not made and not probe.calls
