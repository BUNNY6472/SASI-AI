"""Deterministic, bounded checks. These never execute generated Python.

Link coverage proves only that a URL was fetched, not that it supports a claim.
AST parsing proves Python grammar parses, not that the program runs or is safe.
Every check records the SHA-256 of the exact artifact it checked.
"""
from __future__ import annotations

import ast
import hashlib

from . import codefile
from .tools.evidence import canonical_url, urls_in_text

MAX_CODE_BYTES = 100_000
MAX_OUTPUT_CHARS = 500_000


def artifact_hash(content: str | bytes) -> str:
    return hashlib.sha256(content.encode("utf-8") if isinstance(content, str) else content).hexdigest()


def _check(cid: str, kind: str, status: str, summary: str, digest: str) -> dict:
    return {"id": cid, "kind": kind, "status": status, "summary": summary, "artifact_hash": digest}


def verify_python(code: str, *, expected_hash: str | None = None) -> list[dict]:
    """Check one exact Python artifact; no imports, subprocesses, or execution."""
    digest = artifact_hash(code)
    checks = [_check("artifact-sha256", "artifact_hash", "fail" if expected_hash and expected_hash != digest else "pass",
                     "Artifact differs from the expected SHA-256." if expected_hash and expected_hash != digest else
                     "SHA-256 recorded for the exact checked code." if expected_hash is None else "Artifact SHA-256 matches.", digest)]
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        checks.append(_check("python-syntax", "python_syntax", "unknown", "Code exceeds the 100 KB syntax-check limit.", digest))
    else:
        try:
            ast.parse(code, filename="solution.py")
        except SyntaxError as exc:
            checks.append(_check("python-syntax", "python_syntax", "fail",
                                 f"Python syntax error at line {exc.lineno}, column {exc.offset}: {exc.msg}", digest))
        except (ValueError, RecursionError, MemoryError) as exc:
            checks.append(_check("python-syntax", "python_syntax", "unknown", f"Parser could not check code ({type(exc).__name__}).", digest))
        else:
            checks.append(_check("python-syntax", "python_syntax", "pass", "Python AST parsing passed; behavior, dependencies and safety are untested.", digest))
    checks.append(_check("code-execution", "execution", "unknown", "Generated code was not executed. No isolated execution service is configured.", digest))
    return checks


def check_citations(output: str, evidence=None) -> dict:
    digest = artifact_hash(output)
    items = evidence.get("items", []) if isinstance(evidence, dict) else (evidence or [])
    known = set()
    for item in items:
        if item.get("source_kind") not in ("web", "google_search"):
            continue
        if item.get("source_kind") == "web" and not item.get("excerpt"):
            continue
        for key in ("url", "requested_url"):
            if item.get(key):
                try:
                    known.add(canonical_url(item[key]))
                except ValueError:
                    pass
    if len(output) > MAX_OUTPUT_CHARS:
        return _check("citation-coverage", "citations", "unknown", "Output exceeds the citation-check size limit.", digest)
    prose = codefile.FENCE.sub("", output)
    cited = urls_in_text(prose, limit=101)
    if len(cited) > 100:
        return _check("citation-coverage", "citations", "unknown", "Output exceeds the 100-link citation-check limit.", digest)
    if not cited:
        return _check("citation-coverage", "citations", "unknown", "No web citation URLs were present; factual claims were not independently verified.", digest)
    missing = []
    for url in cited:
        try:
            present = canonical_url(url) in known
        except ValueError:
            present = False
        if not present:
            missing.append(url)
    if missing:
        return _check("citation-coverage", "citations", "fail",
                      f"{len(missing)} of {len(cited)} citation URLs were not retrieved or returned by search: " + "; ".join(u[:180] for u in missing[:3])
                      + ". This does not establish whether their claims are true or false.", digest)
    return _check("citation-coverage", "citations", "pass",
                  f"All {len(cited)} cited URLs match retrieved pages or provider search metadata. Claim support, source quality and completeness still require review.", digest)


def verify_output(output: str, evidence=None, *, artifact: dict | None = None) -> list[dict]:
    """Checks for a final answer and its primary downloadable Python artifact.

    `artifact` may contain {code, name, expected_hash}; omit to use codefile.find.
    The output citation check hashes the full prose; code checks hash exact code.
    """
    checks = [check_citations(output, evidence)]
    if artifact is None and len(output) <= MAX_OUTPUT_CHARS:
        artifact = codefile.find(output)
    if artifact and isinstance(artifact.get("code"), str):
        checks.extend(verify_python(artifact["code"], expected_hash=artifact.get("expected_hash")))
    else:
        checks.append(_check("artifact-sha256", "artifact_hash", "pass", "SHA-256 recorded for the exact final answer.", artifact_hash(output)))
        checks.append(_check("code-execution", "execution", "unknown", "No executable Python artifact was checked or run.", artifact_hash(output)))
    return checks
