"""Model registry: which models SASI can reach, how (Google Cloud Vertex AI or an API key), and who is on a run's team."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from ..config import Settings
from ..keys import hint
from .anthropic_adapter import ClaudeAdapter
from .base import ProviderAdapter
from .catalog import SEATS, name, rank_open
from .gemini_adapter import GeminiAdapter
from .routes import RoutedAdapter, Route
from .simulated import SimulatedAdapter

log = logging.getLogger("sasi.registry")

FRONTIER = ("claude", "gemini")
KEY_SEATS = ("claude", "gemini")  # models that can also use the provider's own API key
PROVIDERS = KEY_SEATS  # kept for older imports: the models with an API key


class ModelRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapters: dict[str, ProviderAdapter] = {}
        self.hints: dict[str, str] = {}
        self.keys: dict[str, str] = {}
        self.status: dict[str, dict] = {}
        self.checked_at: float = 0
        self.context_cap: dict[str, int] = {}  # learned from "input too long" errors
        # Separated by commas, spaces or "+" (a "+" list fits in a Cloud Run env setting, where commas split variables).
        wanted = re.split(r"[\s,+|/]+", settings.open_models.lower())
        self.open_seats = [s for s in dict.fromkeys(wanted) if s in SEATS and SEATS[s].tier == "open"]
        # One Vertex route per model, kept across key changes so its last check still counts.
        self.vertex: dict[str, Route] = {}
        if settings.vertex_ai:
            for p in self.seats:
                try:
                    self.vertex[p] = Route("vertex", self._make_vertex(p))
                except Exception as e:  # noqa: BLE001 - other routes and models still work
                    log.warning("could not set up Vertex AI for %s: %s", p, type(e).__name__)
        self.apply_keys({})

    @property
    def seats(self) -> list[str]:
        """Every model SASI may put on a team, frontier models first."""
        return [*FRONTIER, *self.open_seats]

    def _make(self, provider: str, key: str) -> ProviderAdapter:
        s = self.settings
        if provider == "claude":
            return ClaudeAdapter(key, s.claude_model)
        if provider == "gemini":
            return GeminiAdapter(key, s.gemini_model)
        raise ValueError(f"{name(provider)} has no API key option")

    def _make_vertex(self, provider: str) -> ProviderAdapter:
        from .vertex import VertexClaudeAdapter, VertexGeminiAdapter, VertexOpenModelAdapter

        s = self.settings
        if provider == "claude":
            # Newest first; the first one this project can call is used.
            return VertexClaudeAdapter(s.gcp_project, s.vertex_location,
                                       [s.vertex_claude_model or s.claude_model, "claude-opus-5-5", "claude-opus-5"])
        if provider == "gemini":
            return VertexGeminiAdapter(s.gcp_project, s.vertex_location, [s.vertex_gemini_model or s.gemini_model, "gemini-4-argon"])
        seat = SEATS[provider]
        return VertexOpenModelAdapter(provider, seat.name, s.gcp_project, list(seat.vertex_models), list(seat.locations),
                                      seat.max_output)

    def _build(self, provider: str, key_route: Route | None = None) -> ProviderAdapter | None:
        key = self.keys.get(provider)
        if key and key_route is None:
            key_route = Route("key", self._make(provider, key))
        routes = [r for r in (self.vertex.get(provider), key_route) if r]
        if routes:
            return RoutedAdapter(provider, name(provider), routes)
        if self.settings.allow_simulated:
            return SimulatedAdapter(provider, self.settings.simulated_delay)
        return None

    def _install(self, provider: str, adapter: ProviderAdapter | None) -> None:
        if adapter is None:
            self.adapters.pop(provider, None)
            self.status.pop(provider, None)
            return
        self.adapters[provider] = adapter
        if isinstance(adapter, RoutedAdapter):
            self.status[provider] = adapter.status()
        else:
            self.status.pop(provider, None)

    def apply_keys(self, keys: dict[str, str]) -> None:
        """Rebuild every model's routes from the stored keys. Called at start-up."""
        self.keys = {p: (keys.get(p) or "").strip() for p in KEY_SEATS if (keys.get(p) or "").strip()}
        self.hints = {p: hint(k) for p, k in self.keys.items()}
        self.adapters, self.status = {}, {}
        for p in self.seats:
            self._install(p, self._build(p))

    @property
    def uses_vertex(self) -> bool:
        return bool(self.vertex)

    async def test_key(self, provider: str, key: str) -> str:
        """Check a key by listing the provider's models. Returns the model SASI will use."""
        adapter = self._make(provider, key.strip())
        # Listing models proves the key works, even when the model name is pinned.
        await asyncio.wait_for(adapter.list_models(), timeout=25)
        return await asyncio.wait_for(adapter.resolve_model(), timeout=25)

    def set_adapter(self, provider: str, key: str, model: str) -> None:
        """Use a key that test_key just checked, next to the Google Cloud route if there is one."""
        key = key.strip()
        self.keys[provider] = key
        self.hints[provider] = hint(key)
        adapter = self._make(provider, key)
        adapter._model = model  # already resolved by test_key
        self._install(provider, self._build(provider, Route("key", adapter, ok=True, model=model)))

    def remove(self, provider: str) -> None:
        """Stop using this model's key. Its Google Cloud route, if any, carries on."""
        self.keys.pop(provider, None)
        self.hints.pop(provider, None)
        self._install(provider, self._build(provider))

    @property
    def live_count(self) -> int:
        return sum(1 for a in self.adapters.values() if not a.simulated)

    def eligible(self) -> list[str]:
        """Connected, healthy models, frontier first. Simulated ones (tests only) when nothing real is connected."""
        live = [p for p, a in self.adapters.items() if not a.simulated and self.status.get(p, {}).get("ok", True)]
        if live:
            return live
        return [p for p, a in self.adapters.items() if a.simulated]

    def ranked(self, profile: str, input_tokens: int = 0) -> list[str]:
        """Every working model in order of preference for this kind of work: frontier models first, then the open
        models whose context window holds the input and whose strengths suit the task."""
        working = self.eligible()
        frontier = [p for p in FRONTIER if p in working]
        opens = rank_open([p for p in working if p not in FRONTIER], profile, input_tokens)
        return frontier + opens + [p for p in working if p not in frontier and p not in opens]

    def team(self, profile: str, input_tokens: int = 0) -> list[str]:
        """The models for one run: the best ones from `ranked`, up to the team size."""
        return self.ranked(profile, input_tokens)[: max(1, self.settings.team_size)]

    def context(self, provider: str) -> int:
        seat = SEATS.get(provider)
        base = seat.context if seat else 128_000
        return min(base, self.context_cap.get(provider, base))

    def learn_context(self, provider: str, limit: int) -> int:
        """A model said the input was too long: remember a smaller context size for it."""
        current = self.context(provider)
        new = limit if 0 < limit < current else int(current * 0.75)
        self.context_cap[provider] = new
        return new

    @staticmethod
    async def _check(name_: str, adapter: ProviderAdapter, timeout: float = 20) -> tuple[str, dict]:
        """Listing models (or counting tokens on Vertex) proves a route works without paying for an answer."""
        if isinstance(adapter, RoutedAdapter):
            return name_, await adapter.check(timeout)
        try:
            await asyncio.wait_for(adapter.list_models(), timeout=timeout)
            model = await asyncio.wait_for(adapter.resolve_model(), timeout=timeout)
            return name_, {"ok": True, "model": model, "simulated": adapter.simulated}
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            return name_, {"ok": False, "model": None, "simulated": adapter.simulated, "error": str(e)[:300]}

    async def refresh(self) -> dict[str, dict]:
        results = await asyncio.gather(*(self._check(n, a) for n, a in self.adapters.items()))
        self.status = dict(results)
        self.checked_at = time.time()
        return self.status

    def sync(self, provider: str) -> None:
        """Pick up what a run learned about a model's routes (one stopped working, another took over)."""
        adapter = self.adapters.get(provider)
        if isinstance(adapter, RoutedAdapter) and self.status.get(provider, {}).get("ok") is not False:
            self.status[provider] = adapter.status()

    def mark_failed(self, provider: str, error: str) -> None:
        """Every way to reach this model failed during a run: skip it until it passes a check again."""
        if provider in self.adapters:
            self.status[provider] = {**self.status.get(provider, {}), "ok": False, "error": error[:300]}

    async def recheck_failed(self, timeout: float = 12) -> None:
        """Before a run, check the models that failed (a fixed key or setting is used again) and the ones not
        checked yet, so the team is picked from models that work."""
        failed = [(p, a) for p, a in self.adapters.items()
                  if not a.simulated and self.status.get(p, {}).get("ok") is not True]
        if failed:
            for name_, st in await asyncio.gather(*(self._check(p, a, timeout) for p, a in failed)):
                self.status[name_] = st

    def unavailable(self) -> dict[str, str]:
        """Why a model can't take part right now: a frontier model with nothing connected, or every way to a model failing."""
        out = {}
        working = set(self.eligible())
        for p in self.seats:
            if p in working:
                continue
            adapter = self.adapters.get(p)
            if not adapter or adapter.simulated:
                if p in FRONTIER:
                    out[p] = "no key added"
            elif isinstance(adapter, RoutedAdapter) and [r.via for r in adapter.routes] != ["key"]:
                out[p] = "it is not working: " + (self.status.get(p, {}).get("error") or "the last check failed")[:300]
            else:
                out[p] = "its key is not working: " + (self.status.get(p, {}).get("error") or "the last check failed")[:200]
        return out

    def describe(self) -> list[dict]:
        out = []
        for p in self.seats:
            adapter = self.adapters.get(p)
            if adapter is None and p not in FRONTIER:
                continue  # an open model is only listed when Vertex AI is on
            st = self.status.get(p, {})
            seat = SEATS[p]
            out.append({
                "provider": p,
                "display_name": seat.name,
                "tier": seat.tier,
                "about": seat.about,
                "context": self.context(p),
                "connected": bool(adapter and not adapter.simulated),
                "simulated": bool(adapter and adapter.simulated),
                "ok": st.get("ok"),
                "model": st.get("model"),
                "via": st.get("via"),
                "error": st.get("error"),
                "key_hint": self.hints.get(p),
                "has_key": p in self.keys,
                "key_option": p in KEY_SEATS,
                "routes": adapter.status()["routes"] if isinstance(adapter, RoutedAdapter) else [],
            })
        return out
