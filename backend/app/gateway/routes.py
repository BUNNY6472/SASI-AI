"""Each model can be reached two ways: through Google Cloud Vertex AI, or with the provider's own API key.

A RoutedAdapter holds the ways in order of preference. It uses the first one that works, and if a call
fails before the model has written anything, the next way takes over for that call. A way that fails for
a lasting reason (no permission, not enabled, no credit) is skipped until the next check.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .base import ModelCall, ModelResult, ProviderAdapter, ProviderError, is_key_problem

VIA = {"vertex": "Google Cloud", "key": "API key"}

_LASTING = ("404", "not found", "precondition", "not enabled", "not allowed", "has not been used", "disabled",
            "could not sign in", "could not reach vertex", "no google cloud project")


def _plain(e: Exception) -> str:
    """The error without the "claude: " prefix, since the screen already says which model it is."""
    msg = str(e) or type(e).__name__
    prefix = f"{getattr(e, 'provider', '')}: "
    return (msg[len(prefix):] if prefix != ": " and msg.startswith(prefix) else msg)[:300]


def lasting_problem(message: str) -> bool:
    low = message.lower()
    return is_key_problem(message) or any(t in low for t in _LASTING)


@dataclass
class Route:
    via: str  # "vertex" | "key"
    adapter: ProviderAdapter
    ok: bool | None = None  # None: not checked yet
    model: str | None = None
    error: str | None = None

    def view(self) -> dict:
        return {"via": self.via, "label": VIA[self.via], "ok": self.ok, "model": self.model, "error": self.error}


class RoutedAdapter:
    simulated = False

    def __init__(self, provider: str, display_name: str, routes: list[Route]):
        self.provider = provider
        self.display_name = display_name
        self.routes = routes

    @property
    def active(self) -> Route:
        return next((r for r in self.routes if r.ok is not False), self.routes[0])

    def _order(self) -> list[Route]:
        working = [r for r in self.routes if r.ok is not False]
        return working or list(self.routes)  # if every way failed before, try them all again

    @staticmethod
    async def _check_route(r: Route, timeout: float) -> None:
        try:
            await asyncio.wait_for(r.adapter.list_models(), timeout=timeout)
            r.model = await asyncio.wait_for(r.adapter.resolve_model(), timeout=timeout)
            r.ok, r.error = True, None
        except Exception as e:  # noqa: BLE001 - shown on Connections
            r.ok, r.error = False, _plain(e)

    async def check(self, timeout: float = 20) -> dict:
        await asyncio.gather(*(self._check_route(r, timeout) for r in self.routes))
        return self.status()

    def status(self) -> dict:
        st: dict = {"simulated": False, "routes": [r.view() for r in self.routes]}
        if any(r.ok for r in self.routes):
            a = self.active
            st.update(ok=True, model=a.model, via=a.via)
        elif all(r.ok is False for r in self.routes):
            st.update(ok=False, model=None, via=None,
                      error="; ".join(f"{VIA[r.via]}: {r.error}" for r in self.routes if r.error)[:400])
        return st

    async def list_models(self) -> list[str]:
        st = await self.check()
        if not st.get("ok"):
            raise ProviderError(self.provider, st.get("error") or "not working")
        return [st["model"]]

    async def resolve_model(self) -> str:
        a = self.active
        return a.model or await a.adapter.resolve_model()

    async def generate(self, call: ModelCall) -> ModelResult:
        order = self._order()
        on_text = call.on_text
        last: ProviderError | None = None
        for i, r in enumerate(order):
            wrote = False
            if on_text:
                async def tap(piece: str) -> None:
                    nonlocal wrote
                    wrote = True
                    await on_text(piece)

                call.on_text = tap
            try:
                result = await r.adapter.generate(call)
            except Exception as e:  # noqa: BLE001 - normalised below
                err = e if isinstance(e, ProviderError) else ProviderError(self.provider, f"{type(e).__name__}: {e}")
                last = err
                if not err.retryable and lasting_problem(str(err)):
                    r.ok, r.error = False, _plain(err)  # skip this way until the next check
                if wrote or i == len(order) - 1:
                    if err is e:
                        raise
                    raise err from e
                continue  # nothing written yet: the next way takes over this call
            finally:
                call.on_text = on_text
            r.ok, r.error = True, None
            r.model = r.model or result.model
            result.via = r.via
            return result
        raise last or ProviderError(self.provider, "no way to reach this model")
