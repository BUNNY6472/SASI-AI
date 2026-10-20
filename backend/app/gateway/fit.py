"""Make every request fit the model it goes to.

Inputs are measured conservatively (about 3 characters per token, which over-counts plain English and
suits code and data). When a request is too long for a model's context window, the largest file texts are
trimmed first, keeping the start (which holds each data file's profile) and the end, with a note saying how
much was left out. If that is not enough, the middle of the prompt is trimmed, keeping the instructions at
both ends. Models with large context windows still get everything.
"""
from __future__ import annotations

import dataclasses
import re

from .base import ModelCall, split_attachments

CHARS_PER_TOKEN = 3.0
MARGIN = 4_000  # tokens kept free for the provider's own wrapping and miscounts
IMAGE_TOKENS = 1_600
PDF_BYTES_PER_TOKEN = 300
NOTE_TOKENS = 100  # the note that says what was left out


def tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _media_tokens(call: ModelCall, reads: set[str]) -> int:
    total = 0
    media, _ = split_attachments(call, "", can_read=reads)
    for f in media:
        if f.kind == "image" and "image" in reads:
            total += IMAGE_TOKENS
        elif f.kind == "pdf" and "pdf" in reads:
            total += f.size // PDF_BYTES_PER_TOKEN
    return total


def measure(call: ModelCall, reads: set[str]) -> int:
    _, text = split_attachments(call, "", can_read=reads)
    return tokens(call.full_system) + tokens(call.prompt) + tokens(text) + _media_tokens(call, reads)


def _cut(text: str, keep_chars: int, what: str) -> str:
    if len(text) <= keep_chars:
        return text
    head = int(keep_chars * 0.7)
    tail = keep_chars - head
    dropped = len(text) - head - tail
    return (f"{text[:head]}\n\n[... {dropped:,} characters of {what} left out here so it fits this model's context window. "
            "Omitted content was not included in this call; do not claim it was verified ...]\n\n"
            + (text[-tail:] if tail else ""))


def fit(call: ModelCall, context: int, reads: set[str], reply: int | None = None) -> str:
    """Trim the call in place so its input plus `reply` tokens of answer (thinking included) fits `context`.
    Returns a short note naming what was cut, or "" when nothing was."""
    budget = context - (reply if reply is not None else call.max_output_tokens) - MARGIN
    over = measure(call, reads) - budget
    if over <= 0:
        return ""
    notes = []
    # 1. The largest file texts first, never below a short head and tail.
    texts = sorted((f for f in call.attachments if f.kind in ("text", "other") and f.text), key=lambda f: -len(f.text))
    if texts:
        new_files = {f.id: f for f in call.attachments}
        for f in texts:
            if over <= 0:
                break
            have = tokens(f.text)
            keep = max(2_000, have - over - NOTE_TOKENS)
            if keep >= have:
                continue
            trimmed = dataclasses.replace(f, text=_cut(f.text, int(keep * CHARS_PER_TOKEN), f"'{f.name}'"))
            new_files[f.id] = trimmed
            over -= have - tokens(trimmed.text)
            notes.append(f.name)
        call.attachments = [new_files[f.id] for f in call.attachments]
    # 2. Then the middle of the prompt, where shared material sits; the request and the instructions stay.
    if over > 0 and not call.preserve_prompt and call.kind not in {"review", "plan_review", "final_eval"}:
        keep = max(4_000, tokens(call.prompt) - over - NOTE_TOKENS)
        cut = _cut(call.prompt, int(keep * CHARS_PER_TOKEN), "the shared material")
        if cut != call.prompt:
            call.prompt = cut
            notes.append("the shared material")
    return ", ".join(notes)


_LIMIT = re.compile(r"(?:context length|context window|maximum context|max(?:imum)? (?:input|prompt) tokens?)[^0-9]{0,40}\(?([0-9][0-9,]{3,})")
_TOO_LONG = ("context length", "context window", "too long", "maximum context", "prompt is too long",
             "input is too long", "exceeds the maximum", "too many tokens", "reduce the length")


def context_error(message: str) -> int | None:
    """If an error says the input was too long, the model's real limit when it states one (else 0). None otherwise."""
    low = message.lower()
    if not any(t in low for t in _TOO_LONG):
        return None
    m = _LIMIT.search(low)
    return int(m.group(1).replace(",", "")) if m else 0
