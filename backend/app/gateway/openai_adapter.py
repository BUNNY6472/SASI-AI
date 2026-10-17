from __future__ import annotations

import re

import openai

from .base import ModelCall, ModelResult, ProviderError, split_attachments

# Chat-capable families; everything else (embeddings, audio, image, moderation) is skipped.
_CHAT = re.compile(r"^(gpt-\d|o\d)")
_SKIP = re.compile(r"(audio|realtime|transcribe|tts|image|search|embedding|moderation|instruct|codex|nano|mini)")


class ChatGPTAdapter:
    provider = "chatgpt"
    display_name = "ChatGPT"
    simulated = False

    def __init__(self, api_key: str, model: str = ""):
        self._client = openai.AsyncOpenAI(api_key=api_key, max_retries=2)
        self._pinned = model
        self._model: str | None = None

    async def list_models(self) -> list[str]:
        page = await self._client.models.list()
        items = sorted(page.data, key=lambda m: m.created, reverse=True)
        return [m.id for m in items]

    async def resolve_model(self) -> str:
        if self._model:
            return self._model
        if self._pinned:
            self._model = self._pinned
            return self._model
        # Newest general chat model the key can use. Pin OPENAI_MODEL to override.
        for mid in await self.list_models():
            if _CHAT.match(mid) and not _SKIP.search(mid):
                self._model = mid
                return mid
        raise ProviderError(self.provider, "no chat model available for this key")

    def _input(self, call: ModelCall) -> list[dict]:
        media, extra = split_attachments(call, self.provider)
        content: list[dict] = [{"type": "input_text", "text": extra + call.prompt}]
        for f in media:
            if f.kind == "image":
                content.append({"type": "input_image", "image_url": f"data:{f.mime};base64,{f.b64}"})
            elif f.kind == "pdf":
                content.append({"type": "input_file", "filename": f.name, "file_data": f"data:application/pdf;base64,{f.b64}"})
        return [{"role": "user", "content": content}]

    async def generate(self, call: ModelCall) -> ModelResult:
        model = await self.resolve_model()
        kwargs = dict(model=model, instructions=call.system, input=self._input(call),
                      max_output_tokens=call.max_output_tokens, store=False)
        try:
            if call.on_text:
                async with self._client.responses.stream(**kwargs) as stream:
                    async for event in stream:
                        if event.type == "response.output_text.delta" and event.delta:
                            await call.on_text(event.delta)
                    resp = await stream.get_final_response()
            else:
                resp = await self._client.responses.create(**kwargs)
        except openai.RateLimitError as e:
            raise ProviderError(self.provider, f"rate limited: {e}", retryable=True) from e
        except openai.APIStatusError as e:
            raise ProviderError(self.provider, f"HTTP {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
        except openai.APIConnectionError as e:
            raise ProviderError(self.provider, f"connection error: {e}", retryable=True) from e
        except openai.APIError as e:  # e.g. a stream that broke part-way
            raise ProviderError(self.provider, f"API error: {e}", retryable=True) from e

        usage = resp.usage
        return ModelResult(
            provider=self.provider,
            model=resp.model or model,
            text=resp.output_text or "",
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            stop_reason=resp.status or "",
        )
