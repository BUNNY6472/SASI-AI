"""Google-grounded research with explicit provenance, separate from model opinion.

Search snippets are provider-grounded summaries, never quotations from a page SASI
fetched. Missing grounding metadata means an ungrounded response, not a source.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from urllib.parse import urlsplit

from .base import ModelResult, ProviderError


@dataclass
class ResearchResult:
    result: ModelResult
    evidence: list[dict] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    search_entry_html: str | None = None


class ResearchError(ProviderError):
    """An unusable response whose provider-reported usage must still be charged."""

    def __init__(self, message: str, result: ModelResult):
        super().__init__("gemini", message)
        self.result = result


def parse_research(response, model: str, *, max_sources: int = 6) -> ResearchResult:
    candidates = getattr(response, "candidates", None) or []
    candidate = candidates[0] if candidates else None
    metadata = getattr(candidate, "grounding_metadata", None)
    finish = str(getattr(candidate, "finish_reason", "") or "")
    usage = getattr(response, "usage_metadata", None)
    result = ModelResult(
        provider="gemini", model=getattr(response, "model_version", None) or model,
        text=(getattr(response, "text", None) or "")[:40_000], stop_reason=finish,
        input_tokens=(getattr(usage, "prompt_token_count", 0) or 0),
        output_tokens=(getattr(usage, "candidates_token_count", 0) or 0)
        + (getattr(usage, "thoughts_token_count", 0) or 0),
    )
    # Preserve usage even when the response must not become source evidence.
    if any(marker in finish.lower() for marker in ("max_tokens", "length", "safety", "recitation")):
        raise ResearchError(f"Research response was incomplete ({finish}).", result)
    chunks = getattr(metadata, "grounding_chunks", None) or []
    supports = getattr(metadata, "grounding_supports", None) or []
    snippets: dict[int, list[str]] = {}
    for support in supports:
        segment = getattr(support, "segment", None)
        text = (getattr(segment, "text", None) or "").strip()
        if text:
            for index in (getattr(support, "grounding_chunk_indices", None) or []):
                snippets.setdefault(index, []).append(text[:2000])
    evidence, seen = [], set()
    for index, chunk in enumerate(chunks):
        web = getattr(chunk, "web", None)
        url = (getattr(web, "uri", None) or "").strip()
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid or url in seen or len(url) > 4096:
            continue
        seen.add(url)
        excerpt = "\n".join(dict.fromkeys(snippets.get(index, [])))[:6000]
        evidence.append({
            "id": "search-" + hashlib.sha256(url.encode()).hexdigest()[:12],
            "url": url, "title": (getattr(web, "title", None) or url)[:500],
            "excerpt": excerpt, "retrieved_at": time.time(), "source_kind": "google_search",
            "requested_url": "", "content_hash": hashlib.sha256(excerpt.encode()).hexdigest() if excerpt else "",
        })
        if len(evidence) >= max(1, min(12, max_sources)):
            break
    entry = getattr(metadata, "search_entry_point", None)
    html = getattr(entry, "rendered_content", None)
    # The browser renders this provider markup in a sandbox without scripts.
    return ResearchResult(result=result, evidence=evidence,
                          queries=[str(q)[:1000] for q in (getattr(metadata, "web_search_queries", None) or [])[:20]],
                          search_entry_html=html[:100_000] if html else None)


async def search(adapter, request: str, *, max_sources: int = 6,
                 on_attempt: Callable[[], None] | None = None,
                 on_result: Callable[[ModelResult], None] | None = None) -> ResearchResult:
    """Use existing Gemini routes only; no extra credential or silent model fallback."""
    from .routes import RoutedAdapter

    if getattr(adapter, "simulated", False):
        raise ProviderError("gemini", "Web research is unavailable in simulation.")
    if isinstance(adapter, RoutedAdapter):
        routes = [route for route in adapter.routes if route.ok is not False]
    else:
        routes = [None]
    failures = []
    for route in routes:
        target = route.adapter if route else adapter
        method = getattr(target, "research", None)
        if not method:
            continue
        if on_attempt:
            on_attempt()  # budget rejection must propagate, never become another provider retry
        try:
            result = await method(request, max_sources=max_sources)
            result.result.via = route.via if route else ""
        except Exception as exc:
            # Grounding availability is separate from ordinary generation health.
            if isinstance(exc, ResearchError) and on_result:
                exc.result.via = route.via if route else ""
                on_result(exc.result)
            failures.append(str(exc)[:300])
        else:
            if on_result:
                on_result(result.result)
            return result
    raise ProviderError("gemini", "Grounded search unavailable" + (": " + "; ".join(failures) if failures else "."))
