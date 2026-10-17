"""Model registry: which providers are connected and which model each one uses."""
from __future__ import annotations

import asyncio
import time

from ..config import Settings
from ..keys import hint
from .anthropic_adapter import ClaudeAdapter
from .base import ProviderAdapter
from .gemini_adapter import GeminiAdapter
from .openai_adapter import ChatGPTAdapter
from .simulated import SimulatedAdapter

PROVIDERS = ("claude", "chatgpt", "gemini")
DISPLAY = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini"}


class ModelRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapters: dict[str, ProviderAdapter] = {}
        self.hints: dict[str, str] = {}
        self.status: dict[str, dict] = {}
        self.checked_at: float = 0
        self.apply_keys({})

    def _make(self, provider: str, key: str) -> ProviderAdapter:
        s = self.settings
        if provider == "claude":
            return ClaudeAdapter(key, s.claude_model)
        if provider == "chatgpt":
            return ChatGPTAdapter(key, s.openai_model)
        return GeminiAdapter(key, s.gemini_model)

    def apply_keys(self, keys: dict[str, str]) -> None:
        """Rebuild adapters from the stored keys. Called at start-up and whenever a key changes."""
        self.adapters, self.hints = {}, {}
        for p in PROVIDERS:
            key = (keys.get(p) or "").strip()
            if key:
                self.adapters[p] = self._make(p, key)
                self.hints[p] = hint(key)
            elif self.settings.allow_simulated:
                self.adapters[p] = SimulatedAdapter(p, self.settings.simulated_delay)
        self.status = {p: st for p, st in self.status.items() if p in self.adapters}

    async def test_key(self, provider: str, key: str) -> str:
        """Check a key by listing the provider's models. Returns the model SASI will use."""
        adapter = self._make(provider, key.strip())
        # Listing models proves the key works, even when the model name is pinned.
        await asyncio.wait_for(adapter.list_models(), timeout=25)
        return await asyncio.wait_for(adapter.resolve_model(), timeout=25)

    def set_adapter(self, provider: str, key: str, model: str) -> None:
        adapter = self._make(provider, key.strip())
        adapter._model = model  # already resolved by test_key
        self.adapters[provider] = adapter
        self.hints[provider] = hint(key.strip())
        self.status[provider] = {"ok": True, "model": model, "simulated": False}

    def remove(self, provider: str) -> None:
        self.adapters.pop(provider, None)
        self.hints.pop(provider, None)
        self.status.pop(provider, None)
        if self.settings.allow_simulated:
            self.adapters[provider] = SimulatedAdapter(provider, self.settings.simulated_delay)

    @property
    def live_count(self) -> int:
        return sum(1 for a in self.adapters.values() if not a.simulated)

    def eligible(self) -> list[str]:
        """Connected, healthy providers. Simulated ones (tests only) when no real key exists."""
        live = [p for p, a in self.adapters.items() if not a.simulated and self.status.get(p, {}).get("ok", True)]
        if live:
            return live
        return [p for p, a in self.adapters.items() if a.simulated]

    @staticmethod
    async def _check(name: str, adapter: ProviderAdapter, timeout: float = 20) -> tuple[str, dict]:
        """Listing models proves the key still works (it costs no tokens)."""
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

    def mark_failed(self, provider: str, error: str) -> None:
        """A key failed during a run (rejected, or out of credit): skip it until it passes a check again."""
        if provider in self.adapters:
            self.status[provider] = {**self.status.get(provider, {}), "ok": False, "error": error[:300]}

    async def recheck_failed(self, timeout: float = 12) -> None:
        """Before a run, check again the keys that failed, so a fixed key (or a topped-up account) is used again."""
        failed = [(p, a) for p, a in self.adapters.items() if self.status.get(p, {}).get("ok") is False]
        if failed:
            for name, st in await asyncio.gather(*(self._check(p, a, timeout) for p, a in failed)):
                self.status[name] = st

    def unavailable(self) -> dict[str, str]:
        """Why each model can't take part right now: no key, or a key that is failing."""
        out = {}
        working = set(self.eligible())
        for p in PROVIDERS:
            if p in working:
                continue
            adapter = self.adapters.get(p)
            if not adapter or adapter.simulated:
                out[p] = "no key added"
            else:
                out[p] = "its key is not working: " + (self.status.get(p, {}).get("error") or "the last check failed")[:200]
        return out

    def describe(self) -> list[dict]:
        out = []
        for name in PROVIDERS:
            adapter = self.adapters.get(name)
            st = self.status.get(name, {})
            out.append({
                "provider": name,
                "display_name": DISPLAY[name],
                "connected": bool(adapter and not adapter.simulated),
                "simulated": bool(adapter and adapter.simulated),
                "ok": st.get("ok"),
                "model": st.get("model"),
                "error": st.get("error"),
                "key_hint": self.hints.get(name),
            })
        return out


_KEY_PROBLEMS = ("401", "403", "authentication", "unauthorized", "permission", "api key", "api_key", "invalid x-api-key",
                 "quota", "billing", "credit", "insufficient", "payment")


def is_key_problem(message: str) -> bool:
    """Errors that mean the key itself is bad or the account is out of credit, not a passing hiccup."""
    low = message.lower()
    return any(t in low for t in _KEY_PROBLEMS)
