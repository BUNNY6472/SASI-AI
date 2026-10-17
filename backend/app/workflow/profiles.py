"""Field profiles: the same engine, different instructions and acceptance checks."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldProfile:
    key: str
    name: str
    description: str
    planning: str
    work: str
    review_checks: list[str]
    final_checks: list[str]
    output_format: str


PROFILES: dict[str, FieldProfile] = {
    p.key: p
    for p in [
        FieldProfile(
            key="software",
            name="Software engineering",
            description="Architecture, code, debugging, tests and documentation.",
            planning="Agree the interfaces or API contract first so later tasks can run in parallel against it. Typical tasks: contract/design, implementation pieces, tests, docs.",
            work="Write production-quality code in fenced blocks with file names. Explain decisions briefly. Never include secrets or API keys.",
            review_checks=[
                "Does the code satisfy the stated requirements and the agreed interface?",
                "Are there bugs, unhandled errors, security problems or missing edge cases?",
                "Would it build and run as written? Are tests present where useful?",
            ],
            final_checks=["Requirements coverage", "Consistency between components", "Build/run instructions present"],
            output_format="Markdown with fenced code blocks, a short overview and run instructions.",
        ),
        FieldProfile(
            key="research",
            name="Research and reports",
            description="Explanations, literature-style reviews, analysis and learning material.",
            planning="Split into questions that can be researched independently, then a synthesis task that depends on them.",
            work="Be precise. Separate established facts from inference. Flag uncertainty and anything that may be out of date. Do not invent citations; name sources only if you are confident they exist.",
            review_checks=[
                "Are claims accurate and is uncertainty stated?",
                "Is the reasoning sound, with no invented sources?",
                "Does it answer the assigned question fully?",
            ],
            final_checks=["Every question answered", "Uncertainty and limitations stated", "No unsupported claims"],
            output_format="Structured Markdown report with headings, key findings first, and a limitations section.",
        ),
        FieldProfile(
            key="general",
            name="General",
            description="Product, business, planning and anything else.",
            planning="Use the smallest set of tasks that covers the request. Run independent parts in parallel.",
            work="Be concrete and practical. State assumptions.",
            review_checks=[
                "Does it fully address the assigned task?",
                "Is it correct, concrete and internally consistent?",
                "Are assumptions and risks stated?",
            ],
            final_checks=["Request fully addressed", "Clear and actionable", "Assumptions and limitations stated"],
            output_format="Clear Markdown with headings.",
        ),
    ]
}


def get_profile(key: str) -> FieldProfile:
    return PROFILES.get(key, PROFILES["general"])


_SOFTWARE = re.compile(
    r"\b(code|coding|program|programming|bug|bugs|debug|api|apis|function|python|javascript|typescript|java|kotlin|swift|"
    r"react|next\.?js|node|sql|database|schema|deploy|deployment|backend|frontend|script|algorithm|repo|repository|github|"
    r"docker|kubernetes|compile|stack trace|exception|refactor|unit tests?|endpoint|software|website|web app|mobile app|"
    r"android|ios|flutter|django|fastapi|microservices?|architecture|devops|ci/cd|cloud run|terraform)\b", re.I)
_RESEARCH = re.compile(
    r"\b(research|analy[sz]e|analy[sz]is|analytics|compare|comparison|study|studies|report|literature|paper|papers|evidence|"
    r"data|dataset|csv|statistics|statistical|trend|trends|survey|hypothesis|market|sources|scientific|science|medical|"
    r"clinical|findings|forecast|insights?|investigate|evaluate|assessment|review of|pros and cons|explain why)\b", re.I)
_CODE_FILES = (".py", ".js", ".ts", ".sql", ".java", ".go", ".rs", ".tsx", ".jsx")


def detect_profile(request: str, materials: str = "", file_names: list[str] | None = None) -> str:
    """Choose the field from the request itself, so one box handles every kind of question.

    The planner confirms or corrects this once it has read everything.
    """
    text = f"{request}\n{materials[:4000]}"
    software = len(_SOFTWARE.findall(text))
    research = len(_RESEARCH.findall(text))
    for name in file_names or []:
        low = name.lower()
        if low.endswith(_CODE_FILES):
            software += 2
        elif low.endswith((".csv", ".tsv", ".json", ".pdf", ".xlsx")):
            research += 1
    if software == research == 0:
        return "general"
    if software > research:
        return "software"
    if research > software:
        return "research"
    return "general"
