"""Grounding provenance and workflow integration; no paid calls or external network."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from app.config import Settings
from app.files import StoredFile
from app.gateway.base import ModelResult, ProviderError
from app.gateway.research import (
    ResearchError, ResearchResult, needs_web_research, parse_research, search, web_research_opted_out,
)
from app.gateway.routes import Route, RoutedAdapter
from app.gateway.registry import ModelRegistry
from app.models import FinalResult, Judgement, Run
from app.store.memory import MemoryStore
from app.verification import check_citations
from app.workflow.controller import Controller
from app.workflow.router import PerformanceHistory


SMOKE_REQUEST = (
    "Deployment smoke check: calculate 6 multiplied by 7. Return the numeric answer with one short "
    "sentence explaining the arithmetic. No web research, code, or extra tasks are needed."
)


@pytest.mark.parametrize("prompt", [
    SMOKE_REQUEST,
    "Research my uploaded notes. Do not browse the web.",
    "Summarize current findings. Don't use external sources.",
    "Explain the latest design using my notes, without any web research.",
    "No research is needed. Calculate 6 times 7.",
    "Research isn't required; use the supplied text.",
    "Research isn’t necessary for this calculation.",
    "No need for further research. Use the attachment.",
    "Skip web search and explain current flow from the diagram.",
    "Answer with no-web access.",
])
def test_explicit_no_web_overrides_keywords_and_research_profile(prompt):
    assert web_research_opted_out(prompt)
    assert not needs_web_research(prompt)
    assert not needs_web_research(prompt, "research")


@pytest.mark.parametrize("prompt, profile", [
    ("Research current evidence with primary sources.", "general"),
    ("Browse official documentation for the latest release.", "general"),
    ("Compare the available treatments.", "research"),
    ("No research exists on this exact topic; search the broader literature.", "general"),
    ("Do not skip research. Check current sources.", "general"),
    ("Don't skip web search; find recent sources.", "general"),
])
def test_genuine_research_requests_still_search(prompt, profile):
    assert not web_research_opted_out(prompt)
    assert needs_web_research(prompt, profile)


def test_plain_arithmetic_and_eris_do_not_need_public_search():
    assert not needs_web_research("Calculate 6 multiplied by 7.")
    assert not needs_web_research("Research current evidence.", "research", eris=True)


def response(grounded=True, finish="STOP"):
    metadata = NS(
        grounding_chunks=[NS(web=NS(uri="https://example.org/paper", title="Original paper")),
                          NS(web=NS(uri="javascript:alert(1)", title="invalid")),
                          NS(web=NS(uri="https://example.org/other", title="Link without excerpt"))],
        grounding_supports=[NS(segment=NS(text="A provider-grounded summary."), grounding_chunk_indices=[0])],
        web_search_queries=["test research"], search_entry_point=NS(rendered_content="<div>Search suggestions</div>"),
    ) if grounded else None
    return NS(text="Evidence brief", model_version="test-gemini", candidates=[NS(finish_reason=finish, grounding_metadata=metadata)],
              usage_metadata=NS(prompt_token_count=100, candidates_token_count=50, thoughts_token_count=30))


def test_grounding_uses_actual_metadata_and_counts_reasoning_tokens():
    result = parse_research(response(), "test")
    assert result.result.total_tokens == 180
    assert len(result.evidence) == 2
    assert result.evidence[0]["source_kind"] == "google_search"
    assert result.evidence[0]["excerpt"] == "A provider-grounded summary."
    assert result.evidence[1]["excerpt"] == ""  # metadata never invents page text
    assert result.queries == ["test research"] and result.search_entry_html
    check = check_citations("[Paper](https://example.org/paper)", result.evidence)
    assert check["status"] == "pass" and "metadata" in check["summary"]


def test_missing_grounding_is_never_evidence():
    result = parse_research(response(False), "test")
    assert not result.evidence and not result.queries


def test_truncated_grounding_is_rejected():
    with pytest.raises(ResearchError, match="incomplete") as error:
        parse_research(response(finish="FinishReason.MAX_TOKENS"), "test")
    assert error.value.result.total_tokens == 180


def test_search_tries_existing_routes_without_disabling_normal_generation():
    async def scenario():
        class Unavailable:
            async def research(self, *args, **kwargs):
                raise ProviderError("gemini", "grounding unsupported")
        class Available:
            async def research(self, *args, **kwargs):
                return parse_research(response(), "test")
        adapter = RoutedAdapter("gemini", "Gemini", [Route("vertex", Unavailable(), ok=True), Route("key", Available(), ok=True)])
        result = await search(adapter, "Investigate this")
        assert result.result.via == "key" and adapter.routes[0].ok is True
    asyncio.run(scenario())


def make_controller():
    settings = Settings(_env_file=None, allow_simulated=True, simulated_delay=0)
    registry = ModelRegistry(settings)
    registry.adapters["gemini"] = NS(simulated=False)
    return Controller(settings, registry, MemoryStore(), PerformanceHistory())


@pytest.mark.parametrize("with_attachment", [False, True])
def test_no_web_workflow_skips_search_and_url_fetch_but_keeps_attachments(monkeypatch, with_attachment):
    from app.gateway import research
    from app.tools import evidence
    external_calls = []

    async def forbidden_fetch(*args, **kwargs):
        external_calls.append("fetch")
        raise AssertionError("No-web instruction must prevent URL retrieval")

    async def forbidden_search(*args, **kwargs):
        external_calls.append("search")
        raise AssertionError("No-web instruction must prevent grounded search")

    monkeypatch.setattr(evidence, "fetch_url", forbidden_fetch)
    monkeypatch.setattr(research, "search", forbidden_search)

    async def scenario():
        c = make_controller()
        attachment = StoredFile(id="notes", name="notes.txt", mime="text/plain", kind="text",
                                size=11, data=b"6 x 7 is 42", text="6 x 7 is 42")
        run = Run(request=SMOKE_REQUEST + " Reference: https://example.org/request",
                  profile="research", materials="Supplied notes: https://example.org/materials")
        await c._gather_evidence(run, [attachment] if with_attachment else [])
        assert external_calls == []
        assert run.usage.calls == 0 and run.usage.total_tokens == 0
        assert run.research_status == ("supplied_only" if with_attachment else "not_needed")
        assert not run.search_queries and not run.research_summary
        assert len(run.evidence) == int(with_attachment)
        if with_attachment:
            assert run.evidence[0].source_kind == "attachment"
            assert run.evidence[0].excerpt == "6 x 7 is 42"

    asyncio.run(scenario())


def test_workflow_accounts_grounding_and_does_not_resend_on_resume(monkeypatch):
    from app import tools
    from app.gateway import research
    calls = []
    async def collect(*args, **kwargs):
        return {"items": [], "issues": [], "searched": False}
    async def grounded(adapter, request, **kwargs):
        calls.append(request)
        kwargs["on_attempt"]()
        result = parse_research(response(), "test")
        kwargs["on_result"](result.result)
        return result
    monkeypatch.setattr(tools, "collect_evidence", collect)
    monkeypatch.setattr(research, "search", grounded)
    async def scenario():
        c = make_controller()
        run = Run(request="Research current evidence", profile="research", materials="private attachment content")
        await c._gather_evidence(run, [])
        await c._gather_evidence(run, [])
        assert calls == [run.request] and "private" not in calls[0]
        assert run.usage.calls == 1 and run.usage.total_tokens == 180
        assert run.research_status == "complete" and len(run.evidence) == 2
        assert c._token_reservations[run.id] == 0
    asyncio.run(scenario())


def test_eris_never_searches_or_fetches_request_links(monkeypatch):
    from app import tools
    seen = []
    async def collect(request, materials, files, **kwargs):
        seen.append((request, materials))
        return {"items": [], "issues": [], "searched": False}
    monkeypatch.setattr(tools, "collect_evidence", collect)
    async def scenario():
        c = make_controller()
        run = Run(request="Research https://example.org", materials="https://other.example", eris=True)
        await c._gather_evidence(run, [])
        assert seen == [("", "")] and run.usage.calls == 0
    asyncio.run(scenario())


def test_ungrounded_text_is_not_shown_as_research_summary(monkeypatch):
    from app import tools
    from app.gateway import research
    async def collect(*args, **kwargs):
        return {"items": [], "issues": [], "searched": False}
    async def ungrounded(*args, **kwargs):
        kwargs["on_attempt"]()
        result = ResearchResult(ModelResult(provider="gemini", model="fake", text="Unsupported factual claims"))
        kwargs["on_result"](result.result)
        return result
    monkeypatch.setattr(tools, "collect_evidence", collect)
    monkeypatch.setattr(research, "search", ungrounded)
    async def scenario():
        c = make_controller()
        run = Run(request="Research current facts", profile="research")
        await c._gather_evidence(run, [])
        assert run.research_status == "partial" and not run.research_summary and not run.evidence
    asyncio.run(scenario())


def test_actual_final_checks_find_syntax_failure_after_a_positive_review():
    async def scenario():
        c = make_controller()
        run = Run(request="Write Python", profile="software", final=FinalResult(
            integrator="claude", output="```python solution.py\ndef broken(:\n    x = 1\n    return x\n```"))
        c._stamp_final(run.final)
        run.final.judges = [Judgement(judge="gemini", meets_criteria=True, score=9, artifact_hash=run.final.artifact_hash)]
        await c._verify_final(run)
        assert run.final.verification_status == "failed"
        assert next(c for c in run.final.checks if c.kind == "python_syntax").status == "fail"
        assert next(c for c in run.final.checks if c.kind == "execution").status == "unknown"
    asyncio.run(scenario())


def test_grounding_fallback_charges_each_attempt_and_truncated_usage(monkeypatch):
    from app import tools
    async def collect(*args, **kwargs):
        return {"items": [], "issues": [], "searched": False}
    monkeypatch.setattr(tools, "collect_evidence", collect)
    async def scenario():
        class Truncated:
            async def research(self, *args, **kwargs):
                return parse_research(response(finish="MAX_TOKENS"), "test")
        class Good:
            async def research(self, *args, **kwargs):
                return parse_research(response(), "test")
        c = make_controller()
        c.registry.adapters["gemini"] = RoutedAdapter("gemini", "Gemini", [Route("vertex", Truncated(), ok=True), Route("key", Good(), ok=True)])
        run = Run(request="Research current facts", profile="research")
        await c._gather_evidence(run, [])
        assert run.research_status == "complete"
        assert run.usage.calls == run.usage.by_provider["gemini"]["calls"] == 2
        assert run.usage.total_tokens == 360
        assert len(run.evidence) == 2
    asyncio.run(scenario())
