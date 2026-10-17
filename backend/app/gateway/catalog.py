"""The models SASI can put on a team.

Frontier models come first (Claude and Gemini, through Vertex AI or their own API key). Open models served
by Vertex AI fill the remaining seats, chosen per run by how well they suit the task and whether the
input fits their context window. Every Vertex model is billed to the Google Cloud project.

Context and output sizes are deliberately conservative. If a model still reports that an input is too
long, SASI lowers that model's context size and trims the input again (see gateway/fit.py).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Seat:
    id: str
    name: str
    tier: str  # "frontier" | "open"
    context: int  # tokens the model reads in one request
    max_output: int  # tokens it may write in one reply, thinking included
    strengths: tuple[str, ...]  # "code", "reasoning", "long", "vision"
    reads: frozenset[str]  # file kinds it can open directly; everything else reaches it as text or notes
    vertex_models: tuple[str, ...] = ()  # model IDs on Vertex AI, newest first; the first one the project can call is used
    locations: tuple[str, ...] = ("global",)
    vision_models: tuple[str, ...] = ()  # which of vertex_models read images; the older ones get text only
    key: str = ""  # "anthropic" or "gemini" when the provider's own API key is another route
    about: str = ""
    retirement_date: str | None = None  # YYYY-MM-DD; unavailable from this date in UTC


SEATS: dict[str, Seat] = {s.id: s for s in [
    Seat("claude", "Claude", "frontier", 1_000_000, 128_000, ("code", "reasoning", "long", "vision"),
         frozenset({"image", "pdf", "text"}), key="anthropic", about="Anthropic's frontier model"),
    Seat("gemini", "Gemini", "frontier", 1_000_000, 65_536, ("code", "reasoning", "long", "vision", "video"),
         frozenset({"image", "pdf", "text", "video", "audio"}), key="gemini", about="Google's frontier model"),
    # Open models on Vertex AI, best first.
    # GLM 5.2 and DeepSeek V4.1 Flash read 1M tokens (their Model Garden cards). If an older fallback model is
    # used, its smaller window is learned from its first "too long" error.
    Seat("glm", "GLM", "open", 1_000_000, 32_000, ("code", "reasoning", "agentic", "long"), frozenset({"text"}),
         ("zai-org/glm-5.2-maas",), ("global",),
         about="Z.ai GLM 5.2, a leading open model for coding and long agentic work, with a 1M-token context"),
    Seat("deepseek", "DeepSeek", "open", 1_000_000, 32_000, ("code", "reasoning", "long", "vision"),
         frozenset({"image", "text"}),
         ("deepseek-ai/deepseek-v4.1-flash-maas",),
         ("global",), vision_models=("deepseek-ai/deepseek-v4.1-flash-maas",),
         about="DeepSeek V4.1 Flash, the newest DeepSeek model, with a 1M-token context; it also reads images"),
    Seat("qwen", "Qwen", "open", 262_144, 16_384, ("code",), frozenset({"text"}),
         ("qwen/qwen3-coder-480b-a35b-instruct-maas", "qwen/qwen3-235b-a22b-instruct-2507-maas"), ("global", "us-south1"),
         about="Qwen3 Coder, a strong open coding model", retirement_date="2026-10-21"),
    Seat("kimi", "Kimi", "open", 262_144, 32_000, ("reasoning", "agentic"), frozenset({"text"}),
         ("moonshotai/kimi-k2-thinking-maas",), ("global",), about="Kimi K2 Thinking, an open reasoning model",
         retirement_date="2026-10-21"),
    Seat("minimax", "MiniMax", "open", 196_608, 32_000, ("code", "agentic"), frozenset({"text"}),
         ("minimaxai/minimax-m2-maas",), ("global",), about="MiniMax M2, an open agentic coding model",
         retirement_date="2026-10-21"),
    Seat("gptoss", "gpt-oss", "open", 131_072, 32_000, ("reasoning",), frozenset({"text"}),
         ("openai/gpt-oss-120b-maas",), ("global", "us-central1"), about="OpenAI's open-weight gpt-oss-120b"),
]}

# Old runs may name models that are no longer on the team.
NAMES = {s.id: s.name for s in SEATS.values()} | {"chatgpt": "ChatGPT"}


def name(seat_id: str) -> str:
    return NAMES.get(seat_id, seat_id)


def seat(seat_id: str) -> Seat | None:
    return SEATS.get(seat_id)


def reads(seat_id: str) -> set[str]:
    s = SEATS.get(seat_id)
    return set(s.reads) if s else set()


# What each kind of work needs most, for choosing open models.
NEEDS = {"eris": "code", "software": "code", "research": "reasoning", "general": "reasoning"}


def rank_open(candidates: list[str], profile: str, input_tokens: int) -> list[str]:
    """Open models best suited to this run first: inputs that fit whole, then the right strength, then catalog order."""
    need = NEEDS.get(profile, "reasoning")
    order = list(SEATS)

    def key(sid: str) -> tuple:
        s = SEATS[sid]
        fits = input_tokens + 8_000 <= s.context
        return (0 if fits else 1, 0 if need in s.strengths else 1, order.index(sid))

    return sorted((c for c in candidates if c in SEATS and SEATS[c].tier == "open"), key=key)
