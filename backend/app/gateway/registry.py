"""Model registry: which providers are connected and which model each one uses."""
from __future__ import annotations

import asyncio
import time

from ..config import Settings
from .anthropic_adapter import ClaudeAdapter
from .base import ProviderAdapter, ProviderError
from .gemini_adapter import GeminiAdapter
from .openai_adapter import ChatGPTAdapter
from .simulated import SimulatedAdapter

PROVIDERS = ("claude", "chatgpt", "gemini")


class ModelRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapters: dict[str, ProviderAdapter] = {}
        keys = {
            "claude": (settings.anthropic_api_key, ClaudeAdapter, settings.claude_model),
            "chatgpt": (settings.openai_api_key, ChatGPTAdapter, settings.openai_model),
            "gemini": (settings.gemini_api_key, GeminiAdapter, settings.gemini_model),
        }
        for name, (key, cls, model) in keys.items():
            if key:
                self.adapters[name] = cls(key.strip(), model)
            elif settings.allow_simulated:
                self.adapters[name] = SimulatedAdapter(name, settings.simulated_delay)
        self.status: dict[str, dict] = {}
        self.checked_at: float = 0

    @property
    def live_count(self) -> int:
        return sum(1 for a in self.adapters.values() if not a.simulated)

    def eligible(self) -> list[str]:
        """Providers that are connected and healthy. Simulated ones only when no real key exists."""
        live = [p for p, a in self.adapters.items() if not a.simulated and self.status.get(p, {}).get("ok", True)]
        if live:
            return live
        return [p for p, a in self.adapters.items() if a.simulated]

    async def refresh(self) -> dict[str, dict]:
        async def check(name: str, adapter: ProviderAdapter) -> tuple[str, dict]:
            try:
                model = await asyncio.wait_for(adapter.resolve_model(), timeout=20)
                return name, {"ok": True, "model": model, "simulated": adapter.simulated}
            except (ProviderError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - surfaced to the UI
                return name, {"ok": False, "model": None, "simulated": adapter.simulated, "error": str(e)[:300]}

        results = await asyncio.gather(*(check(n, a) for n, a in self.adapters.items()))
        self.status = dict(results)
        self.checked_at = time.time()
        return self.status

    def describe(self) -> list[dict]:
        out = []
        for name in PROVIDERS:
            adapter = self.adapters.get(name)
            st = self.status.get(name, {})
            out.append({
                "provider": name,
                "display_name": {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini"}[name],
                "connected": bool(adapter and not adapter.simulated),
                "simulated": bool(adapter and adapter.simulated),
                "ok": st.get("ok"),
                "model": st.get("model"),
                "error": st.get("error"),
            })
        return out
