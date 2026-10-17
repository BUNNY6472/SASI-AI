"""Find the code file in a run's final result, so it can be downloaded as solution.py.

The final result is Markdown. The file is the fenced Python block labelled solution.py (in its info string,
such as ```python solution.py, or on the line just above it, such as a "## solution.py" heading). Without such
a label, the largest Python block is used, leaving out helper files such as dry_run.py and tests.
"""
from __future__ import annotations

import re

FENCE = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n`]*)\n(?P<code>.*?)(?:^[ \t]*(?P=fence)[`~]*[ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL,
)
PY_NAME = re.compile(r"([A-Za-z0-9_\-]+\.py)\b")
PYTHON = {"python", "python3", "py", "ipython"}
LOOKS_LIKE_PYTHON = re.compile(r"^\s*(import \w|from [\w.]+ import |def \w|class \w|if __name__ == )", re.MULTILINE)
HELPERS = ("dry_run", "test_", "conftest")
MIN_LINES = 3


def _label(info: str, before: str) -> str:
    """The .py file name a block is labelled with, or ""."""
    m = PY_NAME.search(info)
    if m:
        return m.group(1).lower()
    last = next((ln for ln in reversed(before.splitlines()) if ln.strip()), "")
    m = PY_NAME.search(last) if len(last) < 120 else None  # a heading or caption, not a sentence that mentions a file
    return m.group(1).lower() if m else ""


def find(output: str, any_python: bool = True, name: str = "") -> dict | None:
    """{"name", "code", "lines"} for the result's code file, or None.

    `any_python`: when no block is labelled solution.py, fall back to the largest Python block. Off for runs
    whose result is prose, where a short example is not the deliverable. `name` names the file whatever its label.
    """
    if not output or ("```" not in output and "~~~" not in output):
        return None
    labelled, others = [], []
    for m in FENCE.finditer(output):
        info, code = m.group("info").strip(), m.group("code")
        tag = _label(info, output[max(0, m.start() - 300):m.start()])
        lang = info.split()[0].lower() if info else ""
        python = lang in PYTHON or tag.endswith(".py") or (not lang and LOOKS_LIKE_PYTHON.search(code) is not None)
        if not python or not code.strip():
            continue
        if tag == "solution.py":
            labelled.append(code)
        elif not tag.startswith(HELPERS):
            others.append((code, tag))
    if labelled:
        code, label = max(labelled, key=len), "solution.py"
    elif any_python and others:
        code, label = max(others, key=lambda c: len(c[0]))
    else:
        return None
    code = code.rstrip() + "\n"
    lines = code.count("\n")
    if lines < MIN_LINES:
        return None
    return {"name": name or label or "solution.py", "code": code, "lines": lines}
