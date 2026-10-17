"""Reconcile checkpointed clocks after a process stops mid-run."""
from time import time

from ..models import TERMINAL, Run


def restore_interrupted(run: Run) -> Run:
    """Charge only checkpointed work, then remove transient active state.

    Each active call's start time belongs to its current invocation; the saved
    call_elapsed_s already accounts for earlier invocations. Clearing active_calls
    makes this operation idempotent, including after the recovered run is saved.
    Downtime after the last checkpoint never consumes a model's allowance.
    """
    checkpoint_at = min(run.updated_at, time())
    for key, timing in run.active_calls.items():
        spent = max(0.0, checkpoint_at - timing.started_at)
        run.call_elapsed_s[key] = run.call_elapsed_s.get(key, 0.0) + spent
    run.active_calls = {}
    if run.status not in TERMINAL:
        if run.clock_started_at is not None:
            run.used_s += max(0.0, checkpoint_at - run.clock_started_at)
        run.clock_started_at = run.deadline_at = None
        run.status = "interrupted"
    run.live = {}
    return run
