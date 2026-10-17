"""Date-sensitive routing checks; all provider methods are local stand-ins."""
import asyncio

import pytest

from app.config import Settings
from app.gateway.registry import ModelRegistry


OPEN_MODELS = "glm,deepseek,qwen,kimi,minimax,gptoss"
RETIRING = {"qwen", "kimi", "minimax"}
SURVIVING = {"gemini", "glm", "deepseek", "gptoss"}


class Probe:
    simulated = False

    def __init__(self, provider):
        self.provider = self.display_name = provider
        self.probes = 0

    async def list_models(self):
        self.probes += 1
        return [self.provider]

    async def resolve_model(self):
        return self.provider


@pytest.mark.parametrize("today,retired", [("2026-10-20", False), ("2026-10-21", True)])
def test_retirement_blocks_construction_probes_and_selection_but_keeps_cards(monkeypatch, today, retired):
    constructed = {}
    monkeypatch.setattr("app.gateway.registry._utc_date", lambda: today)

    def make_vertex(self, provider):
        constructed[provider] = Probe(provider)
        return constructed[provider]

    monkeypatch.setattr(ModelRegistry, "_make_vertex", make_vertex)
    registry = ModelRegistry(Settings(_env_file=None, vertex_ai=True, open_models=OPEN_MODELS, team_size=8))
    expected = SURVIVING if retired else SURVIVING | RETIRING
    assert set(constructed) == expected
    asyncio.run(registry.refresh())
    asyncio.run(registry.recheck_failed())
    assert set(registry.eligible()) == set(registry.team("general")) == expected
    cards = {card["provider"]: card for card in registry.describe()}
    for provider in RETIRING:
        assert cards[provider]["retirement_date"] == "2026-10-21"
        if retired:
            assert cards[provider]["connected"] is False and cards[provider]["ok"] is False
            assert "2026-10-21 (UTC)" in cards[provider]["error"]
            assert cards[provider]["routes"] == []
            assert "2026-10-21" in registry.unavailable()[provider]
        else:
            assert cards[provider]["connected"] and cards[provider]["ok"]
            assert constructed[provider].probes == 1


def test_running_registry_drops_retired_routes_at_utc_rollover(monkeypatch):
    today = ["2026-10-20"]
    constructed = {}
    monkeypatch.setattr("app.gateway.registry._utc_date", lambda: today[0])

    def make_vertex(self, provider):
        constructed[provider] = Probe(provider)
        return constructed[provider]

    monkeypatch.setattr(ModelRegistry, "_make_vertex", make_vertex)
    registry = ModelRegistry(Settings(_env_file=None, vertex_ai=True, open_models=OPEN_MODELS))
    asyncio.run(registry.refresh())
    before = {p: a.probes for p, a in constructed.items()}
    today[0] = "2026-10-21"
    for provider in RETIRING:
        registry.status[provider]["ok"] = False
    asyncio.run(registry.recheck_failed())
    asyncio.run(registry.refresh())
    assert set(registry.adapters) == set(registry.vertex) == set(registry.eligible()) == SURVIVING
    assert all(constructed[p].probes == before[p] for p in RETIRING)
    assert all(constructed[p].probes > before[p] for p in SURVIVING)
    assert len(registry.describe()) == 8, "retired cards remain visible alongside Claude and the working routes"
    registry.apply_keys({})
    assert not RETIRING.intersection(registry.adapters), "key changes cannot restore retired routes"


def test_vertex_factory_respects_qwen_fallback_output_limit_and_global_gptoss_preference(monkeypatch):
    monkeypatch.setattr("app.gateway.registry._utc_date", lambda: "2026-10-20")
    registry = ModelRegistry(Settings(_env_file=None, vertex_ai=False))
    qwen = registry._make_vertex("qwen")
    gptoss = registry._make_vertex("gptoss")
    assert qwen._max_output == 16_384
    assert gptoss._candidates[0] == ("openai/gpt-oss-120b-maas", "global")
