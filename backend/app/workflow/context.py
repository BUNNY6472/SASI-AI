"""Deterministic, bounded attachment context for each workflow role.

Original bytes and stored previews are never mutated. These excerpts are a
navigation aid, not retrieval over a whole object and not proof of full-data
inspection. Deliverables and code under review remain in their own full prompt
blocks; this module only reduces repeated source attachments.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, OrderedDict
from dataclasses import replace

from .. import extract
from ..files import StoredFile


ROLE_CONTEXT_CHARS = {
    "analysis": 48_000, "task": 48_000,
    "plan": 24_000, "plan_review": 24_000, "cross_check": 24_000,
    "review": 32_000, "final_eval": 40_000,
    "integrate": 40_000, "fix": 40_000, "wrap_up": 40_000,
}
PER_FILE_CHARS = 24_000
CHUNK_CHARS = 1600
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".c", ".h", ".cpp", ".hpp",
                   ".java", ".go", ".rs", ".sql", ".r", ".sh", ".ps1", ".ipynb"}
STOP_WORDS = frozenset("the and for are with this that from have has into your their they them which when "
                       "where what how can will should would could all any each only please file files "
                       "request task analysis review output input user model data use using source sources "
                       "not but these those then than also other must content answer work original".split())
CONSTRAINT = re.compile(r"\b(?:must|shall|required|never|prohibited|forbidden|do not|cannot|not allowed|"
                        r"maximum|minimum|constraint|submission|acceptance|target|metric)\b", re.I)
WORDS = re.compile(r"\b\w+(?:[.-]\w+)*\b", re.UNICODE)
NEIGHBOR_CHARS = 180
MAX_RELEVANT_PASSAGES = 8

# A bounded cache of small reports avoids re-profiling legacy raw CSV previews
# on every call. Keys retain no source text or file bytes.
_legacy_reports: OrderedDict[tuple, str] = OrderedDict()


def _terms(query: str) -> set[str]:
    return set(_tokens(query[:80_000]))


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in WORDS.findall(text)
            if (len(w) >= 3 or any(c.isdigit() for c in w)) and w.lower() not in STOP_WORDS]


def _prepared_text(f: StoredFile) -> str:
    text = f.text or ""
    if extract.ext_of(f.name) in {".csv", ".tsv"} and not text.startswith(extract.DATA_REPORT_MARKER) and f.data:
        # Old runs may still contain the previous profile-plus-raw-CSV format.
        # Rebuild from original bytes; never attempt to parse a profile as CSV.
        key = (f.id, hashlib.sha256(text.encode()).digest(), f.size, len(f.data))
        if key not in _legacy_reports:
            prefix = f.data[:extract.MAX_TEXT_BYTES]
            report, _ = extract.to_text(f.name, prefix, complete=len(prefix) == f.size, total_bytes=f.size)
            _legacy_reports[key] = report
            if len(_legacy_reports) > 32:
                _legacy_reports.popitem(last=False)
        _legacy_reports.move_to_end(key)
        text = _legacy_reports[key]
    return text


def _chunks(text: str) -> list[tuple[int, int]]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):
            # Prefer section/paragraph boundaries, then a line boundary, so a
            # passage rarely starts halfway through a heading or table row.
            newline = text.rfind("\n\n", start + CHUNK_CHARS // 3, end)
            if newline < 0:
                newline = text.rfind("\n", start + CHUNK_CHARS // 2, end)
            if newline >= 0:
                end = newline + 1
        chunks.append((start, end))
        start = end
    return chunks


def _passage_scores(text: str, chunks: list[tuple[int, int]], terms: set[str]) -> list[float]:
    """BM25 lexical scores within this preview; no learned reranker is claimed.

    Saturation limits repeated keywords' influence and IDF favors informative
    terms. This adapts retrieve-then-select competition pipelines to CPU-only
    operation; their trained cross-encoders are not reproduced here.
    """
    counts = [Counter(_tokens(text[start:end])) for start, end in chunks]
    lengths = [sum(row.values()) for row in counts]
    average = sum(lengths) / max(1, len(lengths)) or 1
    frequencies = Counter(term for row in counts for term in row.keys() & terms)
    size = len(chunks)
    scores = []
    for row, length in zip(counts, lengths):
        denominator = 1.2 * (0.25 + 0.75 * length / average)
        score = 0.0
        for term in sorted(row.keys() & terms):
            frequency = frequencies[term]
            idf = math.log1p((size - frequency + 0.5) / (frequency + 0.5))
            score += idf * row[term] * 2.2 / (row[term] + denominator)
        scores.append(score)
    return scores


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def _coverage_order(indices: list[int]) -> list[int]:
    """Visit the opening, end, middle and successively smaller gaps."""
    ordered = sorted(indices)
    if len(ordered) <= 2:
        return ordered
    result = [ordered[0], ordered[-1]]
    gaps = [(1, len(ordered) - 2)]
    while gaps:
        next_gaps = []
        for left, right in gaps:
            if left > right:
                continue
            middle = (left + right) // 2
            result.append(ordered[middle])
            next_gaps.extend(((left, middle - 1), (middle + 1, right)))
        gaps = next_gaps
    return result


def select_source_excerpt(text: str, query: str = "", max_chars: int = 8000) -> str:
    """Return exact small inputs or a bounded, provenance-preserving selection.

    Ranges are 1-based inclusive character positions in ``text``, never byte
    ranges or page numbers. Original content is not paraphrased. A no-match
    query falls back to broad source coverage; empty budgets return empty text.
    Duplicate removal is byte-for-byte exact, so negations, numbers, identifier
    case and code whitespace cannot silently turn different claims into one.
    """
    max_chars = max(0, max_chars)
    if len(text) <= max_chars:
        return text
    if not max_chars:
        return ""
    chunks = _chunks(text)
    terms = _terms(query)
    scores = _passage_scores(text, chunks, terms)
    matched = any(score > 0 for score in scores)
    strongest = max(scores, default=0)
    # An enormous task transcript is not a focused retrieval query. Prefer
    # broad coverage over aggressively treating its incidental words as intent.
    focused = matched and strongest >= 0.5 and len(terms) <= 96
    note = ("\n[Excerpts only; original character ranges are shown. "
            + ("Selected relevant passages with neighboring context; " if focused else
               "No strong focused match; broad coverage fallback; ")
            + "exact duplicate passages appear once with range aliases. Omitted content, including possible "
              "constraints, is not verified. This is not a complete inspection.]")
    if max_chars < len(note) + 100:
        # Even a tiny budget stays honest; do not provide a deceptively complete
        # unlabelled prefix when no provenance/coverage note can fit.
        return "[Source excerpt omitted: insufficient context budget; original retained.]"[:max_chars]

    # Canonical byte-identical chunks plus their original locations. No fuzzy
    # similarity merging: a changed number or 'not' must remain distinct.
    groups: dict[str, list[int]] = {}
    for index, (start, end) in enumerate(chunks):
        fingerprint = hashlib.sha256(text[start:end].encode()).hexdigest()
        groups.setdefault(fingerprint, []).append(index)
    canonical = {indices[0]: indices for indices in groups.values()}
    ranked = sorted(canonical, key=lambda i: (-scores[i], i))
    constraints = [i for i in ranked if CONSTRAINT.search(text[chunks[i][0]:chunks[i][1]])]
    relevant = [i for i in ranked if scores[i] >= max(0.5, strongest * 0.18)] if focused else ranked
    # Retain opening/schema and the strongest evidence before broad constraints
    # consume the budget. Constraint-bearing passages are never subject to the
    # top-k stopping rule; all still obey the caller's hard character budget.
    order = (list(dict.fromkeys([0] + relevant[:2] + constraints + relevant[:MAX_RELEVANT_PASSAGES])) if focused
             else list(dict.fromkeys([0] + constraints + (ranked[:2] if matched else [])
                                     + _coverage_order(list(canonical)))))
    selected: list[int] = []
    ranges: list[tuple[int, int]] = []

    def render(indices: list[int], intervals: list[tuple[int, int]]) -> str:
        blocks = []
        for start, end in _merge_ranges(intervals):
            aliases = []
            for i in indices:
                a, b = chunks[i]
                overlap_start, overlap_end = max(start, a), min(end, b)
                if overlap_start < overlap_end:
                    aliases.extend((chunks[other][0] + overlap_start - a, chunks[other][0] + overlap_end - a)
                                   for other in canonical.get(i, [i])[1:])
            label = f"\n[preview characters {start + 1}-{end}"
            if aliases:
                label += "; identical passages also at " + ", ".join(f"{a + 1}-{b}" for a, b in aliases[:4])
                if len(aliases) > 4:
                    label += f" (+{len(aliases) - 4} further occurrences)"
            blocks.append(label + "]\n" + text[start:end])
        return "".join(blocks) + note

    for index in order:
        if selected and max_chars - len(render(selected, ranges)) < 160:
            break
        start, end = chunks[index]
        expanded = (max(0, start - NEIGHBOR_CHARS), min(len(text), end + NEIGHBOR_CHARS))
        if index == 0 and len(chunks) > 2 and (not focused or (relevant and relevant[0] != 0)):
            # Small per-source budgets must not spend everything on the opening
            # and miss the late fact that retrieval actually found.
            expanded = (0, min(end, max(120, min(500, (max_chars - len(note)) // 6))))
        candidate = render(selected + [index], ranges + [expanded])
        if len(candidate) <= max_chars:
            selected.append(index)
            ranges.append(expanded)
            continue
        # First drop neighboring expansion, then shorten around the most
        # specific matched term. This matters when a global evidence budget is
        # divided among several sources and each receives less than one chunk.
        plain = render(selected + [index], ranges + [(start, end)])
        if len(plain) <= max_chars:
            selected.append(index)
            ranges.append((start, end))
            continue
        available = max_chars - len(render(selected, ranges)) - 100
        if available >= 160:
            piece = text[start:end]
            counts = Counter(_tokens(piece))
            hits = [(counts[m.group().lower()], -int(any(c.isdigit() for c in m.group())),
                     -len(m.group()), m.start()) for m in WORDS.finditer(piece) if m.group().lower() in terms]
            center = start + min(hits)[-1] if hits else start
            narrow_start = max(start, min(max(start, end - available), center - available // 3))
            narrow = (narrow_start, min(end, narrow_start + available))
            candidate = render(selected + [index], ranges + [narrow])
            if len(candidate) <= max_chars:
                selected.append(index)
                ranges.append(narrow)
    return render(selected, ranges)[:max_chars]


def _metadata(f: StoredFile, length: int) -> str:
    coverage = getattr(f, "coverage_note", "") or ""
    complete = getattr(f, "preview_complete", True)
    return (f"Source file ID {json.dumps(f.id)}; name {json.dumps(f.name)}; original size {f.size:,} bytes. "
            f"Stored extracted preview: {length:,} characters. "
            + ("Only a prefix/preview of the original file was inspected. " if not complete else "")
            + (coverage[:500] + " " if coverage else "")
            + "Character ranges below refer to this extracted preview, not original byte offsets.\n")


def _excerpt(f: StoredFile, text: str, budget: int, query_terms: set[str]) -> str:
    meta = _metadata(f, len(text))
    if len(meta) + len(text) + 80 <= budget:
        return meta + "Complete stored preview below; any extraction sampling limits still apply.\n" + text

    code = extract.ext_of(f.name) in CODE_EXTENSIONS
    note = ("\n[Code is incomplete here; obtain the complete file before validating or rewriting it.]" if code else "")
    room = budget - len(meta) - len(note)
    if room < 120:
        fallback = (f"Source {json.dumps(f.id)} {json.dumps(f.name)} ({f.size:,} bytes): source content omitted by "
                    "this stage's context budget. Original retained; no full-file inspection is established.")
        return fallback[:budget]

    result = meta + select_source_excerpt(text, " ".join(sorted(query_terms)), room) + note
    return result[:budget]


def compact_attachments(files: list[StoredFile], kind: str, query: str = "") -> list[StoredFile]:
    """Return task-scoped text copies with a strict aggregate character budget.

    Native media is kept for the gateway's independent provider capability/size
    checks. No model call, file download, data execution, or mutation occurs here.
    """
    if not files:
        return []
    budget = ROLE_CONTEXT_CHARS.get(kind, 40_000)
    terms = _terms(query)
    text_files = [(i, f, _prepared_text(f)) for i, f in enumerate(files) if f.kind == "text"]
    if not text_files:
        return list(files)

    aliases: dict[int, int] = {}
    seen: dict[bytes, tuple[int, str]] = {}
    by_index = {i: f for i, f, _ in text_files}
    for i, f, text in text_files:
        if not text or extract.ext_of(f.name) in CODE_EXTENSIONS:
            continue  # Keep complete small source-code files unchanged.
        fingerprint = hashlib.sha256(text.encode()).digest()
        previous = seen.get(fingerprint)
        if previous is not None and previous[1] == text:
            aliases[i] = previous[0]
        else:
            seen[fingerprint] = (i, text)

    def duplicate_note(i: int) -> str:
        return (f"Extracted preview is byte-for-byte identical to source file ID {json.dumps(by_index[aliases[i]].id)}; "
                "see its selected context above. This does not establish identical original files or full-data inspection.")

    # Every attached text file gets a reference. Give short files their complete
    # preview, then spend the remainder on relevant longer files.
    desired = {i: min(PER_FILE_CHARS, (len(duplicate_note(i)) if i in aliases else len(text) + 80)
                     + len(_metadata(f, len(text))))
               for i, f, text in text_files}
    fair = max(1, budget // len(text_files))
    allocations = {i: min(fair, desired[i]) for i, _, _ in text_files}
    spare = budget - sum(allocations.values())
    order = sorted(text_files, key=lambda item: (
        -len(_terms(item[1].name + " " + item[2]) & terms),
        -(extract.ext_of(item[1].name) in CODE_EXTENSIONS and desired[item[0]] < PER_FILE_CHARS), item[0]))
    for i, _, _ in order:
        addition = min(spare, desired[i] - allocations[i])
        allocations[i] += addition
        spare -= addition
    copies = list(files)
    for i, f, text in text_files:
        selected = ((_metadata(f, len(text)) + duplicate_note(i))[:allocations[i]] if i in aliases
                    else _excerpt(f, text, allocations[i], terms))
        copies[i] = replace(f, text=selected)
    return copies
