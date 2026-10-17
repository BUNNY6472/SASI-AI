from __future__ import annotations

import anthropic

from .base import ModelCall, ModelResult, ProviderError, split_attachments


class ClaudeAdapter:
    provider = "claude"
    display_name = "Claude"
    simulated = False

    def __init__(self, api_key: str, model: str = ""):
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
        self._pinned = model
        self._model: str | None = None

    async def list_models(self) -> list[str]:
        models = []
        async for m in self._client.models.list(limit=100):
            models.append(m.id)
        return models

    async def resolve_model(self) -> str:
        if self._model:
            return self._model
        if self._pinned:
            self._model = self._pinned
        else:
            # The Models API lists newest first.
            models = await self.list_models()
            if not models:
                raise ProviderError(self.provider, "no models available for this key")
            self._model = models[0]
        return self._model

    def _content(self, call: ModelCall) -> list[dict]:
        media, extra = split_attachments(call, self.provider)
        content: list[dict] = []
        for f in media:
            if f.kind == "image":
                content.append({"type": "image", "source": {"type": "base64", "media_type": f.mime, "data": f.b64}})
            elif f.kind == "pdf":
                content.append({"type": "document", "title": f.name,
                                "source": {"type": "base64", "media_type": "application/pdf", "data": f.b64}})
        content.append({"type": "text", "text": extra + call.prompt})
        return content

    async def generate(self, call: ModelCall) -> ModelResult:
        model = await self.resolve_model()
        kwargs = dict(
            model=model,
            max_tokens=call.max_output_tokens,
            system=call.system,
            messages=[{"role": "user", "content": self._content(call)}],
            output_config={"effort": "medium"},
            # On a safety decline, let the API rerun on Anthropic's recommended fallback.
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        )
        try:
            if call.on_text:
                async with self._client.beta.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        await call.on_text(text)
                    resp = await stream.get_final_message()
            else:
                resp = await self._client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise ProviderError(self.provider, f"rate limited: {e}", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(self.provider, f"HTTP {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(self.provider, f"connection error: {e}", retryable=True) from e
        except anthropic.APIError as e:  # e.g. a stream that broke part-way
            raise ProviderError(self.provider, f"API error: {e}", retryable=True) from e

        if resp.stop_reason == "refusal":
            raise ProviderError(self.provider, "the model declined this request")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return ModelResult(
            provider=self.provider,
            model=resp.model or model,
            text=text,
            input_tokens=resp.usage.input_tokens or 0,
            output_tokens=resp.usage.output_tokens or 0,
            stop_reason=resp.stop_reason or "",
        )
