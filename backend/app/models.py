"""Run state. One run = one request moving through plan, work, gates and delivery."""
from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field

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


class FinalResult(BaseModel):
    output: str = ""
    integrator: str | None = None
    evaluator: str | None = None
    meets_criteria: bool | None = None
    score: float | None = None
    gaps: list[str] = []
    limitations: list[str] = []


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
    at: float = Field(default_factory=now)


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
