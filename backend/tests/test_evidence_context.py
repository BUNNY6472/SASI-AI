"""Evidence selection keeps provenance and counterevidence while bounding repeated input."""
import json

import pytest

from app.models import SourceEvidence
from app.workflow.prompts import EVIDENCE_CONTEXT_CHARS, evidence_block


def records(block):
    return json.loads(block.split('<source_evidence untrusted="true">\n', 1)[1].split("\n</source_evidence>", 1)[0])


def source(sid, text, **kwargs):
    return SourceEvidence(id=sid, url=f"https://example.org/{sid}", title=f"Source {sid}",
                          retrieved_at=1234, excerpt=text, **kwargs)


def test_complete_small_evidence_and_empty_discovered_link_remain_distinct():
    items = [source("S1", "Measurement is 42.", truncated=True), source("S2", "", source_kind="google_search")]
    rows = records(evidence_block(items, "Measurement"))
    assert rows[0]["excerpt"] == "Measurement is 42."
    assert rows[0]["truncated"] is True and rows[0]["context_limited"] is False
    assert rows[1]["excerpt"] == "" and "same_excerpt_as" not in rows[1]
    assert rows[1]["source_kind"] == "google_search"
    assert rows[0]["retrieved_at"] == 1234


@pytest.mark.parametrize("kind", list(EVIDENCE_CONTEXT_CHARS))
def test_stage_excerpts_share_one_budget_and_originals_are_unchanged(kind):
    items = [source(f"S{i}", f"Article {i}.\n\n" + (f"Background discussion {i}. " * 1000)
                    + f"\n\nCalibration ZX71 observation {i} is 42.") for i in range(5)]
    saved = [item.model_dump() for item in items]
    rows = records(evidence_block(items, "What is calibration ZX71?", kind))
    assert sum(len(row["excerpt"]) for row in rows) <= EVIDENCE_CONTEXT_CHARS[kind]
    assert all("Calibration ZX71" in row["excerpt"] for row in rows)
    assert [row["id"] for row in rows] == [item.id for item in items]
    assert all(row["context_limited"] for row in rows)
    assert [item.model_dump() for item in items] == saved


def test_identical_sources_have_aliases_without_implying_independent_support():
    text = "A shared result that is mirrored by another site."
    block = evidence_block([source("S1", text), source("S2", text), source("S3", "Contrary result.")])
    rows = records(block)
    assert rows[0]["excerpt"] == text
    assert rows[1]["excerpt"] == "" and rows[1]["same_excerpt_as"] == "S1"
    assert rows[1]["url"] == "https://example.org/S2"
    assert rows[2]["excerpt"] == "Contrary result."
    assert "not independent corroboration" in block


def test_numeric_and_negative_counterevidence_is_not_deduplicated():
    rows = records(evidence_block([
        source("S1", "Treatment ZX71 improves the outcome by 7 percent."),
        source("S2", "Treatment ZX71 does not improve the outcome by 7 percent."),
        source("S3", "Treatment ZX71 improves the outcome by 2 percent."),
    ], "Treatment ZX71 outcome"))
    assert all(row["excerpt"] and "same_excerpt_as" not in row for row in rows)
    assert "not improve" in rows[1]["excerpt"] and "2 percent" in rows[2]["excerpt"]


def test_no_evidence_does_not_claim_browsing_and_budgeting_is_deterministic():
    assert "No external source evidence" in evidence_block([])
    items = [source("S1", "Unrelated long background.\n" * 2000)]
    first = evidence_block(items, "unmatched-identifier", "plan")
    assert first == evidence_block(items, "unmatched-identifier", "plan")
    assert "absence from this selection is not evidence of absence" in first
