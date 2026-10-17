"""Deterministic, bounded attachment context for each workflow role.

Original bytes and stored previews are never mutated. These excerpts are a
navigation aid, not retrieval over a whole object and not proof of full-data
inspection. Deliverables and code under review remain in their own full prompt
blocks; this module only reduces repeated source attachments.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
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
WORDS = re.compile(r"[\w.-]{3,}", re.UNICODE)

# A bounded cache of small reports avoids re-profiling legacy raw CSV previews
# on every call. Keys retain no source text or file bytes.
_legacy_reports: OrderedDict[tuple, str] = OrderedDict()


def _terms(query: str) -> set[str]:
    return {w.lower() for w in WORDS.findall(query[:80_000]) if w.lower() not in STOP_WORDS}


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
            newline = text.rfind("\n", start + CHUNK_CHARS // 2, end)
            if newline >= 0:
                end = newline + 1
        chunks.append((start, end))
        start = end
    return chunks


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
    note = ("\n[Excerpts only: omitted material remains in the original file. Selection is deterministic lexical "
            "relevance plus opening and constraint-bearing chunks; it is not a complete inspection. "
            "Do not infer absent details or whole-file correctness."
            + (" Code is incomplete here; obtain the complete file before validating or rewriting it." if code else "") + "]")
    room = budget - len(meta) - len(note)
    if room < 120:
        fallback = (f"Source {json.dumps(f.id)} {json.dumps(f.name)} ({f.size:,} bytes): source content omitted by "
                    "this stage's context budget. Original retained; no full-file inspection is established.")
        return fallback[:budget]

    chunks = _chunks(text)
    ranked = []
    for index, (start, end) in enumerate(chunks):
        piece = text[start:end]
        hits = len(_terms(piece) & query_terms)
        # Include the source's coverage/schema opening. Constraint-bearing
        # chunks survive even when the user's phrasing differs from the source.
        score = hits * 4 + min(3, len(CONSTRAINT.findall(piece))) * 3
        if index == 0:
            score += 1000
        ranked.append((-score, index, start, end))
    selected = []
    for _, index, start, end in sorted(ranked):
        label = f"\n[Source {f.id}, preview characters {start + 1}-{end}]\n"
        needed = len(label) + end - start
        if needed <= room:
            selected.append((start, label + text[start:end]))
            room -= needed
        elif not selected and room > len(label) + 80:
            end = start + room - len(label) - 16
            label = f"\n[Source {f.id}, preview characters {start + 1}-{end}]\n"
            selected.append((start, label + text[start:end]))
            room = 0
        if room < 100:
            break
    # Source order makes the remaining references and code fragments legible.
    result = meta + "".join(s for _, s in sorted(selected)) + note
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

    # Every attached text file gets a reference. Give short files their complete
    # preview, then spend the remainder on relevant longer files.
    desired = {i: min(PER_FILE_CHARS, len(text) + len(_metadata(f, len(text))) + 80)
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
        copies[i] = replace(f, text=_excerpt(f, text, allocations[i], terms))
    return copies
