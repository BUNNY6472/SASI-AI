from __future__ import annotations

from ..models import Run


class MemoryStore:
    """Keeps runs in process memory. Used locally and as the live cache for BigQuery."""

    def __init__(self, max_runs: int = 200):
        self._runs: dict[str, Run] = {}
        self._max = max_runs

    async def load(self) -> list[Run]:
        return list(self._runs.values())

    async def save(self, run: Run, checkpoint: bool = False) -> None:
        self._runs[run.id] = run
        if len(self._runs) > self._max:
            oldest = sorted(self._runs.values(), key=lambda r: r.updated_at)[: len(self._runs) - self._max]
            for r in oldest:
                self._runs.pop(r.id, None)

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list(self, limit: int = 50) -> list[Run]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)[:limit]
