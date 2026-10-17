"""Field profiles: the same engine, different instructions and acceptance checks."""
from __future__ import annotations

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
