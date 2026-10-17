"""Common contract every provider adapter follows.

Adapters normalize text, usage and errors so the workflow controller never
touches provider-specific shapes or keys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

if TYPE_CHECKING:
    from ..files import StoredFile


@dataclass
class ModelCall:
    """One task-scoped request to a model."""

    system: str
    prompt: str
    kind: str  # plan | plan_review | task | review | integrate | final_eval
    max_output_tokens: int = 4000
    # Free-form hints used only by the simulated adapter.
    hints: dict = field(default_factory=dict)
    # Files the model should look at directly (images, video, audio, PDFs, data).
    attachments: list["StoredFile"] = field(default_factory=list)
    # Called with each piece of text as the model writes it, so the screen updates live.
    on_text: Callable[[str], Awaitable[None]] | None = None


def split_attachments(call: ModelCall, provider: str) -> tuple[list["StoredFile"], str]:
    """Files this provider can read directly, plus prompt text for data files and ones it cannot open."""
    from ..files import CAN_READ, DIRECT_LIMIT

    media, notes = [], []
    for f in call.attachments:
        if f.kind == "other":
            notes.append(f"[{f.name} ({f.mime}, {f.size / 1024:.0f} KB) is attached, but it is a binary format no model can open. "
                         + (f.text or "Mention it if it matters.") + "]")
        elif f.kind == "text":
            notes.append(f'<file name="{f.name}" untrusted="true">\n{f.text}\n</file>')
        elif f.kind in CAN_READ.get(provider, set()) and f.size <= DIRECT_LIMIT.get(provider, {}).get(f.kind, f.size):
            media.append(f)
        elif f.kind in CAN_READ.get(provider, set()):
            notes.append(f"[{f.name} is too large for this model to open directly. Use the other models' analyses of it.]")
        else:
            notes.append(f"[{f.name} is a {f.kind} file this model cannot open directly. "
                         "Use the other models' analyses of it and say so where it matters.]")
    text = ("\n\n".join(notes) + "\n\n") if notes else ""
    return media, text


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
