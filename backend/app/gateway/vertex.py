"""Models through Google Cloud Vertex AI: Claude, Gemini and the open models in the catalog.

The app's own service account signs every call, so no API keys are needed and usage is billed to the
Google Cloud project. Vertex has no model list for partner and open models, so each adapter tries a short
list of model IDs, newest first, and keeps the first one this project can call. Claude and Gemini are
checked by counting tokens, which is free; if that is not offered where the model runs, a reply of a few
tokens settles it. Open models are asked for a few tokens.
"""
from __future__ import annotations

import asyncio

import anthropic
import openai
from google import genai
from google.genai import errors, types

from .anthropic_adapter import ClaudeAdapter
from .base import ModelCall, ModelResult, ProviderError, no_text, reply_room, split_attachments
from .fit import context_error
from .gemini_adapter import INLINE_LIMIT, GeminiAdapter

CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

GARDEN = "console.cloud.google.com/vertex-ai/model-garden"


def _hint(provider: str) -> str:
    if provider == "gemini":
        return "Check that the Vertex AI API is on for this project"
    from .catalog import name

    return f"Enable {name(provider)} for this project in Vertex AI Model Garden ({GARDEN})"


def _unique(models: list[str]) -> list[str]:
    return [m for m in dict.fromkeys(m.strip() for m in models) if m]


def _not_found(provider: str, tried: list[str], detail: str) -> ProviderError:
    return ProviderError(provider, f"HTTP 404: none of {', '.join(tried)} can be used by this project on Vertex AI. "
                                   f"{_hint(provider)}. ({detail[:160]})")


class VertexClaudeAdapter(ClaudeAdapter):
    """Claude on Vertex AI: same requests, no Models API and no server-side fallback."""

    def __init__(self, project: str, location: str, models: list[str]):
        extra = {"project_id": project} if project else {}
        self._client = anthropic.AsyncAnthropicVertex(region=location, max_retries=2, **extra)
        self._candidates = _unique(models)
        self._pinned = ""
        self._model: str | None = None

    async def list_models(self) -> list[str]:
        detail = ""
        hello = [{"role": "user", "content": "Say OK."}]
        for m in self._candidates:
            try:
                try:
                    await self._client.messages.count_tokens(model=m, messages=hello)
                except anthropic.APIStatusError as e:
                    if e.status_code in (401, 403, 429):
                        raise
                    # Token counting may not be offered for this model or location: a tiny reply tells for sure.
                    await self._client.messages.create(model=m, max_tokens=64, messages=hello, output_config={"effort": "low"})
            except anthropic.NotFoundError as e:
                detail = e.message
                continue
            except anthropic.APIStatusError as e:
                raise ProviderError(self.provider, f"HTTP {e.status_code}: {e.message}") from e
            self._model = m
            return [m]
        raise _not_found(self.provider, self._candidates, detail)

    async def resolve_model(self) -> str:
        return self._model or (await self.list_models())[0]

    def _kwargs(self, call: ModelCall, model: str) -> dict:
        kwargs = super()._kwargs(call, model)
        # Server-side fallback is not offered on Vertex. A decline falls over to the API key route when one is saved.
        kwargs.pop("betas", None)
        kwargs.pop("extra_body", None)
        return kwargs

    @property
    def _messages(self):
        return self._client.messages


class VertexGeminiAdapter(GeminiAdapter):
    """Gemini on Vertex AI. Vertex has no Files API, so files go inline up to the request limit."""

    def __init__(self, project: str, location: str, models: list[str]):
        self._candidates = _unique(models)
        self._pinned = ""
        self._model: str | None = None
        self._uploaded = {}
        self._upload_lock = asyncio.Lock()
        self._init_error = ""
        try:
            self._client = genai.Client(vertexai=True, project=project or None, location=location)
        except Exception as e:  # noqa: BLE001 - e.g. no Google credentials on this machine
            self._client = None
            self._init_error = f"{type(e).__name__}: {e}"[:300]

    async def list_models(self) -> list[str]:
        if self._client is None:
            raise ProviderError(self.provider, f"could not reach Vertex AI: {self._init_error}")
        detail = ""
        for m in self._candidates:
            try:
                try:
                    await self._client.aio.models.count_tokens(model=m, contents="hi")
                except errors.APIError as e:
                    if (getattr(e, "code", 0) or 0) in (401, 403, 429):
                        raise
                    await self._client.aio.models.generate_content(
                        model=m, contents="Say OK.", config=types.GenerateContentConfig(max_output_tokens=64))
            except errors.APIError as e:
                if (getattr(e, "code", 0) or 0) == 404:
                    detail = e.message or ""
                    continue
                raise ProviderError(self.provider, f"HTTP {e.code}: {e.message}") from e
            self._model = m
            return [m]
        raise _not_found(self.provider, self._candidates, detail)

    async def resolve_model(self) -> str:
        return self._model or (await self.list_models())[0]

    async def _contents(self, call: ModelCall) -> list[types.Content]:
        media, extra = split_attachments(call, self.provider)
        parts: list[types.Part] = []
        notes: list[str] = []
        used = 0
        for f in media:
            if used + f.size <= INLINE_LIMIT:
                parts.append(types.Part.from_bytes(data=f.data, mime_type=f.mime))
                used += f.size
            else:
                notes.append(f"[{f.name} is too large to send to Gemini through Google Cloud with the other files. "
                             "Use the other models' analyses of it.]")
        note = ("\n\n".join(notes) + "\n\n") if notes else ""
        parts.append(types.Part.from_text(text=note + extra + call.prompt))
        return [types.Content(role="user", parts=parts)]


class VertexOpenModelAdapter:
    """An open model on Vertex AI (GLM, DeepSeek, Qwen, Kimi, ...), through Vertex's OpenAI-compatible
    Chat Completions endpoint, signed with a Google access token. Models listed in `vision_models` get images
    inline; every other file reaches them as text, or as notes pointing to the other models' analyses.
    """

    simulated = False

    def __init__(self, provider: str, display_name: str, project: str, models: list[str], locations: list[str],
                 max_output: int = 32_000, vision_models: list[str] | None = None):
        self.provider = provider
        self._vision = set(vision_models or [])
        self._images_ok = True  # turned off if the endpoint refuses images
        self.display_name = display_name
        self._project = project
        # Every model ID in every location, newest model first.
        self._candidates = [(m, loc) for m in _unique(models) for loc in _unique(locations)]
        self._max_output = max_output
        self._model: str | None = None
        self._location: str | None = None
        self._creds = None
        self._lock = asyncio.Lock()
        self._clients: dict[str, openai.AsyncOpenAI] = {}

    async def _api(self, location: str) -> openai.AsyncOpenAI:
        """A client for one location, signed with a fresh Google access token (tokens last an hour)."""
        import google.auth
        import google.auth.transport.requests

        async with self._lock:
            if self._creds is None:
                self._creds, found = await asyncio.to_thread(google.auth.default, scopes=[CLOUD_SCOPE])
                self._project = self._project or found or ""
            if not self._creds.valid:
                await asyncio.to_thread(self._creds.refresh, google.auth.transport.requests.Request())
            if not self._project:
                raise ProviderError(self.provider, "no Google Cloud project is set (GCP_PROJECT)")
            if location not in self._clients:
                host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
                self._clients[location] = openai.AsyncOpenAI(
                    base_url=f"https://{host}/v1/projects/{self._project}/locations/{location}/endpoints/openapi",
                    api_key="unused", max_retries=2)
            return self._clients[location].with_options(api_key=self._creds.token)

    async def _signed(self, location: str) -> openai.AsyncOpenAI:
        try:
            return await self._api(location)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - missing or unusable Google credentials
            raise ProviderError(self.provider, f"could not sign in to Google Cloud: {type(e).__name__}: {e}") from e

    async def list_models(self) -> list[str]:
        detail = ""
        for m, loc in self._candidates:
            client = await self._signed(loc)
            try:
                await client.chat.completions.create(model=m, messages=[{"role": "user", "content": "Say OK."}], max_tokens=16)
            except (openai.NotFoundError, openai.BadRequestError) as e:
                if isinstance(e, openai.BadRequestError) and "not found" not in str(e).lower() and "not supported" not in str(e).lower():
                    raise ProviderError(self.provider, f"HTTP {e.status_code}: {e.message}") from e
                detail = e.message
                continue
            except openai.APIStatusError as e:
                raise ProviderError(self.provider, f"HTTP {e.status_code}: {e.message}") from e
            self._model, self._location = m, loc
            return [m]
        raise _not_found(self.provider, _unique([m for m, _ in self._candidates]), detail)

    async def resolve_model(self) -> str:
        return self._model or (await self.list_models())[0]

    def _messages(self, call: ModelCall, images: bool) -> tuple[list[dict], str, bool]:
        """The request messages, the file notes added to the prompt, and whether any image went inline."""
        media, extra = split_attachments(call, self.provider, None if images else set())
        user: str | list[dict] = extra + call.prompt
        if media:
            user = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": f"data:{f.mime};base64,{f.b64}"}} for f in media]
        return [{"role": "system", "content": call.full_system}, {"role": "user", "content": user}], extra, bool(media)

    async def generate(self, call: ModelCall) -> ModelResult:
        model = await self.resolve_model()
        messages, extra, sent_images = self._messages(call, model in self._vision and self._images_ok)
        # Reasoning counts toward max_tokens, so leave room for the answer.
        kwargs = dict(model=model, messages=messages, max_tokens=reply_room(call, self._max_output))
        text, usage, finish, served = "", None, "", model
        client = await self._signed(self._location or "global")
        try:
            pieces: list[str] = []
            try:
                stream = await client.chat.completions.create(**kwargs, stream=True)
            except openai.BadRequestError as e:
                if not sent_images or context_error(str(e.message)) is not None:
                    raise
                # The endpoint would not take the images: if text alone works, send text only from now on.
                kwargs["messages"], extra, _ = self._messages(call, False)
                stream = await client.chat.completions.create(**kwargs, stream=True)
                self._images_ok = False
            async for chunk in stream:
                served = chunk.model or served
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                for choice in chunk.choices or []:
                    piece = choice.delta.content if choice.delta else None
                    if piece:
                        pieces.append(piece)
                        if call.on_text:
                            await call.on_text(piece)
                    if choice.finish_reason:
                        finish = choice.finish_reason
            text = "".join(pieces)
        except openai.RateLimitError as e:
            raise ProviderError(self.provider, f"rate limited: {e}", retryable=True) from e
        except openai.APIStatusError as e:
            raise ProviderError(self.provider, f"HTTP {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
        except openai.APIConnectionError as e:
            raise ProviderError(self.provider, f"connection error: {e}", retryable=True) from e
        except openai.APIError as e:
            raise ProviderError(self.provider, f"API error: {e}", retryable=True) from e

        if not text.strip():
            raise no_text(self.provider, finish)
        if usage:
            tokens_in, tokens_out = usage.prompt_tokens or 0, usage.completion_tokens or 0
        else:  # the stream did not report usage: estimate it
            tokens_in = (len(call.full_system) + len(extra) + len(call.prompt)) // 4
            tokens_out = len(text) // 4
        return ModelResult(provider=self.provider, model=served, text=text, input_tokens=tokens_in,
                           output_tokens=tokens_out, stop_reason=finish)
