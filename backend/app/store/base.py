from __future__ import annotations

from typing import Protocol

from ..models import Run


class RunStore(Protocol):
    async def load(self) -> list[Run]: ...

    async def save(self, run: Run, checkpoint: bool = False) -> None: ...

    def get(self, run_id: str) -> Run | None: ...

    def list(self, limit: int = 50) -> list[Run]: ...
