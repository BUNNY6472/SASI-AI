"""Prevent internal workflow boilerplate from conflicting with user deliverables."""
import pytest

from app.models import Plan, Task
from app.workflow import prompts
from app.workflow.profiles import get_profile


REQUEST = "Calculate 6 multiplied by 7. Return the numeric answer with one short sentence. No extra text."
ANSWER = "42. Multiplying 6 by 7 means adding 6 seven times, which equals 42."


def plan_and_task():
    task = Task(id="t1", title="Calculate", description=REQUEST, status="accepted", output=ANSWER)
    return Plan(goal=REQUEST, tasks=[task], deliverables=["Numeric answer and one short sentence"],
                success_criteria=["Answer is 42", "One short explanation; no extra text"]), task


@pytest.mark.parametrize("stage", ["task", "integrate", "fix", "wrap_up"])
def test_deliverable_stages_preserve_user_format_without_mandatory_workflow_sections(stage):
    plan, task = plan_and_task()
    profile = get_profile("general")
    if stage == "task":
        system, prompt = prompts.task_prompt(plan, task, [], profile, "", request=REQUEST)
    elif stage == "integrate":
        # This always-nonempty evidence note previously triggered a comparison
        # table even though no independent analyses or sources existed.
        system, prompt = prompts.integrate_prompt(REQUEST, plan, [task], [], profile, prompts.evidence_block([]))
    elif stage == "fix":
        system, prompt = prompts.fix_prompt(REQUEST, plan, ANSWER, ["Remove extra headings"], profile)
    else:
        system, prompt = prompts.wrap_up_prompt(REQUEST, plan, profile, 1, accepted=[task])
    assert REQUEST in prompt
    assert "requested format, length, and scope take precedence" in system
    assert "only when they materially affect the answer" in system
    assert "Never conceal material uncertainty" in system
    assert "Default format when the user has not specified one" in prompt
    assert "How the models compared" not in prompt and "What the team fixed" not in prompt
    assert "End with" not in prompt


def test_review_prompts_keep_strict_json_and_real_error_checks_without_inventing_format_requirements():
    plan, task = plan_and_task()
    profile = get_profile("general")
    for builder in (
        lambda: prompts.plan_review_prompt(REQUEST, plan, profile),
        lambda: prompts.review_prompt(plan, task, ANSWER, profile, REQUEST),
        lambda: prompts.final_eval_prompt(REQUEST, plan, ANSWER, profile),
    ):
        system, prompt = builder()
        assert "Do not invent requirements" in system
        assert "Still reject factual errors, unsupported claims" in system
        assert prompts.JSON_ONLY in prompt and "Schema:" in prompt
    system, prompt = prompts.final_eval_prompt(REQUEST, plan, ANSWER, profile)
    assert "assurance limitations in the JSON limitations field" in system
    assert "not an executed test" in system
    assert '"meets_criteria": bool' in prompt and '"must_fix": [str]' in prompt
    assert "Assumptions and limitations stated" not in prompt


def test_planner_preserves_output_constraints_without_forcing_immaterial_assumptions():
    system, prompt = prompts.plan_prompt(REQUEST, "", get_profile("general"), 1)
    assert "Preserve the user's output constraints" in system
    assert "assumptions may be an empty list" in system
    assert prompts.JSON_ONLY in prompt


def test_eris_deliverable_requirements_and_unreviewed_work_caveats_remain():
    plan, task = plan_and_task()
    system, prompt = prompts.integrate_prompt("Complete the Eris solution", plan, [], [task], get_profile("eris"),
                                              drafts=[(task.title, task.output)])
    assert "exactly ONE complete solution.py" in prompt and "## Dry run" in prompt and "## Eris rule check" in prompt
    assert "do not claim independent verification" in prompt
    assert "Never conceal material uncertainty" in system
