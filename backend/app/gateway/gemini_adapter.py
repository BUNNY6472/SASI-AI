from __future__ import annotations

import re

from google import genai
from google.genai import errors, types

from .base import ModelCall, ModelResult, ProviderError

_SKIP = re.compile(r"(embedding|aqa|imagen|veo|tts|audio|image|live|lite|nano|learnlm|gemma)")


def _version_key(name: str) -> tuple:
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", name)]
    pro = 1 if "pro" in name else 0
    preview = 0 if ("preview" in name or "exp" in name) else 1
    return (nums[:1] or [0], pro, preview, name)


class GeminiAdapter:
    provider = "gemini"
    display_name = "Gemini"
    simulated = False

    def __init__(self, api_key: str, model: str = ""):
        self._client = genai.Client(api_key=api_key)
        self._pinned = model
        self._model: str | None = None

    async def list_models(self) -> list[str]:
        names = []
        pager = await self._client.aio.models.list()
        async for m in pager:
            actions = m.supported_actions or []
            if "generateContent" in actions:
                names.append((m.name or "").removeprefix("models/"))
        return names

    async def resolve_model(self) -> str:
        if self._model:
            return self._model
        if self._pinned:
            self._model = self._pinned
            return self._model
        # Highest-version "pro" text model the key can use. Pin GEMINI_MODEL to override.
        candidates = [n for n in await self.list_models() if n.startswith("gemini") and not _SKIP.search(n)]
        if not candidates:
            raise ProviderError(self.provider, "no text model available for this key")
        self._model = sorted(candidates, key=_version_key, reverse=True)[0]
        return self._model

    async def generate(self, call: ModelCall) -> ModelResult:
        model = await self.resolve_model()
        try:
            resp = await self._client.aio.models.generate_content(
                model=model,
                contents=call.prompt,
                config=types.GenerateContentConfig(
                    system_instruction=call.system,
                    max_output_tokens=call.max_output_tokens,
                ),
            )
        except errors.APIError as e:
            code = getattr(e, "code", 0) or 0
            raise ProviderError(self.provider, f"HTTP {code}: {e.message}", retryable=code == 429 or code >= 500) from e

        usage = resp.usage_metadata
        return ModelResult(
            provider=self.provider,
            model=resp.model_version or model,
            text=resp.text or "",
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=(usage.candidates_token_count or 0) if usage else 0,
            stop_reason=str(resp.candidates[0].finish_reason) if resp.candidates else "",
        )
