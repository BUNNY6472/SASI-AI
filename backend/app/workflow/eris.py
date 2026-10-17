"""Project Eris mode: every model follows the Eris Solver Guidebook, and only dry runs are allowed.

The guidebook text lives next to this file (eris_guidebook.md). When a run has Eris
mode on, RULES goes in front of every model's instructions, so the same long text
is reused across calls (the providers cache a repeated prefix, which keeps it cheap).
"""
from __future__ import annotations

from pathlib import Path

GUIDEBOOK = Path(__file__).with_name("eris_guidebook.md").read_text(encoding="utf-8").strip()

# Short names for the rules, used in the rule-check table, the reviews and the final evaluation.
CHECKS: list[str] = [
    "Real ML: a model is genuinely trained or fine-tuned inside the script (no inference-only, no rule-based or pure algorithmic solution)",
    "No hardcoding: no hardcoded labels, values or hyperparameters found offline; any HPO runs inside the script with a fixed seed and a fixed number of trials",
    "One end-to-end script: preprocessing, features, training and inference run from the raw challenge data in one script that writes submission.csv; no cached artifacts, no self-fine-tuned weights loaded back in",
    "Data: only the challenge data plus general pretrained backbone weights (timm, Hugging Face); no external datasets and no synthetic data",
    "Libraries: only what the Kaggle Docker image already has; no pip installs, no GitHub code or models",
    "Test set: used only for one-sample or one-batch inference (no pseudo-labelling, test-time adaptation, or calibration on the whole test set)",
    "Determinism: every seed set (random, NumPy, PyTorch CPU and GPU, every model, split and DataLoader); nothing reads the clock or adapts to the machine; no try/except fallbacks; no environment switches, debug flags or unused branches",
    "Runtime: fixed epochs or steps that finish within 1.5 hours on one Nvidia A10G, including inference and writing the file",
    "Submission: submission.csv is complete, with every test id and the exact columns and format the challenge asks for",
    "Domain rules: the guidebook's rules for this kind of challenge (CV, NLP, RAG, fine-tuning, from-scratch, bio/chem) are followed, grey areas are named, and the challenge's own real restrictions win",
]

DRY_RUN = (
    "DRY RUNS ONLY. Never run, and never tell the user to run, the full end-to-end training: a full run on the test "
    "environment costs a submission credit and up to 1.5 hours. Never claim you ran or measured anything. Verify with dry "
    "runs only:\n"
    "- Give the final solution.py clean: no DRY_RUN flag, debug switch, environment variable or if-branch for it, because "
    "the determinism check rejects those.\n"
    "- Separately, under '## Dry run', list the exact edits that turn a COPY named dry_run.py into a quick check: for example "
    "read only the first 2,000 training rows and 500 test rows, 1 epoch or a few hundred steps, and 2 HPO trials.\n"
    "- Say what to check after the dry run: it finishes without errors, submission.csv has the right columns and format "
    "(on the dry run it will hold only the sampled rows), the validation score is printed, and the timing measured from the "
    "shell (for example `time python dry_run.py`, never a clock inside the script) scales to well under 1.5 hours on an A10G.\n"
    "- Remind the user to delete dry_run.py and submit only the clean solution.py."
)


def checklist() -> str:
    return "\n".join(f"{i}. {c}" for i, c in enumerate(CHECKS, 1))


RULES = (
    "PROJECT ERIS MODE IS ON. The user is solving a Project Eris ML challenge. Every answer must follow the Eris Solver "
    "Guidebook below. Where a challenge's own description sets a real restriction, it wins, as the guidebook explains in "
    "section 5.4. If something is a grey area, say so plainly rather than quietly relying on it.\n\n"
    f"{DRY_RUN}\n\n"
    "The rules every solution is checked against:\n"
    f"{checklist()}\n\n"
    "<eris_solver_guidebook>\n"
    f"{GUIDEBOOK}\n"
    "</eris_solver_guidebook>"
)
