"""Turn data and document files into text the models can read.

Parquet, Excel, Word, PowerPoint, JSON, ZIP and gzip files are converted here,
with explicit bounded sampling and coverage notes. Sampling is not a substitute
for querying the original dataset. Anything else that looks like text is
read as text; other binary files are kept with the run and described to the models.
"""
from __future__ import annotations

import csv
import gzip
import html
import io
import json
import re
import zipfile
from contextlib import contextmanager
from itertools import islice

TEXT_LIMIT = 150_000  # characters per data file; models with smaller context windows get a trimmed copy (gateway/fit.py)
MAX_INNER_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 512
MAX_PREVIEW_FILES = 20
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_TABLE_ROWS = 5000
MAX_COLUMNS = 80
MAX_CELLS = 100_000
MAX_CELL_CHARS = 2000
SAMPLE_ROWS = 200


class ExtractionLimit(ValueError):
    """The file exceeds the bounded preview reader's resource limits."""


@contextmanager
def _archive(data: bytes):
    # Inspect the bounded end record before ZipFile allocates every member's metadata.
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if end < 0 or end + 22 > len(data):
        raise ExtractionLimit("ZIP end record is missing or incomplete")
    count = int.from_bytes(data[end + 10:end + 12], "little")
    directory_bytes = int.from_bytes(data[end + 12:end + 16], "little")
    if count > MAX_ARCHIVE_ENTRIES or directory_bytes > MAX_INNER_BYTES:
        raise ExtractionLimit("Archive has too many entries or oversized metadata")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
            raise ExtractionLimit("Archive exceeds the entry or 32 MiB expansion limit")
        if any(i.file_size > MAX_INNER_BYTES or i.flag_bits & 1 for i in infos):
            raise ExtractionLimit("Archive contains an encrypted or oversized member (8 MiB maximum)")
        yield z


def _member(z: zipfile.ZipFile, name) -> bytes:
    with z.open(name) as source:
        data = source.read(MAX_INNER_BYTES + 1)
    if len(data) > MAX_INNER_BYTES:
        raise ExtractionLimit("Archive member exceeds the expansion limit")
    return data

TEXT_EXT = {
    ".csv", ".tsv", ".txt", ".md", ".markdown", ".json", ".jsonl", ".ndjson", ".geojson", ".xml", ".log", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env.example", ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go",
    ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".r", ".m", ".sh", ".bat", ".ps1", ".sql",
    ".html", ".htm", ".css", ".scss", ".svg", ".tex", ".bib", ".rst", ".srt", ".vtt", ".ipynb", ".graphql", ".proto",
    ".dockerfile", ".gitignore", ".tf", ".vue", ".svelte", ".dart", ".lua", ".pl", ".hs", ".ex", ".exs", ".clj", ".sas",
    ".do", ".stan", ".rmd", ".qmd", ".org", ".adoc", ".rtf",
}
EXCEL = {".xlsx", ".xlsm"}
CONVERTERS = {".parquet", ".pq", ".xlsx", ".xlsm", ".docx", ".pptx", ".zip", ".gz", ".json", ".jsonl", ".ndjson", ".geojson", ".ipynb"}


def ext_of(name: str) -> str:
    low = name.lower()
    dot = low.rfind(".")
    return low[dot:] if dot >= 0 else ""


def clip(text: str, limit: int = TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated: showing {limit:,} of {len(text):,} characters]"


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def looks_like_text(data: bytes) -> bool:
    head = data[:8192]
    if not head:
        return False
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return True
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        # A multi-byte character cut at the 8 KB boundary is still text.
        return e.start >= len(head) - 4


# ---------------------------------------------------------------- tables


def _num(v: str) -> float | None:
    try:
        return float(v.replace(",", "")) if v.strip() else None
    except ValueError:
        return None


def profile_rows(header: list[str], body: list[list]) -> str:
    """Per-column profile of a table: type, count, min, max, mean, distinct values."""
    lines = [f"Rows: {len(body)} (plus header). Columns: {len(header)}."]
    for i, col in enumerate(header[:80]):
        vals = ["" if i >= len(r) or r[i] is None else _cell(r[i]) for r in body]
        filled = [v for v in vals if v.strip()]
        nums = [n for n in (_num(v) for v in filled) if n is not None]
        if filled and len(nums) >= 0.9 * len(filled):
            mean = sum(nums) / len(nums)
            lines.append(f"- {col}: numeric, {len(filled)} values, min {min(nums):g}, max {max(nums):g}, mean {mean:.4g}, empty {len(vals) - len(filled)}")
        else:
            distinct = sorted(set(filled))
            sample = ", ".join(v[:40] for v in distinct[:5])
            lines.append(f"- {col}: text, {len(filled)} values, {len(distinct)} distinct (e.g. {sample[:160]}), empty {len(vals) - len(filled)}")
    if len(header) > 80:
        lines.append(f"- ... and {len(header) - 80} more columns")
    return "\n".join(lines)


def profile_table(text: str, delimiter: str) -> str:
    try:
        source = csv.reader(io.StringIO(text), delimiter=delimiter)
        header = next(source, [])
        limit = min(MAX_TABLE_ROWS, MAX_CELLS // max(1, min(len(header), MAX_COLUMNS)))
        rows = [[_cell(v) for v in row[:MAX_COLUMNS]] for row in islice(source, limit)]
    except csv.Error:
        return ""
    if not rows:
        return ""
    return f"Preview profile: at most {limit:,} rows and {MAX_COLUMNS} columns; may omit data.\n" + profile_rows(header[:MAX_COLUMNS], rows)


def _rows_to_csv(header: list, rows: list[list]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return out.getvalue()


def parquet_text(data: bytes) -> str:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    if len(data) < 12 or int.from_bytes(data[-8:-4], "little") > MAX_TEXT_BYTES:
        raise ExtractionLimit("Parquet metadata exceeds the preview limit")
    source = pq.ParquetFile(io.BytesIO(data), thrift_string_size_limit=MAX_TEXT_BYTES,
                            thrift_container_size_limit=100_000)
    meta = source.metadata
    if meta.num_row_groups > MAX_ARCHIVE_ENTRIES or meta.num_columns > 1024:
        raise ExtractionLimit("Parquet has too many row groups or columns")
    if sum(meta.row_group(i).total_byte_size for i in range(meta.num_row_groups)) > MAX_ARCHIVE_BYTES:
        raise ExtractionLimit("Parquet exceeds the 32 MiB declared expansion limit")
    columns = source.schema_arrow.names[:MAX_COLUMNS]
    batch = next(source.iter_batches(batch_size=SAMPLE_ROWS, columns=columns, use_threads=False), None)
    if batch is None:
        return f"Parquet table. Rows: {meta.num_rows:,}. Columns: {meta.num_columns}. No rows."
    table = pa.Table.from_batches([batch])
    lines = [f"Parquet table. Rows: {meta.num_rows:,}. Columns: {meta.num_columns}.",
             f"Preview statistics cover only the first {table.num_rows} rows and {len(columns)} columns; not the whole dataset."]
    for name, col in zip(table.column_names[:80], table.columns[:80]):
        t = col.type
        info = f"- {name} ({t}): nulls {col.null_count:,}"
        try:
            if pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t):
                mm = pc.min_max(col)
                mean = pc.mean(col).as_py()
                info += f", min {mm['min'].as_py()}, max {mm['max'].as_py()}, mean {mean:.4g}" if mean is not None else ""
            elif pa.types.is_temporal(t):
                mm = pc.min_max(col)
                info += f", from {mm['min'].as_py()} to {mm['max'].as_py()}"
            elif pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_boolean(t) or pa.types.is_dictionary(t):
                uniq = pc.unique(col)
                sample = ", ".join(str(v)[:40] for v in uniq.slice(0, 5).to_pylist())
                info += f", {len(uniq):,} distinct (e.g. {sample[:160]})"
        except Exception:  # noqa: BLE001 - a profile line is best effort
            pass
        lines.append(info)
    head = table.slice(0, SAMPLE_ROWS)
    rows = [[_cell(v) for v in r.values()] for r in head.to_pylist()]
    sample = _rows_to_csv(table.column_names, rows)
    return "\n".join(lines) + f"\n\nFirst {min(SAMPLE_ROWS, table.num_rows)} rows (CSV):\n{sample}"


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)[:500]
    return str(v)[:MAX_CELL_CHARS]


def excel_text(data: bytes) -> str:
    import openpyxl

    with _archive(data):
        pass  # Validate all workbook members before openpyxl expands shared strings/XML.
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    parts = [f"Excel workbook with {len(wb.sheetnames)} sheet(s): {', '.join(wb.sheetnames[:20])}"]
    try:
        for ws in wb.worksheets[:10]:
            columns = max(1, min(ws.max_column or MAX_COLUMNS, MAX_COLUMNS))
            limit = min(MAX_TABLE_ROWS, MAX_CELLS // columns)
            rows = []
            for row in ws.iter_rows(max_row=limit, max_col=columns, values_only=True):
                if any(c is not None for c in row):
                    rows.append([_cell(c) for c in row])
            if not rows:
                parts.append(f"\n## Sheet '{ws.title}': no nonempty rows in the bounded preview")
                continue
            header, body = rows[0], rows[1:]
            parts.append(clip(f"\n## Sheet '{ws.title}'\nPreview: first {limit} physical rows, {columns} columns; formulas use cached values.\n"
                              f"{profile_rows([str(h) for h in header], body)}\n\nFirst rows (CSV):\n"
                              + _rows_to_csv(header, body[:SAMPLE_ROWS]), TEXT_LIMIT // 10))
    finally:
        wb.close()
    parts.append("Only the first 10 sheets are inspected. Preview statistics may omit data; formulas were not recalculated.")
    return clip("\n".join(parts))


# ---------------------------------------------------------------- documents


def _xml_text(xml: str, para_tag: str, text_tag: str) -> str:
    paras = []
    for chunk in re.split(rf"</{para_tag}>", xml):
        words = re.findall(rf"<{text_tag}(?:\s[^>]*)?>([^<]*)</{text_tag}>", chunk)
        if words:
            paras.append(html.unescape("".join(words)))
    return "\n".join(paras)


def docx_text(data: bytes) -> str:
    with _archive(data) as z:
        xml = _member(z, "word/document.xml").decode("utf-8", errors="replace")
    return "Word document text:\n" + _xml_text(xml, "w:p", "w:t")


def pptx_text(data: bytes) -> str:
    with _archive(data) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = [f"PowerPoint with {len(slides)} slides."]
        for i, n in enumerate(slides[:100], 1):
            out.append(clip(f"\n## Slide {i}\n" + _xml_text(_member(z, n).decode("utf-8", errors="replace"), "a:p", "a:t"), TEXT_LIMIT // 10))
            if sum(map(len, out)) >= TEXT_LIMIT:
                break
        out.append("Text-only preview, limited to 100 slides and the text budget; figures and speaker notes are not inspected.")
    return clip("\n".join(out))


def json_text(text: str, name: str) -> str:
    """Describe the structure of a JSON or JSON Lines file, then include it."""
    ext = ext_of(name)
    try:
        if ext in (".jsonl", ".ndjson"):
            items = [json.loads(line) for line in text.splitlines()[:5000] if line.strip()]
            shape = f"JSON Lines: {len(text.splitlines()):,} lines."
            sample = items
        else:
            doc = json.loads(text)
            if ext == ".ipynb" and isinstance(doc, dict):
                cells = doc.get("cells") or []
                src = "\n\n".join(f"[{c.get('cell_type')}]\n" + "".join(c.get("source") or []) for c in cells)
                return f"Jupyter notebook with {len(cells)} cells.\n\n{src}"
            shape = f"JSON {type(doc).__name__}"
            if isinstance(doc, list):
                shape += f" with {len(doc):,} items."
                sample = doc
            elif isinstance(doc, dict):
                shape += f" with keys: {', '.join(list(doc)[:40])}."
                sample = next((v for v in doc.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
            else:
                sample = []
        objs = [o for o in sample[:2000] if isinstance(o, dict)]
        if objs:
            keys = list(dict.fromkeys(k for o in objs for k in o))[:80]
            shape += "\n" + profile_rows(keys, [[o.get(k) for k in keys] for o in objs])
        return f"{shape}\n\nContent:\n{text}"
    except (json.JSONDecodeError, RecursionError, ValueError):
        return text


def zip_text(data: bytes, depth: int = 0) -> str:
    with _archive(data) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        out = [f"ZIP archive with {len(infos)} files:"]
        out += [f"- {i.filename} ({i.file_size / 1024:.0f} KB)" for i in infos[:300]]
        budget = TEXT_LIMIT
        for i in infos[:MAX_PREVIEW_FILES]:
            if budget <= 2000 or depth > 0:
                break
            if i.file_size > MAX_INNER_BYTES or i.filename.startswith(("__MACOSX/", ".")):
                continue
            ext = ext_of(i.filename)
            if ext not in TEXT_EXT and ext not in CONVERTERS:
                continue
            inner = _member(z, i)
            text, ok = to_text(i.filename, inner, depth + 1)
            if ok:
                piece = clip(text, budget)
                out.append(f"\n<file name=\"{i.filename}\">\n{piece}\n</file>")
                budget -= len(piece)
        out.append(f"Archive preview inspects at most {MAX_PREVIEW_FILES} entries and the text budget; nested ZIP contents are not expanded.")
    return "\n".join(out)


# ---------------------------------------------------------------- entry point


def to_text(name: str, data: bytes, depth: int = 0) -> tuple[str, bool]:
    """Return (text for the models, readable). readable=False means the file is binary we cannot convert."""
    if depth > 2:
        raise ExtractionLimit("Nested archive depth exceeds the preview limit")
    ext = ext_of(name)
    if ext in (".parquet", ".pq") or data[:4] == b"PAR1":
        return parquet_text(data), True
    if ext in EXCEL:
        return excel_text(data), True
    if ext == ".docx":
        return docx_text(data), True
    if ext == ".pptx":
        return pptx_text(data), True
    if ext == ".zip":
        return zip_text(data, depth), True
    if ext == ".gz" and depth < 2:
        with gzip.open(io.BytesIO(data)) as fh:
            inner = fh.read(MAX_INNER_BYTES + 1)
        if len(inner) > MAX_INNER_BYTES:
            raise ExtractionLimit("Gzip expands beyond the 8 MiB preview limit")
        return to_text(name[:-3], inner, depth + 1)
    if ext in TEXT_EXT or looks_like_text(data):
        text = decode(data[:MAX_TEXT_BYTES])
        note = f"\n[Preview reads only the first {MAX_TEXT_BYTES:,} of {len(data):,} bytes.]" if len(data) > MAX_TEXT_BYTES else ""
        if ext in (".json", ".jsonl", ".ndjson", ".geojson", ".ipynb"):
            return clip(json_text(text, name)) + note, True
        if ext in (".csv", ".tsv"):
            profile = profile_table(text, "\t" if ext == ".tsv" else ",")
            if profile:
                return clip(f"Data preview (bounded sample):\n{profile}\n\n{text}") + note, True
        return clip(text) + note, True
    return "", False
