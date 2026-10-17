"""Counter invariants and stage budgets; provider calls are never made here."""
import asyncio
from types import SimpleNamespace

from app.config import Settings
from app.files import StoredFile
from app.gateway.anthropic_adapter import ClaudeAdapter
from app.gateway.base import ModelCall, ModelResult, reply_room, split_attachments
from app.gateway.fit import fit, measure
from app.gateway.gemini_adapter import GeminiAdapter
from app.models import Run, Usage
from app.workflow import policy
from app.workflow.controller import Controller


def test_usage_subcategories_are_not_double_counted_and_survive_serialization():
    usage = Usage(calls=1)
    usage.record("claude", "task", 1200, 300, uncached_input_tokens=100,
                 cache_read_input_tokens=1000, cache_write_input_tokens=100, detailed=True)
    usage.record("gemini", "review", 200, 80, uncached_input_tokens=150,
                 cache_read_input_tokens=50, thinking_tokens=60, detailed=True)
    assert usage.total_tokens == 1780
    assert usage.unclassified_input_tokens == 0
    assert usage.by_stage["review"]["thinking_tokens"] == 60
    assert usage.by_provider["claude"]["cache_read_input_tokens"] == 1000
    assert Usage.model_validate_json(usage.model_dump_json()).total_tokens == 1780


def test_historical_unknown_and_failed_estimates_are_not_called_uncached_usage():
    usage = Usage(input_tokens=700, output_tokens=40)
    assert not usage.usage_details_available
    usage.record("claude", "review", 100, 200, estimated=True)
    assert usage.estimated_tokens == 300
    assert usage.unclassified_input_tokens == 800
    assert usage.uncached_input_tokens == 0
    assert usage.total_tokens == 1040


def test_provider_usage_recording_keeps_stage_and_reasoning_details():
    run = Run(request="Check the source")
    result = ModelResult("gemini", "test", "result", input_tokens=100, output_tokens=70,
                         uncached_input_tokens=40, cache_read_input_tokens=60,
                         thinking_tokens=50, usage_details_available=True)
    Controller._record_usage(run, "final_eval", result)
    assert run.usage.total_tokens == 170
    assert run.usage.by_stage["final_eval"]["thinking_tokens"] == 50


def test_review_allowance_is_smaller_without_limiting_deep_implementation():
    review = ModelCall("s", "p", "review", 2500)
    work = ModelCall("s", "p", "task", 64000, reasoning_effort="high")
    assert reply_room(review, 128000) == 6500
    assert reply_room(work, 128000) == 128000
    assert reply_room(work, 65536) == 65536
    assert Settings().max_tokens_per_run == Settings().eris_max_tokens_per_run == 5_000_000


def test_adaptive_avoids_full_debate_and_deep_can_still_compare():
    research = Run(request="Investigate conflicting evidence", profile="research")
    assert policy.allocate(research, 6, 6).analysts == 2
    assert not policy.allocate(research, 6, 6).cross_check
    deep = research.model_copy(update={"mode": "deep"})
    assert policy.allocate(deep, 6, 6).analysts == 6
    assert policy.allocate(deep, 6, 6).cross_check


def test_cache_blocks_keep_sources_separate_from_changing_instructions():
    adapter = ClaudeAdapter("not-a-real-key", model="test")
    file = StoredFile("f1", "rules.txt", "text/plain", "text", 3, b"abc", text="abc")
    first = ModelCall("role", "write first", "task", attachments=[file], rules="shared rules")
    second = ModelCall("role", "write second", "task", attachments=[file], rules="shared rules")
    a, b = adapter._content(first), adapter._content(second)
    assert a[0] == b[0] and a[-1] != b[-1]
    assert a[0]["cache_control"] == {"type": "ephemeral"}
    assert adapter._system(first)[0]["cache_control"]["ttl"] == "1h"
    asyncio.run(adapter._client.close())


def test_large_media_is_never_sent_as_empty_or_partial_inline_file():
    file = StoredFile("f1", "large.pdf", "application/pdf", "pdf", 2_000_000_000, b"%PDF")
    media, note = split_attachments(ModelCall("s", "p", "analysis", attachments=[file]), "claude")
    assert media == []
    assert "Do not claim to have viewed it" in note
    assert measure(ModelCall("s", "p", "analysis", attachments=[file]), {"pdf"}) < 1000
    assert Controller._estimate(Run(request="Inspect metadata"), [file]) < 1000


def test_final_review_never_silently_cuts_the_artifact_to_fit():
    full = "<deliverable>" + "original artifact " * 20000 + "</deliverable>"
    call = ModelCall("Review the exact artifact", full, "final_eval", 2500)
    fit(call, context=16000, reads=set(), reply=6500)
    assert call.prompt == full
    assert measure(call, set()) > 16000  # controller refuses rather than certifying an excerpt


def _streamed_gemini_result(usage):
    """A trailing metadata-only event must not erase usage or truncation state."""
    chunks = [SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(text="partial answer", thought=False)]), finish_reason="MAX_TOKENS")],
        usage_metadata=usage, model_version="reported-model"),
        SimpleNamespace(candidates=[], usage_metadata=None, model_version=None)]

    async def generate_stream(**kwargs):
        async def events():
            for chunk in chunks:
                yield chunk
        return events()

    adapter = GeminiAdapter.__new__(GeminiAdapter)
    adapter._model = "requested-model"
    adapter._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
        generate_content_stream=generate_stream)))
    emitted = []

    async def on_text(piece):
        emitted.append(piece)

    result = asyncio.run(adapter.generate(ModelCall("system", "task", "review", 2500, on_text=on_text)))
    assert emitted == ["partial answer"]
    assert result.model == "reported-model"
    assert result.stop_reason == "MAX_TOKENS"
    return result


def test_gemini_stream_preserves_usage_and_finish_before_empty_final_event():
    result = _streamed_gemini_result(SimpleNamespace(
        prompt_token_count=100, candidates_token_count=20,
        thoughts_token_count=30, cached_content_token_count=60))
    assert result.input_tokens == 100 and result.output_tokens == 50
    assert result.cache_read_input_tokens == 60 and result.uncached_input_tokens == 40
    assert result.thinking_tokens == 30
    assert result.usage_details_available and not result.usage_estimated


def test_gemini_stream_missing_usage_is_estimated_instead_of_free():
    result = _streamed_gemini_result(None)
    assert result.input_tokens > 0 and result.output_tokens == 6500
    assert result.usage_estimated and not result.usage_details_available
