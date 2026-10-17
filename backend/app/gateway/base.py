"""Common contract every provider adapter follows.

Adapters normalize text, usage and errors so the workflow controller never
touches provider-specific shapes or keys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ModelCall:
    """One task-scoped request to a model."""

    system: str
    prompt: str
    kind: str  # plan | plan_review | task | review | integrate | final_eval
    max_output_tokens: int = 4000
    # Free-form hints used only by the simulated adapter.
    hints: dict = field(default_factory=dict)


@dataclass
class ModelResult:
    provider: str
    model: str
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    simulated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, retryable: bool = False):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable


class ProviderAdapter(Protocol):
    provider: str  # "claude" | "chatgpt" | "gemini"
    display_name: str
    simulated: bool

    async def list_models(self) -> list[str]: ...

    async def resolve_model(self) -> str: ...

    async def generate(self, call: ModelCall) -> ModelResult: ...
