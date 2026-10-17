"""Run state. One run = one request moving through plan, work, gates and delivery."""
from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from . import codefile

RunStatus = Literal[
    "analyzing", "cross_checking", "planning", "reviewing_plan", "awaiting_approval", "running", "integrating",
    "completed", "failed", "cancelled", "budget_stopped", "interrupted",
]
TaskStatus = Literal["pending", "working", "reviewing", "revising", "accepted", "needs_human_review", "blocked", "failed"]
TERMINAL = {"completed", "failed", "cancelled", "budget_stopped", "interrupted"}


def now() -> float:
    return time.time()


class Review(BaseModel):
    reviewer: str
    model: str = ""
    verdict: str  # pass | revise | block | accept
    score: float | None = None
    findings: list[str] = []
    at: float = Field(default_factory=now)


class Attempt(BaseModel):
    provider: str
    model: str = ""
    output: str = ""
    is_revision: bool = False
    review: Review | None = None
    error: str | None = None
    at: float = Field(default_factory=now)


class Task(BaseModel):
    id: str
    title: str
    description: str = ""
    depends_on: list[str] = []
    status: TaskStatus = "pending"
    implementer: str | None = None
    reviewer: str | None = None
    route_reason: str = ""
    attempts: list[Attempt] = []
    output: str = ""
    # objectively-tested | evidence-reviewed | self-reviewed | human-approved
    assurance: str | None = None


class Plan(BaseModel):
    goal: str = ""
    assumptions: list[str] = []
    deliverables: list[str] = []
    success_criteria: list[str] = []
    tasks: list[Task] = []
    planner: str | None = None
    version: int = 1


class Usage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_provider: dict[str, dict[str, int]] = {}

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Event(BaseModel):
    at: float = Field(default_factory=now)
    type: str
    message: str
    provider: str | None = None
    task_id: str | None = None


class Judgement(BaseModel):
    """One model's test of the final result: its score and what must be fixed."""

    judge: str
    model: str = ""
    score: float | None = None
    meets_criteria: bool | None = None
    must_fix: list[str] = []
    gaps: list[str] = []
    round: int = 0  # 0 = first check, 1 = after the fixes
    at: float = Field(default_factory=now)


class FinalResult(BaseModel):
    output: str = ""
    integrator: str | None = None
    evaluator: str | None = None
    meets_criteria: bool | None = None
    score: float | None = None  # average of the other models' scores on the last check
    gaps: list[str] = []
    limitations: list[str] = []
    judges: list[Judgement] = []
    fixes: int = 0  # rounds of fixes after the other models tested it
    best_approach: str | None = None  # whose approach the single solution was built on


class FileRef(BaseModel):
    id: str
    name: str
    mime: str
    kind: str  # image | video | audio | pdf | text
    size: int


class Opinion(BaseModel):
    """One model's independent analysis, or its cross-check of the others."""

    provider: str
    model: str = ""
    output: str = ""
    error: str | None = None
    scores: dict[str, float] = {}  # cross-checks only: this model's 0-10 score for each approach, by provider
    at: float = Field(default_factory=now)


class Consensus(BaseModel):
    """The models' scores for each other's approaches, and the one they build the single answer on."""

    best: str
    average: dict[str, float] = {}  # approach (provider) -> average score from the other models
    votes: dict[str, dict[str, float]] = {}  # judge -> approach -> score


class LiveText(BaseModel):
    """Text a model is writing right now, streamed to the screen."""

    provider: str
    label: str
    text: str = ""
    at: float = Field(default_factory=now)


class Run(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    request: str
    materials: str = ""
    profile: str = "general"
    eris: bool = False  # Project Eris mode: every model follows the Eris Solver Guidebook, dry runs only
    require_plan_approval: bool = True
    compare_all: bool = True
    files: list[FileRef] = []
    analyses: list[Opinion] = []
    cross_checks: list[Opinion] = []
    consensus: Consensus | None = None
    team: list[str] = []  # the models working on this run, best first (chosen when it starts)
    skipped: dict[str, str] = {}  # models not taking part in this run, and why (no key, failing key)
    live: dict[str, LiveText] = {}
    status: RunStatus = "planning"
    simulated: bool = False
    plan: Plan | None = None
    plan_review: Review | None = None
    plan_approved: bool = False
    final: FinalResult | None = None
    usage: Usage = Field(default_factory=Usage)
    events: list[Event] = []
    error: str | None = None
    # Time is the run's limit. The clock pauses while the plan waits for approval.
    time_limit_s: float = 0  # 0: no limit
    wrap_up_s: float = 0  # the last part of the limit, kept for delivering the complete result
    used_s: float = 0  # working time used before the clock last started
    clock_started_at: float | None = None  # set while the clock runs
    deadline_at: float | None = None  # when the run must be finished, while the clock runs
    wrapping_up: bool = False
    finished_at: float | None = None

    @computed_field  # saved with the run and sent to the page; the code file in the final result
    @property
    def solution(self) -> dict | None:
        if not self.final or not self.final.output:
            return None
        return codefile.find(self.final.output, any_python=self.eris or self.profile == "software",
                             name="solution.py" if self.eris else "")

    @property
    def elapsed_s(self) -> float:
        return self.used_s + (now() - self.clock_started_at if self.clock_started_at else 0)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "request": self.request[:200],
            "profile": self.profile,
            "eris": self.eris,
            "status": self.status,
            "simulated": self.simulated,
            "tasks": len(self.plan.tasks) if self.plan else 0,
            "tokens": self.usage.total_tokens,
            "files": len(self.files),
            "score": self.final.score if self.final else None,
            "team": self.team,
            "elapsed_s": round(self.elapsed_s),
            "time_limit_s": self.time_limit_s,
        }


class CreateRun(BaseModel):
    request: str = Field(min_length=3, max_length=20_000)
    materials: str = Field(default="", max_length=60_000)
    profile: str = "auto"  # "auto" lets SASI pick the field from the request
    require_plan_approval: bool = True
    compare_all: bool = True
    eris: bool = False  # Project Eris switch
    file_ids: list[str] = Field(default=[], max_length=10)


class ApprovePlan(BaseModel):
    note: str = ""


class SaveKey(BaseModel):
    key: str  # validated in the endpoint so error replies never echo it


class Login(BaseModel):
    code: str
