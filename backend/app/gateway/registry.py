"""Model registry: which models SASI can reach, how (Google Cloud Vertex AI or an API key), and who is on a run's team."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..keys import hint
from .anthropic_adapter import ClaudeAdapter
from .base import ProviderAdapter, ProviderError
from .catalog import SEATS, name, rank_open
from .gemini_adapter import GeminiAdapter
from .routes import RoutedAdapter, Route
from .simulated import SimulatedAdapter

log = logging.getLogger("sasi.registry")

FRONTIER = ("claude", "gemini")
KEY_SEATS = ("claude", "gemini")  # models that can also use the provider's own API key
PROVIDERS = KEY_SEATS  # kept for older imports: the models with an API key


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_date() -> str:
    return _utc_now().date().isoformat()


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
                if not self._vertex_route_allowed(p):
                    continue
                try:
                    self.vertex[p] = Route("vertex", self._make_vertex(p))
                except Exception as e:  # noqa: BLE001 - other routes and models still work
                    log.warning("could not set up Vertex AI for %s: %s", p, type(e).__name__)
        self.apply_keys({})

    @property
    def seats(self) -> list[str]:
        """Every model SASI may put on a team, frontier models first."""
        return [*FRONTIER, *self.open_seats]

    @staticmethod
    def _retirement_error(provider: str) -> str | None:
        seat = SEATS.get(provider)
        if seat and seat.retirement_date and _utc_date() >= seat.retirement_date:
            return f"Vertex AI endpoints retired on {seat.retirement_date} (UTC). This model is unavailable."
        return None

    def trial_credit_window_error(self) -> str | None:
        if not self.settings.trial_credits_only:
            return None
        deadline = self.settings.trial_credit_expires_at.strip()
        if not deadline:
            return None  # no expiry is asserted; this is not a credit-balance check
        try:
            expires = datetime.fromisoformat(deadline)
            if expires.tzinfo is None or expires.utcoffset() != timedelta(0):
                raise ValueError("a UTC timestamp is required")
        except (ValueError, OverflowError):
            return "Trial credit expiry is invalid. All model access is disabled until a valid UTC deadline is configured."
        if _utc_now() >= expires:
            return f"Trial credit window ended at {deadline}. All model access is disabled."
        return None

    def _trial_policy_error(self, provider: str) -> str | None:
        window_error = self.trial_credit_window_error()
        if window_error:
            return window_error
        if self.settings.trial_credits_only:
            if provider == "claude" and self.settings.allow_claude_api_key:
                return None
            if provider != "gemini" or not self.settings.vertex_ai:
                exception = " and the owner's explicitly enabled direct Claude API key" if self.settings.allow_claude_api_key else ""
                return f"Disabled by trial-credit-only policy: only Gemini through Vertex AI{exception} is allowed."
        return None

    def key_route_error(self, provider: str) -> str | None:
        """Authorize the provider's own API, independently of its Vertex route."""
        if error := self.trial_credit_window_error():
            return error
        if provider not in KEY_SEATS:
            return f"{name(provider)} has no API key option."
        if self.settings.trial_credits_only and not (provider == "claude" and self.settings.allow_claude_api_key):
            return "This API-key route is disabled by trial-credit-only policy."
        return None

    def key_route_allowed(self, provider: str) -> bool:
        return self.key_route_error(provider) is None

    def _vertex_route_allowed(self, provider: str) -> bool:
        if not self.settings.vertex_ai or self._disabled_error(provider):
            return False
        if provider == "claude":
            return (self.settings.vertex_claude_enabled and not self.settings.trial_credits_only
                    and not self.settings.allow_claude_api_key)
        return True

    def _route_allowed(self, provider: str, via: str) -> bool:
        if via == "key":
            return self.key_route_allowed(provider)
        return via == "vertex" and self._vertex_route_allowed(provider)

    def _disabled_error(self, provider: str) -> str | None:
        reasons = [self._trial_policy_error(provider), self._retirement_error(provider)]
        return " ".join(reason for reason in reasons if reason) or None

    def _remove_disabled(self) -> None:
        # Enforce policy and retirement dates before checks or selection,
        # including for cached routes in a long-running process.
        for provider in self.seats:
            error = self._disabled_error(provider)
            if error:
                self.adapters.pop(provider, None)
                self.vertex.pop(provider, None)
                self.status[provider] = {"ok": False, "error": error, "model": None, "simulated": False}
            else:
                if not self._vertex_route_allowed(provider):
                    self.vertex.pop(provider, None)
                adapter = self.adapters.get(provider)
                if isinstance(adapter, RoutedAdapter):
                    allowed = [route for route in adapter.routes if self._route_allowed(provider, route.via)]
                    if not allowed:
                        self.adapters.pop(provider, None)
                        self.status.pop(provider, None)
                    elif len(allowed) != len(adapter.routes):
                        adapter.routes = allowed
                        self.status[provider] = adapter.status()
                elif adapter is not None and self.settings.trial_credits_only:
                    self.adapters.pop(provider, None)
                    self.status.pop(provider, None)

    def _make(self, provider: str, key: str) -> ProviderAdapter:
        s = self.settings
        if error := self.key_route_error(provider):
            raise ProviderError(provider, error)
        if provider == "claude":
            return ClaudeAdapter(key, s.claude_model)
        if provider == "gemini":
            return GeminiAdapter(key, s.gemini_model)
        raise ValueError(f"{name(provider)} has no API key option")

    def _make_vertex(self, provider: str) -> ProviderAdapter:
        error = self._disabled_error(provider)
        if error:
            raise ProviderError(provider, error)
        if provider == "claude" and not self._vertex_route_allowed(provider):
            raise ProviderError(provider, "This Vertex AI route is disabled by the configured provider policy.")
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
                                      seat.max_output, list(seat.vision_models))

    def _build(self, provider: str, key_route: Route | None = None) -> ProviderAdapter | None:
        if self._disabled_error(provider):
            return None
        if not self.key_route_allowed(provider):
            key_route = None
        key = self.keys.get(provider) if self.key_route_allowed(provider) else None
        if key and key_route is None:
            key_route = Route("key", self._make(provider, key))
        routes = [r for r in (self.vertex.get(provider), key_route) if r and self._route_allowed(provider, r.via)]
        if routes:
            return RoutedAdapter(provider, name(provider), routes)
        if self.settings.allow_simulated and not self.settings.trial_credits_only:
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
        self._remove_disabled()

    @property
    def uses_vertex(self) -> bool:
        self._remove_disabled()
        return bool(self.vertex)

    async def test_key(self, provider: str, key: str) -> str:
        """Check a key by listing the provider's models. Returns the model SASI will use."""
        if error := self.key_route_error(provider):
            raise ProviderError(provider, error)
        adapter = self._make(provider, key.strip())
        # Listing models proves the key works, even when the model name is pinned.
        await asyncio.wait_for(adapter.list_models(), timeout=25)
        return await asyncio.wait_for(adapter.resolve_model(), timeout=25)

    def set_adapter(self, provider: str, key: str, model: str) -> None:
        """Use a key that test_key just checked, next to the Google Cloud route if there is one."""
        key = key.strip()
        self.keys[provider] = key
        self.hints[provider] = hint(key)
        if not self.key_route_allowed(provider):
            self._install(provider, self._build(provider))
            self._remove_disabled()
            return
        adapter = self._make(provider, key)
        adapter._model = model  # already resolved by test_key
        self._install(provider, self._build(provider, Route("key", adapter, ok=True, model=model)))

    def remove(self, provider: str) -> None:
        """Stop using this model's key. Its Google Cloud route, if any, carries on."""
        self.keys.pop(provider, None)
        self.hints.pop(provider, None)
        self._install(provider, self._build(provider))
        self._remove_disabled()

    @property
    def live_count(self) -> int:
        self._remove_disabled()
        return sum(1 for a in self.adapters.values() if not a.simulated)

    def eligible(self) -> list[str]:
        """Connected, healthy models, frontier first. Simulated ones (tests only) when nothing real is connected."""
        self._remove_disabled()
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

    async def _check(self, name_: str, adapter: ProviderAdapter, timeout: float = 20) -> tuple[str, dict]:
        """Listing models (or counting tokens on Vertex) proves a route works without paying for an answer."""
        error = self._disabled_error(name_)
        if not error and ((isinstance(adapter, RoutedAdapter)
                           and any(not self._route_allowed(name_, route.via) for route in adapter.routes))
                          or (self.settings.trial_credits_only and not isinstance(adapter, RoutedAdapter))):
            error = "This route cannot be checked under the configured provider policy."
        if error:
            return name_, {"ok": False, "error": error, "model": None, "simulated": False}
        if isinstance(adapter, RoutedAdapter):
            return name_, await adapter.check(timeout)
        try:
            await asyncio.wait_for(adapter.list_models(), timeout=timeout)
            model = await asyncio.wait_for(adapter.resolve_model(), timeout=timeout)
            return name_, {"ok": True, "model": model, "simulated": adapter.simulated}
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            return name_, {"ok": False, "model": None, "simulated": adapter.simulated, "error": str(e)[:700]}

    async def refresh(self) -> dict[str, dict]:
        self._remove_disabled()
        results = await asyncio.gather(*(self._check(n, a) for n, a in self.adapters.items()))
        self.status = dict(results)
        self._remove_disabled()
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
            self.status[provider] = {**self.status.get(provider, {}), "ok": False, "error": error[:700]}

    async def recheck_failed(self, timeout: float = 12) -> None:
        """Before a run, check the models that failed (a fixed key or setting is used again) and the ones not
        checked yet, so the team is picked from models that work."""
        self._remove_disabled()
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
            error = self._disabled_error(p)
            if error:
                out[p] = error
                continue
            adapter = self.adapters.get(p)
            if not adapter or adapter.simulated:
                if self.settings.trial_credits_only and p == "gemini":
                    out[p] = "Vertex Gemini is unavailable; API-key fallback is disabled by trial-credit-only policy."
                elif p in FRONTIER:
                    out[p] = "no key added"
            elif isinstance(adapter, RoutedAdapter) and [r.via for r in adapter.routes] != ["key"]:
                out[p] = "it is not working: " + (self.status.get(p, {}).get("error") or "the last check failed")[:300]
            else:
                out[p] = "its key is not working: " + (self.status.get(p, {}).get("error") or "the last check failed")[:200]
        return out

    def describe(self) -> list[dict]:
        self._remove_disabled()
        out = []
        for p in self.seats:
            adapter = self.adapters.get(p)
            retired = self._retirement_error(p)
            policy_blocked = bool(self._trial_policy_error(p))
            if adapter is None and p not in FRONTIER and not (retired and self.settings.vertex_ai) and not policy_blocked:
                continue  # an open model is only listed when Vertex AI is on
            st = self.status.get(p, {})
            if self.settings.trial_credits_only and p == "gemini" and adapter is None and not st.get("error"):
                st = {"ok": False, "error": "Vertex Gemini is unavailable; API-key fallback is disabled by trial-credit-only policy."}
            seat = SEATS[p]
            out.append({
                "provider": p,
                "display_name": seat.name,
                "tier": seat.tier,
                "about": seat.about,
                "retirement_date": seat.retirement_date,
                "policy_blocked": policy_blocked,
                "context": self.context(p),
                "connected": bool(adapter and not adapter.simulated),
                "simulated": bool(adapter and adapter.simulated),
                "ok": st.get("ok"),
                "model": st.get("model"),
                "via": st.get("via"),
                "error": st.get("error"),
                "key_hint": self.hints.get(p),
                "has_key": p in self.keys,
                "key_option": self.key_route_allowed(p),
                "routes": adapter.status()["routes"] if isinstance(adapter, RoutedAdapter) else [],
            })
        return out
