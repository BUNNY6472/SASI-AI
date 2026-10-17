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
            key="eris",
            name="Project Eris ML",
            description="ML challenge solutions that follow the Eris Solver Guidebook, checked with dry runs only.",
            planning=(
                "This is a Project Eris ML challenge. Plan an Eris-compliant solution: understand the data, metric and "
                "submission format; choose a model that is genuinely trained or fine-tuned inside the script, with any HPO "
                "inside it (fixed seed, fixed number of trials) and fixed epochs that fit 1.5 hours on an A10G. Exactly ONE "
                "task writes the complete solution.py; other tasks can analyse the data and design the approach before it, "
                "and one task after it writes the dry-run steps and checks the script rule by rule."
            ),
            work=(
                "Follow every Eris rule. Write complete, runnable Python in fenced blocks with file names, never fragments "
                "or placeholders. Put the seed template at the top, use only libraries in the Kaggle Docker image, and give "
                "dry-run steps instead of asking for a full run."
            ),
            review_checks=[
                "Does it do what the challenge asks, using only the challenge data?",
                "Does the code run as written, without bugs, missing pieces or a malformed submission.csv?",
                *[f"Eris rule: {c}" for c in [
                    "real training or fine-tuning inside the script",
                    "no hardcoded values or offline-found hyperparameters; HPO inside the script with a fixed seed and a fixed number of trials",
                    "one end-to-end script from raw data to submission.csv; no cached artifacts or self-fine-tuned weights",
                    "no external or synthetic data; only Kaggle Docker image libraries; no pip or GitHub installs",
                    "test set only for one-sample or one-batch inference",
                    "determinism: all seeds set; no clock or machine checks; no try/except fallbacks; no switches or debug flags",
                    "fixed epochs or steps that fit 1.5 hours on an A10G",
                    "the guidebook's domain rules for this kind of challenge",
                    "dry runs only: no instruction to run the full end-to-end training",
                ]],
            ],
            final_checks=[
                "One complete end-to-end solution.py that writes a complete submission.csv",
                "Every Eris rule passes (training, hardcoding, data, libraries, test set, determinism, runtime, domain rules)",
                "Dry-run steps are given and the full end-to-end run is not requested",
                "Grey areas and the expected runtime on an A10G are stated",
            ],
            output_format=(
                "Markdown: the approach in a few lines, the complete solution.py in one fenced block, '## Dry run' steps, "
                "'## Eris rule check' as a table (rule, pass or fail or unclear, why), the expected runtime on an A10G, and grey areas or risks."
            ),
        ),
        FieldProfile(
            key="general",
            name="General",
            description="Product, business, planning and anything else.",
            planning="Use the smallest set of tasks that covers the request. Run independent parts in parallel.",
            work="Be concrete, correct, and concise. State only assumptions that materially affect the answer and fit the user's requested format.",
            review_checks=[
                "Does it fully address the assigned task?",
                "Is it correct, concrete and internally consistent?",
                "Are any material assumptions or risks made clear, where relevant and compatible with the requested format?",
            ],
            final_checks=["Request fully addressed in the requested format", "Clear and correct",
                          "Material assumptions or limitations made clear when relevant and compatible with the requested format"],
            output_format="Use the simplest clear format that fits the answer; headings only when helpful and compatible with the request.",
        ),
    ]
}


# Fields the planner can choose. Project Eris is only ever chosen by the user's switch.
AUTO_FIELDS = ("software", "research", "general")


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
