"""Model registry: which models SASI can reach, how (Google Cloud Vertex AI or an API key), and which model each uses."""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import Settings
from ..keys import hint
from .anthropic_adapter import ClaudeAdapter
from .base import ProviderAdapter
from .gemini_adapter import GeminiAdapter
from .openai_adapter import ChatGPTAdapter
from .routes import RoutedAdapter, Route
from .simulated import SimulatedAdapter

log = logging.getLogger("sasi.registry")

PROVIDERS = ("claude", "chatgpt", "gemini")
DISPLAY = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini"}


class ModelRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapters: dict[str, ProviderAdapter] = {}
        self.hints: dict[str, str] = {}
        self.keys: dict[str, str] = {}
        self.status: dict[str, dict] = {}
        self.checked_at: float = 0
        # One Vertex route per model, kept across key changes so its last check still counts.
        self.vertex: dict[str, Route] = {}
        if settings.vertex_ai:
            for p in PROVIDERS:
                try:
                    self.vertex[p] = Route("vertex", self._make_vertex(p))
                except Exception as e:  # noqa: BLE001 - the API key route still works
                    log.warning("could not set up Vertex AI for %s: %s", p, type(e).__name__)
        self.apply_keys({})

    def _make(self, provider: str, key: str) -> ProviderAdapter:
        s = self.settings
        if provider == "claude":
            return ClaudeAdapter(key, s.claude_model)
        if provider == "chatgpt":
            return ChatGPTAdapter(key, s.openai_model)
        return GeminiAdapter(key, s.gemini_model)

    def _make_vertex(self, provider: str) -> ProviderAdapter:
        from .vertex import VertexClaudeAdapter, VertexGeminiAdapter, VertexOpenAIAdapter

        s = self.settings
        if provider == "claude":
            # Newest first; the first one this project can call is used.
            return VertexClaudeAdapter(s.gcp_project, s.vertex_location,
                                       [s.vertex_claude_model or s.claude_model, "claude-opus-5-5", "claude-opus-5"])
        if provider == "chatgpt":
            return VertexOpenAIAdapter(s.gcp_project, s.vertex_openai_location, [s.vertex_openai_model, "openai/gpt-oss-120b-maas"])
        return VertexGeminiAdapter(s.gcp_project, s.vertex_location, [s.vertex_gemini_model or s.gemini_model, "gemini-4-argon"])

    def _key_first(self, provider: str) -> bool:
        return provider in {p.strip() for p in self.settings.key_first.split(",")}

    def _build(self, provider: str, key_route: Route | None = None) -> ProviderAdapter | None:
        key = self.keys.get(provider)
        if key and key_route is None:
            key_route = Route("key", self._make(provider, key))
        routes = [r for r in (self.vertex.get(provider), key_route) if r]
        if key_route and self._key_first(provider):
            routes = [key_route] + [r for r in routes if r is not key_route]
        if routes:
            return RoutedAdapter(provider, DISPLAY[provider], routes)
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
        self.keys = {p: (keys.get(p) or "").strip() for p in PROVIDERS if (keys.get(p) or "").strip()}
        self.hints = {p: hint(k) for p, k in self.keys.items()}
        self.adapters, self.status = {}, {}
        for p in PROVIDERS:
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
        """Connected, healthy providers. Simulated ones (tests only) when nothing real is connected."""
        live = [p for p, a in self.adapters.items() if not a.simulated and self.status.get(p, {}).get("ok", True)]
        if live:
            return live
        return [p for p, a in self.adapters.items() if a.simulated]

    @staticmethod
    async def _check(name: str, adapter: ProviderAdapter, timeout: float = 20) -> tuple[str, dict]:
        """Listing models (or counting tokens on Vertex) proves a route works without paying for an answer."""
        if isinstance(adapter, RoutedAdapter):
            return name, await adapter.check(timeout)
        try:
            await asyncio.wait_for(adapter.list_models(), timeout=timeout)
            model = await asyncio.wait_for(adapter.resolve_model(), timeout=timeout)
            return name, {"ok": True, "model": model, "simulated": adapter.simulated}
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            return name, {"ok": False, "model": None, "simulated": adapter.simulated, "error": str(e)[:300]}

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
        """Before a run, check again the models that failed, so a fixed key or setting is used again."""
        failed = [(p, a) for p, a in self.adapters.items() if self.status.get(p, {}).get("ok") is False]
        if failed:
            for name, st in await asyncio.gather(*(self._check(p, a, timeout) for p, a in failed)):
                self.status[name] = st

    def unavailable(self) -> dict[str, str]:
        """Why each model can't take part right now: nothing connected, or every way to it is failing."""
        out = {}
        working = set(self.eligible())
        for p in PROVIDERS:
            if p in working:
                continue
            adapter = self.adapters.get(p)
            if not adapter or adapter.simulated:
                out[p] = "no key added"
            elif isinstance(adapter, RoutedAdapter) and [r.via for r in adapter.routes] != ["key"]:
                out[p] = "it is not working: " + (self.status.get(p, {}).get("error") or "the last check failed")[:300]
            else:
                out[p] = "its key is not working: " + (self.status.get(p, {}).get("error") or "the last check failed")[:200]
        return out

    def describe(self) -> list[dict]:
        out = []
        for name in PROVIDERS:
            adapter = self.adapters.get(name)
            st = self.status.get(name, {})
            routes = adapter.status()["routes"] if isinstance(adapter, RoutedAdapter) else []
            out.append({
                "provider": name,
                "display_name": DISPLAY[name],
                "connected": bool(adapter and not adapter.simulated),
                "simulated": bool(adapter and adapter.simulated),
                "ok": st.get("ok"),
                "model": st.get("model"),
                "via": st.get("via"),
                "error": st.get("error"),
                "key_hint": self.hints.get(name),
                "has_key": name in self.keys,
                "routes": routes,
            })
        return out
