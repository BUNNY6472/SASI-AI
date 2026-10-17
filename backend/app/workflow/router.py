"""Chooses who does each task and who reviews it, and records why.

Inputs: eligibility (connected + healthy), internal performance history on this
field profile, and load within the current run. No provider is assumed to be
"best" by name; history starts neutral and is learned from accepted work.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class Choice:
    provider: str
    reason: str


class PerformanceHistory:
    """Per (provider, profile) counts of accepted vs attempted work."""

    def __init__(self) -> None:
        self.attempts: dict[tuple[str, str], int] = defaultdict(int)
        self.accepted: dict[tuple[str, str], int] = defaultdict(int)

    def record(self, provider: str, profile: str, accepted: bool) -> None:
        self.attempts[(provider, profile)] += 1
        if accepted:
            self.accepted[(provider, profile)] += 1

    def score(self, provider: str, profile: str) -> float:
        a = self.attempts.get((provider, profile), 0)
        ok = self.accepted.get((provider, profile), 0)
        # Smoothed acceptance rate: unknown = 0.5, and a few results cannot dominate.
        return (ok + 3) / (a + 6)

    def snapshot(self) -> list[dict]:
        rows = []
        for (provider, profile), a in sorted(self.attempts.items()):
            rows.append({
                "provider": provider,
                "profile": profile,
                "attempts": a,
                "accepted": self.accepted[(provider, profile)],
                "score": round(self.score(provider, profile), 3),
            })
        return rows


class Router:
    def __init__(self, history: PerformanceHistory):
        self.history = history

    def _rank(self, eligible: list[str], profile: str, load: dict[str, int], exclude: set[str], kind: str) -> list[tuple[float, str]]:
        ranked = []
        for p in eligible:
            if p in exclude:
                continue
            # Spread work across providers in a run so their strengths combine.
            s = self.history.score(p, profile) - 0.2 * load.get(f"{kind}:{p}", 0)
            ranked.append((s, p))
        ranked.sort(key=lambda t: (-t[0], t[1]))
        return ranked

    def pick(self, eligible: list[str], profile: str, load: dict[str, int], role: str,
             exclude: set[str] | None = None, kind: str = "work") -> Choice:
        """Pick the best eligible provider and count it in this run's load for `kind` (work or review)."""
        exclude = exclude or set()
        ranked = self._rank(eligible, profile, load, exclude, kind)
        if not ranked:
            # Only excluded providers left: fall back to the full list (lower assurance).
            ranked = self._rank(eligible, profile, load, set(), kind)
            if not ranked:
                raise RuntimeError("no providers available")
            s, p = ranked[0]
            load[f"{kind}:{p}"] = load.get(f"{kind}:{p}", 0) + 1
            return Choice(p, f"{role}: only option left (no independent provider available)")
        s, p = ranked[0]
        hist = self.history.attempts.get((p, profile), 0)
        basis = f"{self.history.accepted.get((p, profile), 0)}/{hist} accepted on this field" if hist else "no history yet on this field"
        current = load.get(f"{kind}:{p}", 0)
        load[f"{kind}:{p}"] = current + 1
        return Choice(p, f"{role}: score {s:.2f} ({basis}; {current} {kind} items already in this run)")
