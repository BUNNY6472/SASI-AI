"""CPU retrieval regression fixtures, not a claim about competition scores."""
import re

import pytest

from app.files import StoredFile
from app.workflow.context import (_chunks, _passage_scores, _terms, compact_attachments,
                                   select_source_excerpt)


def paragraph(label, words=180):
    return f"## {label}\n" + (f"{label} observation background. " * words) + "\n\n"


@pytest.mark.parametrize("limit", [0, 1, 10, 100, 400, 1000, 2000, 8000, 24000])
def test_excerpt_always_respects_its_hard_character_budget(limit):
    text = paragraph("Introduction") + paragraph("irrelevant") * 20 + "The calibration code is QX-42.\n"
    result = select_source_excerpt(text, "QX-42 calibration", limit)
    assert len(result) <= limit
    if limit == 0:
        assert result == ""


def test_small_inputs_remain_exact_including_code_whitespace_and_negation():
    text = 'if allowed:\n    print("not 42")\nelse:\n    print("43")\n'
    assert select_source_excerpt(text, "allowed", len(text)) == text


def test_rare_identifier_outweighs_repeated_common_query_words():
    text = ("calibration background observation. " * 1000
            + "\n\nDevice QX-42 calibration failed because its voltage exceeded 7 volts.\n\n"
            + "calibration background observation. " * 1000)
    chunks = _chunks(text)
    scores = _passage_scores(text, chunks, _terms("QX-42 calibration voltage"))
    best = chunks[max(range(len(scores)), key=lambda index: scores[index])]
    assert "QX-42" in text[best[0]:best[1]]
    result = select_source_excerpt(text, "QX-42 calibration voltage", 6000)
    assert "exceeded 7 volts" in result and "not a complete inspection" in result


def test_late_constraint_retained_even_without_matching_query_word():
    text = paragraph("Opening") + paragraph("filler") * 10
    text += "\n\nMandatory restriction: you MUST NOT use the held-out labels for tuning.\n\n"
    text += paragraph("quasar") + paragraph("filler") * 10
    result = select_source_excerpt(text, "quasar observations", 7000)
    assert "MUST NOT use the held-out labels for tuning" in result
    assert "quasar" in result


@pytest.mark.parametrize("budget", [1000, 1600, 2400])
def test_small_per_source_budget_still_selects_the_late_relevant_fact(budget):
    text = paragraph("Opening") + paragraph("unrelated") * 10
    text += "\n\nDevice QX-42 calibration failed above 7 volts.\n\n" + paragraph("unrelated") * 10
    result = select_source_excerpt(text, "QX-42 calibration voltage", budget)
    assert "Device QX-42 calibration failed above 7 volts" in result
    assert len(result) <= budget


def test_opposing_claims_and_changed_numbers_are_not_deduplicated():
    text = paragraph("Intro")
    text += "\n\nSensor QX-42 is safe at 7 volts.\n" + "measurement " * 150 + "\n\n"
    text += "Sensor QX-42 is not safe at 7 volts.\n" + "measurement " * 150 + "\n\n"
    text += "Sensor QX-42 is safe at 9 volts.\n" + "measurement " * 150 + "\n\n"
    text += paragraph("unrelated") * 10
    result = select_source_excerpt(text, "QX-42 safe voltage", 9000)
    assert "Sensor QX-42 is safe at 7 volts" in result
    assert "Sensor QX-42 is not safe at 7 volts" in result
    assert "Sensor QX-42 is safe at 9 volts" in result


def test_neighbor_expansion_keeps_a_boundary_qualifier():
    # Construct a single-line fixed-size boundary; the qualifier starts in the
    # next passage and shares no words with the retrieval query.
    first = "intro " * 200
    text = first + "x" * (1600 - len(first))
    middle = "QX-42 diagnostic calibration " + "detail " * 220
    text += middle[:1600].ljust(1600, "x")
    text += " HOWEVER THIS RESULT IS PRELIMINARY AND CANNOT ESTABLISH SAFETY. " + "filler " * 3000
    result = select_source_excerpt(text, "QX-42 diagnostic calibration", 6000)
    assert "HOWEVER THIS RESULT IS PRELIMINARY" in result


def test_no_match_fallback_is_explicit_and_contains_actual_ranges():
    text = "".join(paragraph(f"Section{i}", 80) for i in range(30))
    result = select_source_excerpt(text, "unfindable-zebra-identity", 7000)
    assert "No strong focused match; broad coverage fallback" in result
    assert "Section0" in result
    assert "Section29" in result, "fallback covers the end rather than merely filling the beginning"
    assert "Section14" in result or "Section15" in result, "fallback also covers the middle"
    assert len(re.findall(r"\[preview characters \d+-\d+", result)) >= 1


def test_source_range_blocks_are_exact_original_slices():
    text = paragraph("intro") + paragraph("biology") + paragraph("filler") * 10 + paragraph("enzyme")
    result = select_source_excerpt(text, "biology enzyme", 8000)
    blocks = list(re.finditer(r"\n\[preview characters (\d+)-(\d+)[^\]]*\]\n", result))
    assert blocks
    for index, block in enumerate(blocks):
        start, end = int(block.group(1)) - 1, int(block.group(2))
        expected = text[start:end]
        assert result[block.end():block.end() + len(expected)] == expected
        assert 0 <= start < end <= len(text)


def test_repeated_exact_passages_include_aliases_and_leave_room_for_unique_evidence():
    repeated = ("same background observation " * 100)[:1600]
    text = repeated * 20 + "\nUnique device QX-42 voltage is 7.5.\n" + repeated * 5
    result = select_source_excerpt(text, "QX-42 voltage", 8000)
    assert "QX-42 voltage is 7.5" in result
    assert "identical passages also at" in result
    assert len(result) < 6500


def test_identical_file_previews_keep_each_file_identity_and_original_coverage():
    text = "Important shared instructions.\n" * 100
    files = [StoredFile(f"f{i}", f"note{i}.txt", "text/plain", "text", len(text), text.encode(), text=text)
             for i in range(3)]
    result = compact_attachments(files, "analysis", "shared instructions")
    assert text in result[0].text
    assert 'source file ID "f0"' in result[1].text and 'Source file ID "f1"' in result[1].text
    assert 'Source file ID "f2"' in result[2].text
    assert "does not establish identical original files" in result[2].text
    assert all(original.text == text for original in files)
    assert sum(len(f.text) for f in result) < len(text) * 2


def test_identical_small_code_files_are_kept_in_full():
    text = "def example():\n    return 42\n"
    files = [StoredFile(f"f{i}", f"code{i}.py", "text/plain", "text", len(text), text.encode(), text=text)
             for i in range(2)]
    result = compact_attachments(files, "review", "Check the code")
    assert all(text in f.text for f in result)


def test_selection_is_deterministic_and_stops_after_relevant_context():
    text = paragraph("Introduction", 60) + paragraph("miscellaneous", 200) * 15
    text += "QX-42 calibration evidence: measured offset is 3.5 mm.\n"
    text += paragraph("miscellaneous", 200) * 15
    first = select_source_excerpt(text, "QX-42 calibration offset", 24000)
    assert first == select_source_excerpt(text, "QX-42 calibration offset", 24000)
    assert "measured offset is 3.5 mm" in first
    assert len(first) < 7000, "a context ceiling must not force unrelated passages into the prompt"
