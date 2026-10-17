"""Stand-in agents used until real API keys are added.

They follow the same contract as real adapters and return well-formed outputs,
so the whole workflow (plan, review, parallel work, gates, integration) can be
exercised end to end. Every output is clearly labelled as simulated.
"""
from __future__ import annotations

import asyncio
import json
import random

from .base import ModelCall, ModelResult

PERSONAS = {
    "claude": ("Claude", "careful structure and edge cases"),
    "chatgpt": ("ChatGPT", "practical implementation detail"),
    "gemini": ("Gemini", "breadth of alternatives and data"),
}


class SimulatedAdapter:
    simulated = True

    def __init__(self, provider: str, delay: float = 0.6):
        self.provider = provider
        self.display_name = PERSONAS[provider][0]
        self._delay = delay

    async def list_models(self) -> list[str]:
        return [f"simulated-{self.provider}"]

    async def resolve_model(self) -> str:
        return f"simulated-{self.provider}"

    async def generate(self, call: ModelCall) -> ModelResult:
        await asyncio.sleep(self._delay * (0.5 + random.random()))
        text = self._respond(call)
        return ModelResult(
            provider=self.provider,
            model=f"simulated-{self.provider}",
            text=text,
            input_tokens=len(call.prompt) // 4,
            output_tokens=len(text) // 4,
            stop_reason="end_turn",
            simulated=True,
        )

    def _respond(self, call: ModelCall) -> str:
        name, angle = PERSONAS[self.provider]
        request = call.hints.get("request", "the request")
        if call.kind == "plan":
            return json.dumps({
                "goal": f"Deliver a clear, checked answer to: {request}",
                "assumptions": ["Demo mode: no provider keys are connected yet."],
                "deliverables": ["Final written deliverable with evidence and limitations"],
                "success_criteria": [
                    "Addresses every part of the request",
                    "States assumptions and limitations",
                    "Each section was independently reviewed",
                ],
                "tasks": [
                    {"id": "t1", "title": "Clarify requirements and constraints", "description": "List requirements, constraints and open questions.", "depends_on": []},
                    {"id": "t2", "title": "Draft the core solution", "description": "Produce the main content of the deliverable.", "depends_on": ["t1"]},
                    {"id": "t3", "title": "Identify risks and alternatives", "description": "Collect risks, trade-offs and alternatives.", "depends_on": ["t1"]},
                ],
            })
        if call.kind in ("plan_review", "review"):
            # Occasionally ask for one revision so the revise path is visible in demos.
            verdict = "revise" if (call.kind == "review" and not call.hints.get("is_revision") and random.random() < 0.25) else "pass"
            if call.kind == "plan_review":
                verdict = "accept"
            return json.dumps({
                "verdict": verdict,
                "score": 8 if verdict in ("pass", "accept") else 5,
                "findings": [f"[{name}, simulated] Checked for {angle}."] + (["Add one concrete example."] if verdict == "revise" else []),
            })
        if call.kind == "final_eval":
            return json.dumps({
                "meets_criteria": True,
                "score": 8,
                "gaps": [],
                "limitations": ["Produced by simulated agents. Add API keys for real answers."],
            })
        title = call.hints.get("task_title", "the task")
        if call.kind == "integrate":
            return (
                f"# Result\n\n_Simulated output assembled by {name}. Connect real API keys to get real answers._\n\n"
                f"**Request:** {request}\n\n"
                + "\n\n".join(f"## {t}\n{c}" for t, c in call.hints.get("sections", []))
            )
        return (
            f"_Simulated work by {name}, focused on {angle}._\n\n"
            f"- Key point about **{title}** for the request.\n"
            f"- A second consideration a real model would expand on.\n"
            + ("- Added a concrete example after review.\n" if call.hints.get("is_revision") else "")
        )
