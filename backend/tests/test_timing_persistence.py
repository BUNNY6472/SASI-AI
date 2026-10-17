"""Durable timing checkpoints, using local SQLite and a fake BigQuery database.

These tests never construct a cloud client or contact a model provider.
"""
import asyncio
import copy
import json

import pytest

from app.models import CallTiming, LiveText, PartialOutput, Plan, Run, Task
from app.store.bigquery import BigQueryStore, CHECKPOINTS
from app.store.sqlite import SQLiteDatabase, SQLiteStore


class FakeBigQuery:
    """Mimic JSON snapshot storage and the latest-checkpoint read contract."""

    def __init__(self):
        self.rows = []

    def ensure(self):
        pass

    def table(self, name):
        return name

    def load_rows(self, table, rows):
        if table == CHECKPOINTS:
            self.rows.extend(copy.deepcopy(rows))

    def query(self, sql):
        latest = {}
        for row in self.rows:
            previous = latest.get(row["run_id"])
            if previous is None or row["seq"] > previous["seq"]:
                latest[row["run_id"]] = row
        return [
            {**copy.deepcopy(row), "snapshot": json.dumps(row["snapshot"])}
            for row in latest.values()
        ]


@pytest.fixture(params=["sqlite", "bigquery"])
def durable_store(request, tmp_path):
    """Return fresh-store construction plus insertion of a historical snapshot."""
    if request.param == "sqlite":
        db = SQLiteDatabase(str(tmp_path / "timing.sqlite3"))

        def create():
            return SQLiteStore(db)

        def insert(snapshot):
            with db.connect() as conn:
                conn.execute("INSERT INTO runs VALUES (?,?,?)", (
                    snapshot["id"], snapshot["updated_at"], json.dumps(snapshot)))

    else:
        db = FakeBigQuery()

        def create():
            return BigQueryStore("", "", db)

        def insert(snapshot):
            db.load_rows(CHECKPOINTS, [{
                "run_id": snapshot["id"], "seq": 1, "status": snapshot["status"],
                "snapshot": snapshot, "created_at": snapshot["updated_at"],
            }])

    return create, insert


def test_interrupted_timing_and_partial_work_survive_durable_reload(durable_store):
    async def scenario():
        create, _ = durable_store
        run = Run(
            request="Investigate a long-running research question", status="analyzing",
            timing_policy="graceful", time_limit_minutes=20, time_limit_s=1200,
            model_phase_min_s=1200, model_phase_grace_s=300,
            total_time_limit_s=6600,
            finalization_deadline_elapsed_s=1800,
            used_s=20, clock_started_at=100, updated_at=115, deadline_at=1280,
            call_elapsed_s={"analysis:claude": 735.25, "task:t2": 82.5},
            call_allowance_s={"analysis:claude": 1500, "task:t2": 1500},
            call_completed_keys=["gemini:task:t1:"],
            partial_outputs=[
                PartialOutput(provider="claude", model="test-model", kind="analysis",
                              label="Independent analysis", text="Unfinished source comparison",
                              reason="Application stopped", created_at=110),
                PartialOutput(provider="gemini", model="test-model", kind="task",
                              task_id="t2", label="Check claim", text="Unfinished calculation",
                              reason="Time allowance exhausted", created_at=112),
            ],
            plan=Plan(tasks=[
                Task(id="t1", title="Read sources", status="accepted", output="Reviewed source notes"),
                Task(id="t2", title="Check claim", status="pending", depends_on=["t1"]),
            ]),
            live={"analysis:claude": LiveText(provider="claude", label="Analysis",
                                             text="Ephemeral streaming display")},
        )
        expected_partials = [item.model_dump() for item in run.partial_outputs]
        await create().save(run, checkpoint=True)

        restored = (await create().load())[0]
        assert restored.status == "interrupted"
        assert restored.used_s == restored.elapsed_s == 35
        assert restored.clock_started_at is None and restored.deadline_at is None
        assert restored.live == {}, "transient stream displays must not appear to remain active"
        assert restored.timing_policy == "graceful"
        assert restored.model_phase_min_s == 1200 and restored.model_phase_grace_s == 300
        assert restored.total_time_limit_s == 6600, "a stricter saved total cap must not reset to 120 minutes"
        assert restored.call_elapsed_s == run.call_elapsed_s
        assert restored.call_allowance_s == run.call_allowance_s
        assert restored.call_completed_keys == ["gemini:task:t1:"]
        assert restored.finalization_deadline_elapsed_s == 1800
        assert [item.model_dump() for item in restored.partial_outputs] == expected_partials
        assert all(item.usage_estimated for item in restored.partial_outputs)

        assert not restored.analyses and not restored.cross_checks
        assert restored.plan.tasks[0].status == "accepted"
        assert restored.plan.tasks[0].output == "Reviewed source notes"
        pending = restored.plan.tasks[1]
        assert pending.status == "pending" and not pending.output and not pending.attempts
        assert restored.final is None, "saved partials must never become a completed deliverable"

    asyncio.run(scenario())


def test_latest_checkpoint_keeps_spent_call_budget_and_finalization_deadline(durable_store):
    async def scenario():
        create, _ = durable_store
        store = create()
        run = Run(request="Resume a partially finished analysis", status="interrupted",
                  timing_policy="graceful", used_s=400, updated_at=100,
                  call_elapsed_s={"analysis:claude": 400},
                  call_allowance_s={"analysis:claude": 1500},
                  finalization_deadline_elapsed_s=1700)
        await store.save(run, checkpoint=True)
        run.used_s = 900
        run.updated_at = 200
        run.call_elapsed_s["analysis:claude"] = 900
        run.partial_outputs.append(PartialOutput(
            provider="claude", kind="analysis", text="Checkpointed unfinished finding",
            reason="Interrupted again", created_at=200))
        await store.save(run, checkpoint=True)

        restored = (await create().load())[0]
        assert restored.used_s == 900
        assert restored.call_elapsed_s == {"analysis:claude": 900}
        assert restored.call_allowance_s == {"analysis:claude": 1500}
        assert restored.finalization_deadline_elapsed_s == 1700
        assert len(restored.partial_outputs) == 1
        assert restored.partial_outputs[0].text == "Checkpointed unfinished finding"
        assert restored.analyses == []

    asyncio.run(scenario())


def test_legacy_five_minute_snapshot_loads_without_new_timing_fields(durable_store):
    async def scenario():
        create, insert = durable_store
        # Deliberately insert the old schema, rather than serializing a new Run
        # whose defaults would accidentally make this compatibility check pass.
        insert({
            "id": "legacy-five-minute", "request": "An older five-minute request",
            "status": "analyzing", "created_at": 90, "updated_at": 115,
            "time_limit_minutes": 5, "time_limit_s": 300, "wrap_up_s": 60,
            "used_s": 20, "clock_started_at": 100, "deadline_at": 380,
            "analyses": [],
        })
        loaded = await create().load()
        assert len(loaded) == 1, "historical runs must not be silently discarded"
        restored = loaded[0]
        assert restored.id == "legacy-five-minute" and restored.status == "interrupted"
        assert restored.time_limit_minutes == 5 and restored.time_limit_s == 300
        assert restored.wrap_up_s == 60 and restored.used_s == 35
        assert restored.timing_policy == "strict"
        assert restored.partial_outputs == []
        assert restored.call_elapsed_s == {} and restored.call_allowance_s == {}
        assert restored.finalization_deadline_elapsed_s is None
        assert restored.total_time_limit_s == 7200, "legacy records receive the bounded 120-minute total cap"

    asyncio.run(scenario())


def interrupted_call_run():
    return Run(
        request="Resume the same model phase after a crash", status="analyzing",
        timing_policy="graceful", updated_at=115, clock_started_at=100, used_s=20,
        call_elapsed_s={"claude:analysis::": 400, "gemini:analysis::": 200},
        call_allowance_s={"claude:analysis::": 1200, "gemini:analysis::": 1200},
        total_time_limit_s=6600,
        finalization_deadline_elapsed_s=1800,
        active_calls={
            "claude:analysis::": CallTiming(provider="claude", kind="analysis",
                started_at=100, soft_deadline_at=900, hard_deadline_at=1200),
            "gemini:analysis::": CallTiming(provider="gemini", kind="analysis",
                started_at=120, soft_deadline_at=1120, hard_deadline_at=1420),
        },
    )


@pytest.mark.parametrize("current_time,charged", [(110, 10), (200, 15)])
def test_active_call_recovery_charges_checkpointed_time_once(
        durable_store, monkeypatch, current_time, charged):
    monkeypatch.setattr("app.store.recovery.time", lambda: current_time)

    async def scenario():
        create, _ = durable_store
        run = interrupted_call_run()
        await create().save(run, checkpoint=True)
        recovered_store = create()
        restored = (await recovered_store.load())[0]
        assert restored.active_calls == {}
        assert restored.call_elapsed_s == {
            "claude:analysis::": 400 + charged, "gemini:analysis::": 200,
        }, "charge only the current invocation, never negative time or downtime"
        assert restored.call_allowance_s == run.call_allowance_s
        assert restored.finalization_deadline_elapsed_s == 1800
        assert restored.total_time_limit_s == 6600
        assert restored.used_s == 20 + charged
        assert not restored.analyses and restored.final is None

        await recovered_store.save(restored, checkpoint=True)
        reloaded = (await create().load())[0]
        assert reloaded.active_calls == {}
        assert reloaded.call_elapsed_s == restored.call_elapsed_s
        assert reloaded.used_s == restored.used_s, "saving recovered state must prevent double charging"
        assert reloaded.total_time_limit_s == 6600

    asyncio.run(scenario())


@pytest.mark.parametrize("lookup", ["get", "list"])
def test_sqlite_uncached_lookup_reconciles_active_calls(tmp_path, monkeypatch, lookup):
    monkeypatch.setattr("app.store.recovery.time", lambda: 200)

    async def scenario():
        db = SQLiteDatabase(str(tmp_path / "uncached-timing.sqlite3"))
        run = interrupted_call_run()
        await SQLiteStore(db).save(run, checkpoint=True)
        restarted = SQLiteStore(db)
        assert not restarted._runs, "exercise retrieval without calling load first"
        restored = restarted.get(run.id) if lookup == "get" else restarted.list()[0]
        assert restored.status == "interrupted" and restored.active_calls == {}
        assert restored.call_elapsed_s == {"claude:analysis::": 415, "gemini:analysis::": 200}
        assert restored.used_s == 35
        repeated = restarted.get(run.id) if lookup == "get" else restarted.list()[0]
        assert repeated.call_elapsed_s == restored.call_elapsed_s
        assert repeated.used_s == restored.used_s

    asyncio.run(scenario())
