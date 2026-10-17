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
    # Long shared rules (Project Eris guidebook) that go before the system prompt on every call of a run.
    # Kept separate so providers can cache this repeated prefix.
    rules: str = ""

    @property
    def full_system(self) -> str:
        return f"{self.rules}\n\n{self.system}" if self.rules else self.system


def reply_room(call: ModelCall, model_max: int) -> int:
    """Tokens a model may use for one reply. Thinking models spend part of it reasoning before they write,
    so the answer allowance gets the same again on top (at least 8,000), within the model's own maximum."""
    return max(1_000, min(model_max, call.max_output_tokens + max(8_000, call.max_output_tokens)))


def no_text(provider: str, stop_reason: str) -> "ProviderError":
    """A reply with no answer text, which used to show as an empty panel."""
    why = ("it used its whole reply allowance thinking before writing the answer"
           if "max" in (stop_reason or "").lower() or "length" in (stop_reason or "").lower()
           else f"stop reason: {stop_reason or 'none given'}")
    return ProviderError(provider, f"the model returned no answer text ({why})", retryable=True)


def split_attachments(call: ModelCall, provider: str, can_read: set[str] | None = None) -> tuple[list["StoredFile"], str]:
    """Files this provider can read directly, plus prompt text for data files and ones it cannot open.
    `can_read` overrides the file kinds the provider's catalog entry says it reads."""
    from ..files import DIRECT_LIMIT
    from .catalog import reads

    can_read = reads(provider) if can_read is None else can_read

    media, notes = [], []
    for f in call.attachments:
        if f.kind == "other":
            notes.append(f"[{f.name} ({f.mime}, {f.size / 1024:.0f} KB) is attached, but it is a binary format no model can open. "
                         + (f.text or "Mention it if it matters.") + "]")
        elif f.kind == "text":
            notes.append(f'<file name="{f.name}" untrusted="true">\n{f.text}\n</file>')
        elif f.kind in can_read and f.size <= DIRECT_LIMIT.get(provider, {}).get(f.kind, f.size):
            media.append(f)
        elif f.kind in can_read:
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
    via: str = ""  # "vertex" (Google Cloud) or "key" (the provider's API key); empty when not known

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, retryable: bool = False):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable


_KEY_PROBLEMS = ("401", "403", "authentication", "unauthorized", "permission", "api key", "api_key", "invalid x-api-key",
                 "quota", "billing", "credit", "insufficient", "payment")


def is_key_problem(message: str) -> bool:
    """Errors that mean the key itself is bad or the account is out of credit, not a passing hiccup."""
    low = message.lower()
    return any(t in low for t in _KEY_PROBLEMS)


class ProviderAdapter(Protocol):
    provider: str  # a seat id from gateway/catalog.py, e.g. "claude", "gemini", "glm"
    display_name: str
    simulated: bool

    async def list_models(self) -> list[str]: ...

    async def resolve_model(self) -> str: ...

    async def generate(self, call: ModelCall) -> ModelResult: ...
