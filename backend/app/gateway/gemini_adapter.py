from __future__ import annotations

import asyncio
import io
import re
import time

from google import genai
from google.genai import errors, types

from .base import ModelCall, ModelResult, ProviderError, split_attachments

_SKIP = re.compile(r"(embedding|aqa|imagen|veo|tts|audio|image|live|lite|nano|learnlm|gemma)")
INLINE_LIMIT = 14 * 1024 * 1024  # larger requests go through the Files API
FILE_TTL = 40 * 3600  # Gemini keeps uploaded files for 48 hours


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
        self._uploaded: dict[str, tuple[str, float]] = {}  # file id -> (uri, uploaded at)
        self._upload_lock = asyncio.Lock()

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

    async def _upload(self, f) -> str:
        """Upload a large file once through the Files API and wait until Gemini has processed it."""
        async with self._upload_lock:
            cached = self._uploaded.get(f.id)
            if cached and time.time() - cached[1] < FILE_TTL:
                return cached[0]
            up = await self._client.aio.files.upload(
                file=io.BytesIO(f.data), config=types.UploadFileConfig(mime_type=f.mime, display_name=f.name[:100]))
            for _ in range(90):  # video can take a minute or two to process
                if up.state == types.FileState.ACTIVE:
                    break
                if up.state == types.FileState.FAILED:
                    raise ProviderError(self.provider, f"Gemini could not process {f.name}")
                await asyncio.sleep(2)
                up = await self._client.aio.files.get(name=up.name)
            else:
                raise ProviderError(self.provider, f"Gemini took too long to process {f.name}")
            self._uploaded[f.id] = (up.uri, time.time())
            return up.uri

    async def _contents(self, call: ModelCall) -> list[types.Content]:
        media, extra = split_attachments(call, self.provider)
        parts: list[types.Part] = []
        large = sum(f.size for f in media) > INLINE_LIMIT
        for f in media:
            if large and f.size > 512 * 1024:
                parts.append(types.Part.from_uri(file_uri=await self._upload(f), mime_type=f.mime))
            else:
                parts.append(types.Part.from_bytes(data=f.data, mime_type=f.mime))
        parts.append(types.Part.from_text(text=extra + call.prompt))
        return [types.Content(role="user", parts=parts)]

    async def generate(self, call: ModelCall) -> ModelResult:
        model = await self.resolve_model()
        config = types.GenerateContentConfig(system_instruction=call.system, max_output_tokens=call.max_output_tokens)
        try:
            contents = await self._contents(call)
            if call.on_text:
                chunks: list[str] = []
                last = None
                async for chunk in await self._client.aio.models.generate_content_stream(
                        model=model, contents=contents, config=config):
                    piece = "".join(p.text or "" for c in (chunk.candidates or [])[:1]
                                    for p in ((c.content.parts if c.content else None) or []) if not p.thought)
                    if piece:
                        chunks.append(piece)
                        await call.on_text(piece)
                    last = chunk
                text = "".join(chunks)
                usage = last.usage_metadata if last else None
                version = (last.model_version if last else None) or model
                finish = str(last.candidates[0].finish_reason) if last and last.candidates else ""
            else:
                resp = await self._client.aio.models.generate_content(model=model, contents=contents, config=config)
                text = resp.text or ""
                usage = resp.usage_metadata
                version = resp.model_version or model
                finish = str(resp.candidates[0].finish_reason) if resp.candidates else ""
        except errors.APIError as e:
            code = getattr(e, "code", 0) or 0
            raise ProviderError(self.provider, f"HTTP {code}: {e.message}", retryable=code == 429 or code >= 500) from e

        return ModelResult(
            provider=self.provider,
            model=version,
            text=text,
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=(usage.candidates_token_count or 0) if usage else 0,
            stop_reason=finish,
        )
