"""Bounded, observable effort allocation. Time is a ceiling, never a target to fill.

Initial estimates are deliberately simple. Actual call durations replace them as
the run progresses; quality gates, rather than elapsed time, decide completion.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from ..models import Run


@dataclass(frozen=True)
class Effort:
    analysts: int
    cross_check: bool
    task_limit: int
    reason: str


def complex_request(run: Run) -> bool:
    text = run.request + "\n" + run.materials
    signals = re.findall(r"\b(research|investigate|compare|prove|architecture|experiment|complex|comprehensive|literature|optimi[sz]e)\b", text, re.I)
    return run.eris or run.profile == "research" or len(text) > 1800 or len(run.files) > 1 or len(signals) >= 2


def allocate(run: Run, available: int, max_tasks: int) -> Effort:
    available = max(1, available)
    if run.compare_all is False:
        return Effort(0, False, max_tasks, "Legacy comparison is disabled")
    if run.compare_all is True or run.mode == "deep":
        return Effort(available, available > 1, max_tasks, "Deep comparison across the available team")
    if run.mode == "quick":
        return Effort(1, False, min(2, max_tasks), "Quick mode uses one analysis and independent review")
    if complex_request(run):
        count = min(3 if run.profile == "research" or run.eris else 2, available)
        return Effort(count, count > 1, max_tasks, "Research or complex requirements benefit from independent approaches")
    return Effort(1, False, min(3, max_tasks), "A focused request starts with one analysis and independent review")


def expected_seconds(run: Run, kind: str) -> float:
    defaults = {"analysis": 60.0, "cross_check": 35.0, "plan": 25.0, "plan_review": 20.0,
                "task": 80.0, "review": 25.0, "integrate": 80.0, "final_eval": 30.0, "wrap_up": 55.0}
    multiplier = 1.5 if complex_request(run) or run.mode == "deep" else 1.0
    ratios = [value / (defaults.get(stage, 40.0) * multiplier) for stage, value in run.call_duration_s.items()]
    speed = max(0.25, min(4.0, statistics.median(ratios))) if ratios else 1.0
    fallback = defaults.get(kind, 40.0) * multiplier * speed
    # A little headroom remains even when earlier calls were exceptionally fast.
    return max(0.1, run.call_duration_s.get(kind, fallback))


def observe(run: Run, kind: str, duration: float) -> None:
    measured = max(0.01, duration)
    previous = run.call_duration_s.get(kind)
    run.call_duration_s[kind] = measured if previous is None else 0.6 * measured + 0.4 * previous


def finish_reserve(run: Run) -> float:
    """Time for synthesis and independent review, adjusted using measured latency."""
    reserve = 1.4 * expected_seconds(run, "integrate") + 1.3 * expected_seconds(run, "final_eval")
    if run.mode == "deep":
        reserve += expected_seconds(run, "integrate") + expected_seconds(run, "final_eval")
    return reserve


def stage_allowance(run: Run, kind: str, remaining: float | None) -> float | None:
    if remaining is None:
        return None
    desired = 2.0 * expected_seconds(run, kind)
    reserved = finish_reserve(run) + expected_seconds(run, "plan") + expected_seconds(run, "task")
    return max(0.05, min(desired, max(0.05, remaining - reserved)))


def reasoning_effort(run: Run, kind: str, remaining: float | None) -> str:
    """Provider reasoning effort, independently bounded by delivery time."""
    if run.mode == "quick":
        return "low" if kind in {"analysis", "plan", "plan_review", "cross_check"} else "medium"
    core = kind in {"task", "integrate", "final_eval"}
    if not core or not (run.mode == "deep" or complex_request(run)):
        return "medium"
    needed = 1.5 * expected_seconds(run, kind)
    if kind == "task":
        needed += finish_reserve(run)
    return "high" if remaining is None or remaining >= needed else "medium"
