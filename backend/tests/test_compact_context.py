"""Context efficiency must not turn sampled data into false full-data claims."""
import csv
import io
import json

from app import extract
from app.files import StoredFile, prepare
from app.workflow.context import ROLE_CONTEXT_CHARS, compact_attachments


def source(name, text, *, file_id="source-1"):
    data = text.encode()
    return StoredFile(id=file_id, name=name, mime="text/plain", kind="text", size=len(data), data=data, text=text)


def test_csv_small_complete_statistics_cover_quoted_newlines_and_missing_values():
    raw = b'id,value,note\n1,2,"line one\nline two"\n2,,missing\n3,999,late\n'
    text, ok = extract.to_text("values.csv", raw)
    assert ok
    assert "All data rows in the supplied file were inspected" in text
    assert "Rows profiled: 3" in text and "max 999" in text and "empty 1" in text
    assert "line one\nline two" in text
    assert "data row 3" in text
    assert text.count("id,value,note") == 1, "original table is not appended a second time"


def test_csv_late_outlier_is_explicitly_uninspected_when_row_limit_applies(monkeypatch):
    monkeypatch.setattr(extract, "MAX_TABLE_ROWS", 2)
    text, _ = extract.to_text("values.csv", b"value\n1\n2\n987654321\n")
    assert "max 2" in text and "987654321" not in text
    assert "may omit data, including late outliers" in text
    assert "All data rows" not in text


def test_partial_csv_excludes_unfinished_unquoted_record():
    text, _ = extract.to_text("values.csv", b"id,value\n1,10\n2,999999", complete=False, total_bytes=2_000_000_000)
    assert "Rows profiled: 1" in text and "999999" not in text
    assert "of 2,000,000,000 bytes" in text and "bounded sample/prefix" in text


def test_partial_csv_excludes_unfinished_quoted_multiline_record():
    text, _ = extract.to_text("values.csv", b'id,note\n1,ok\n2,"partial\nstillpartial\n',
                              complete=False, total_bytes=9999)
    assert "Rows profiled: 1" in text and "stillpartial" not in text
    assert "parsing stopped" in text


def test_complete_csv_final_record_does_not_need_a_newline():
    text, _ = extract.to_text("values.csv", b"id,value\n1,10\n2,999")
    assert "Rows profiled: 2" in text and "max 999" in text
    assert "All data rows" in text


def test_csv_nonfinite_values_do_not_poison_numeric_summary():
    text, _ = extract.to_text("values.csv", ("value\n" + "1\n" * 20 + "NaN\ninf\n\n").encode())
    assert "mean 1" in text and "other/nonfinite 2" in text and "empty 1" in text
    assert "mean nan" not in text and "max inf" not in text


def test_dataset_profile_reports_class_balance_and_distribution():
    text, _ = extract.to_text("cohort.csv", b"group,value\ncase,1\ncontrol,2\ncontrol,3\ncontrol,4\n")
    assert '"control":3' in text and '"case":1' in text
    assert "p10/p50/p90" in text


def test_csv_reports_columns_and_long_cell_truncation(monkeypatch):
    monkeypatch.setattr(extract, "MAX_COLUMNS", 2)
    monkeypatch.setattr(extract, "MAX_CELL_CHARS", 5)
    text, _ = extract.to_text("wide.csv", b"a,b,c\n1,longvalue,hidden\n")
    assert "profiled first 2" in text and "Additional columns are omitted" in text
    assert "Some cells are clipped" in text and "hidden" not in text


def test_empty_partial_csv_reports_no_inspection():
    text, _ = extract.to_text("data.csv", b"verylongheaderwithoutnewline", complete=False, total_bytes=9999)
    assert "no complete header" in text


def test_partial_unicode_preview_does_not_corrupt_earlier_complete_records():
    raw = 'id,note\n1,calibré\n2,é'.encode()
    text, _ = extract.to_text("data.csv", raw[:-1], complete=False, total_bytes=9999)
    assert "calibré" in text and "calibrÃ" not in text
    assert "Rows profiled: 1" in text


def test_large_json_table_has_bounded_examples_not_repeated_raw_rows():
    raw = json.dumps([{"id": i, "value": i * 2, "note": "descriptive value" * 8} for i in range(1500)]).encode()
    text, _ = extract.to_text("values.json", raw)
    assert extract.DATA_REPORT_MARKER in text and "Rows profiled: 1500" in text
    assert len(text) < 10_000 and len(text) < len(raw) // 10
    assert "Content:\n" not in text


def test_small_json_configuration_is_retained_exactly():
    raw = b'{"seed": 42, "forbidden": ["synthetic data"], "learning_rate": 0.001}'
    text, _ = extract.to_text("config.json", raw)
    assert raw.decode() in text and "Complete content:" in text


def test_partial_structured_binary_is_metadata_only():
    text, ok = extract.to_text("large.parquet", b"PAR1prefix", complete=False, total_bytes=1_000_000_000)
    assert not ok and "contents were not inspected" in text and "complete file" in text


def test_context_preserves_original_bytes_metadata_and_small_code():
    f = source("solution.py", "def solve(x):\n    return x * 2\n")
    result = compact_attachments([f], "final_eval", "Check correctness")
    assert result[0] is not f and result[0].data is f.data
    assert f.text == "def solve(x):\n    return x * 2\n" and f.text in result[0].text
    assert result[0].id == f.id and result[0].size == f.size


def test_context_repeats_partial_source_coverage_without_claiming_full_inspection():
    f = source("huge.csv", "SASI dataset report: sampled first 100 rows")
    f.preview_complete = False
    f.coverage_note = "Only the first 2 MiB are available to preview."
    f.size = 2_000_000_000
    output = compact_attachments([f], "analysis")[0].text
    assert "Only a prefix/preview of the original file was inspected" in output
    assert "Only the first 2 MiB" in output and "2,000,000,000 bytes" in output
    assert f.text == "SASI dataset report: sampled first 100 rows"


def test_context_is_bounded_deterministic_and_keeps_late_constraints_and_query_matches():
    text = "Introductory context.\n" + ("Unrelated general discussion. " * 4000)
    text += "\nThe quasar calibration MUST use only split validation; never inspect held-out labels.\n"
    text += "Unrelated general discussion. " * 4000
    f = source("research.txt", text)
    first = compact_attachments([f], "plan_review", "Audit quasar calibration and leakage")[0].text
    second = compact_attachments([f], "plan_review", "Audit quasar calibration and leakage")[0].text
    assert first == second and len(first) <= ROLE_CONTEXT_CHARS["plan_review"]
    assert "quasar calibration MUST use only split validation" in first
    assert "preview characters" in first and "Excerpts only" in first
    assert "not a complete inspection" in first


def test_context_total_cap_holds_for_many_files_and_all_sources_are_referenced():
    files = [source(f"source-{i}.txt", "background " * 20_000, file_id=f"f{i}") for i in range(40)]
    outputs = compact_attachments(files, "plan", "background")
    assert sum(len(f.text) for f in outputs) <= ROLE_CONTEXT_CHARS["plan"]
    assert len(outputs) == len(files)
    assert all(f.id in f.text for f in outputs)


def test_legacy_csv_attachment_is_compacted_without_mutating_stored_preview():
    raw = "value,note\n" + "12,ordinary\n" * 5000
    f = source("legacy.csv", raw)
    f.text = "Data preview (bounded sample):\nLegacy old summary\n\n" + raw
    result = compact_attachments([f], "analysis", "value distribution")[0]
    assert extract.DATA_REPORT_MARKER in result.text
    assert len(result.text) < len(f.text) // 10
    assert f.text.endswith(raw) and result.data is f.data


def test_native_media_is_not_silently_converted_or_removed():
    f = StoredFile(id="image", name="plot.png", mime="image/png", kind="image", size=3, data=b"png")
    assert compact_attachments([f], "analysis") == [f]


def test_dataset_prompt_size_reduction_is_observable_without_model_calls():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "measurement", "group", "description"])
    for i in range(5000):
        writer.writerow([i, (i % 100) / 10, "case" if i % 2 else "control", "Repeated observation of calibrated panel value."])
    raw = out.getvalue().encode()
    # Prior implementation attached a 150k-character raw-data preview on each call.
    previous_chars = min(150_000, len(raw))
    current = compact_attachments([prepare("cohort.csv", "text/csv", raw)], "analysis", "Compare group measurements")[0]
    assert len(current.text) < previous_chars // 10
    assert "max 9.9" in current.text and "Rows profiled: 5000" in current.text
