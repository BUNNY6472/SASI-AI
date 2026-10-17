from __future__ import annotations

import anthropic

from .base import ModelCall, ModelResult, ProviderError


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

    async def generate(self, call: ModelCall) -> ModelResult:
        model = await self.resolve_model()
        try:
            resp = await self._client.beta.messages.create(
                model=model,
                max_tokens=call.max_output_tokens,
                system=call.system,
                messages=[{"role": "user", "content": call.prompt}],
                output_config={"effort": "medium"},
                # On a safety decline, let the API rerun on Anthropic's recommended fallback.
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},
            )
        except anthropic.RateLimitError as e:
            raise ProviderError(self.provider, f"rate limited: {e}", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(self.provider, f"HTTP {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(self.provider, f"connection error: {e}", retryable=True) from e

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
