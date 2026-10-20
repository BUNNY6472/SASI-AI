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
    uncached_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    thinking_tokens: int = 0
    estimated_tokens: int = 0
    detailed_calls: int = 0
    usage_details_available: bool = False
    by_stage: dict[str, dict[str, int]] = {}

    @computed_field
    @property
    def unclassified_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.uncached_input_tokens
                   - self.cache_read_input_tokens - self.cache_write_input_tokens)

    def record(self, provider: str, stage: str, input_tokens: int, output_tokens: int, *,
               uncached_input_tokens: int = 0, cache_read_input_tokens: int = 0,
               cache_write_input_tokens: int = 0, thinking_tokens: int = 0,
               detailed: bool = False, estimated: bool = False) -> None:
        """Calls are admitted separately. Cache and reasoning counters are subsets, never extra usage."""
        values = {"input_tokens": max(0, input_tokens), "output_tokens": max(0, output_tokens),
                  "uncached_input_tokens": max(0, uncached_input_tokens),
                  "cache_read_input_tokens": max(0, cache_read_input_tokens),
                  "cache_write_input_tokens": max(0, cache_write_input_tokens),
                  "thinking_tokens": max(0, thinking_tokens),
                  "estimated_tokens": max(0, input_tokens + output_tokens) if estimated else 0,
                  "detailed_calls": int(detailed)}
        for key, value in values.items():
            setattr(self, key, getattr(self, key) + value)
        self.usage_details_available = self.usage_details_available or detailed or estimated
        for group, key in ((self.by_provider, provider), (self.by_stage, stage)):
            entry = group.setdefault(key, {})
            entry["calls"] = entry.get("calls", 0) + 1
            for name, value in values.items():
                entry[name] = entry.get(name, 0) + value

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
    artifact_hash: str = ""
    artifact_version: int = 0


class CheckResult(BaseModel):
    id: str
    kind: str
    status: Literal["pass", "fail", "unknown"]
    summary: str
    artifact_hash: str


class SourceEvidence(BaseModel):
    id: str
    url: str
    title: str = ""
    excerpt: str = ""
    retrieved_at: float = Field(default_factory=now)
    source_kind: str = "web"
    requested_url: str = ""
    content_hash: str = ""
    truncated: bool = False


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
    verification_status: Literal["unverified", "reviewed", "partial", "failed"] = "unverified"
    artifact_hash: str = ""
    artifact_version: int = 0
    # A completed fix returned identical bytes under this unchanged review context.
    # Persist across resume; new evidence/criteria/files invalidate the stop.
    stalled_context_hash: str = ""
    checks: list[CheckResult] = Field(default_factory=list)


class FileRef(BaseModel):
    id: str
    name: str
    mime: str
    kind: str  # image | video | audio | pdf | text
    size: int
    preview_complete: bool = True
    preview_bytes: int = 0
    coverage_note: str = ""
    storage: str = "local"


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


class CallTiming(BaseModel):
    provider: str
    kind: str
    label: str = ""
    started_at: float
    soft_deadline_at: float
    hard_deadline_at: float
    in_grace: bool = False


class PartialOutput(BaseModel):
    """Preserved unfinished text; never a completed opinion, accepted task, or passing review."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    provider: str
    model: str = ""
    kind: str
    label: str = ""
    text: str
    task_id: str | None = None
    reason: str
    created_at: float = Field(default_factory=now)
    usage_estimated: bool = True


class BridgeReview(BaseModel):
    """A compact review bound to exact answer bytes and their full review context."""
    provider: str
    model: str = ""
    artifact_hash: str
    context_hash: str
    round: int
    packet: dict | None = None
    error: str = ""


class BridgeState(BaseModel):
    phase: Literal["routing", "planning", "answering", "verifying", "correcting", "complete"] = "routing"
    complexity: str = "simple"
    reasons: list[str] = Field(default_factory=list)
    answerer: str | None = None
    verifier: str | None = None
    plan: dict | None = None
    plan_attempted: bool = False
    reviews: list[BridgeReview] = Field(default_factory=list)
    review_context_hash: str = ""
    correction_attempted: bool = False
    correction_completed: bool = False
    # Conservatively charged after a crash if provider usage was never checkpointed.
    inflight_tokens: int = 0
    inflight_provider: str = ""
    inflight_stage: str = ""


class Run(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    request: str
    materials: str = ""
    profile: str = "general"
    eris: bool = False  # Project Eris mode: every model follows the Eris Solver Guidebook, dry runs only
    require_plan_approval: bool = True
    compare_all: bool | None = None  # explicit legacy setting; None lets the effort policy decide
    mode: Literal["adaptive", "quick", "deep"] = "adaptive"
    # Missing on old checkpoints: keep their original workflow and timing semantics.
    workflow: Literal["legacy", "bridge"] = "legacy"
    bridge: BridgeState | None = None
    time_limit_minutes: float | None = Field(default=None, ge=5, le=120)
    evidence: list[SourceEvidence] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    search_entry_html: str | None = None
    research_summary: str = ""
    research_status: str = "not_started"
    call_duration_s: dict[str, float] = Field(default_factory=dict)
    gap_tasks_added: int = 0
    files: list[FileRef] = []
    analyses: list[Opinion] = []
    cross_checks: list[Opinion] = []
    consensus: Consensus | None = None
    team: list[str] = []  # the models working on this run, best first (chosen when it starts)
    skipped: dict[str, str] = {}  # models not taking part in this run, and why (no key, failing key)
    live: dict[str, LiveText] = {}
    active_calls: dict[str, CallTiming] = Field(default_factory=dict)
    call_elapsed_s: dict[str, float] = Field(default_factory=dict)
    call_allowance_s: dict[str, float] = Field(default_factory=dict)
    call_completed_keys: list[str] = Field(default_factory=list)
    partial_outputs: list[PartialOutput] = Field(default_factory=list)
    status: RunStatus = "planning"
    simulated: bool = False
    plan: Plan | None = None
    plan_review: Review | None = None
    plan_approved: bool = False
    final: FinalResult | None = None
    usage: Usage = Field(default_factory=Usage)
    events: list[Event] = []
    error: str | None = None
    # Older records retain strict/graceful deadlines. New bridge runs only track elapsed time.
    # Working-time accounting pauses while waiting for approval.
    timing_policy: Literal["strict", "graceful", "question_driven"] = "strict"
    model_phase_min_s: float = 1200
    model_phase_grace_s: float = 300
    total_time_limit_s: float = 7200  # hard active-runtime ceiling including final assembly; pauses exclude approval/downtime
    finalization_deadline_elapsed_s: float | None = None
    time_limit_s: float = 0  # 0: no limit
    wrap_up_s: float = 0  # the last part of the limit, kept for delivering the complete result
    used_s: float = 0  # working time used before the clock last started
    clock_started_at: float | None = None  # set while the clock runs
    deadline_at: float | None = None  # admission target (graceful) or finish deadline (legacy strict)
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
            "total_time_limit_s": self.total_time_limit_s,
            "timing_policy": self.timing_policy,
            "mode": self.mode,
            "workflow": self.workflow,
            "verification_status": self.final.verification_status if self.final else "unverified",
        }


class CreateRun(BaseModel):
    request: str = Field(min_length=3, max_length=20_000)
    materials: str = Field(default="", max_length=60_000)
    profile: str = "auto"  # "auto" lets SASI pick the field from the request
    require_plan_approval: bool = False
    compare_all: bool | None = None
    mode: Literal["adaptive", "quick", "deep"] = "adaptive"
    # Accepted for older clients, but new question-driven runs do not use a timer.
    time_limit_minutes: float | None = Field(default=None, ge=20, le=120)
    eris: bool = False  # Project Eris switch
    file_ids: list[str] = Field(default=[], max_length=20)


class ApprovePlan(BaseModel):
    note: str = ""


class SaveKey(BaseModel):
    key: str  # validated in the endpoint so error replies never echo it


class Login(BaseModel):
    code: str
