import gzip
import io
import zipfile

import pytest

from app import extract
from app.files import prepare


def archive(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, value in entries:
            z.writestr(name, value)
    return out.getvalue()


@pytest.mark.parametrize("name,member", [("note.docx", "word/document.xml"), ("slides.pptx", "ppt/slides/slide1.xml"),
                                      ("bundle.zip", "data.txt"), ("sheet.xlsx", "xl/sharedStrings.xml")])
def test_all_zip_document_readers_reject_expansion_before_parsing(monkeypatch, name, member):
    monkeypatch.setattr(extract, "MAX_INNER_BYTES", 4096)
    raw = archive([(member, "x" * 4097)])
    with pytest.raises(extract.ExtractionLimit, match="oversized"):
        extract.to_text(name, raw)
    saved = prepare(name, "", raw)
    assert saved.kind == "other" and "ExtractionLimit" in saved.text
    assert saved.data == raw, "unreadable attachment is preserved without pretending it was read"


def test_archive_count_is_checked_before_zipfile_allocates_members(monkeypatch):
    raw = archive([(f"item-{i}.txt", "x") for i in range(4)])
    monkeypatch.setattr(extract, "MAX_ARCHIVE_ENTRIES", 3)
    monkeypatch.setattr(extract.zipfile, "ZipFile", lambda *a, **k: pytest.fail("central directory allocated first"))
    with pytest.raises(extract.ExtractionLimit, match="too many entries"):
        extract.to_text("bundle.zip", raw)


def test_total_archive_expansion_is_bounded(monkeypatch):
    monkeypatch.setattr(extract, "MAX_ARCHIVE_BYTES", 100)
    with pytest.raises(extract.ExtractionLimit, match="expansion"):
        extract.to_text("bundle.zip", archive([("a.txt", "a" * 60), ("b.txt", "b" * 60)]))


def test_gzip_oversize_is_reported_not_silently_truncated(monkeypatch):
    monkeypatch.setattr(extract, "MAX_INNER_BYTES", 100)
    with pytest.raises(extract.ExtractionLimit, match="Gzip"):
        extract.to_text("data.csv.gz", gzip.compress(b"a,b\n" * 30))


def test_csv_profile_is_bounded_and_does_not_claim_complete_coverage(monkeypatch):
    monkeypatch.setattr(extract, "MAX_TABLE_ROWS", 2)
    text, _ = extract.to_text("data.csv", b"a,b\n1,2\n3,4\n1000000,2000000\n")
    assert "max 3" in text and "bounded sample" in text and "may omit data" in text
    assert "whole file" not in text


def test_plain_text_reports_byte_preview(monkeypatch):
    monkeypatch.setattr(extract, "MAX_TEXT_BYTES", 8)
    text, ok = extract.to_text("data.txt", b"abcdefghijkl")
    assert ok and text.startswith("abcdefgh") and "first 8 of 12 bytes" in text


def test_parquet_samples_rows_instead_of_loading_complete_table(monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.table({"value": list(range(300))}), buf)
    monkeypatch.setattr(pq, "read_table", lambda *a, **k: pytest.fail("unbounded table read"))
    text, _ = extract.to_text("data.parquet", buf.getvalue())
    assert "Rows: 300" in text and "first 200 rows" in text and "not the whole dataset" in text


def test_parquet_declared_expansion_limit(monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.table({"value": list(range(300))}), buf)
    monkeypatch.setattr(extract, "MAX_ARCHIVE_BYTES", 100)
    with pytest.raises(extract.ExtractionLimit, match="expansion"):
        extract.to_text("data.parquet", buf.getvalue())


def test_excel_preview_bounds_sparse_dimensions():
    import openpyxl

    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["value"])
    ws.append([3])
    ws.cell(100000, 1000, "outside preview")
    wb.save(buf)
    text, _ = extract.to_text("data.xlsx", buf.getvalue())
    assert "outside preview" not in text
    assert "Preview: first" in text and "formulas were not recalculated" in text
