import asyncio
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from app.files import StoredFile
from app.models import Run
from app.store.sqlite import SQLiteDatabase, SQLiteFileStore, SQLiteStore


def test_old_runs_remain_retrievable_after_cache_eviction_and_restart(tmp_path):
    async def scenario():
        db = SQLiteDatabase(str(tmp_path / "history.sqlite3"))
        store = SQLiteStore(db, max_runs=2)
        runs = [Run(request=f"Question {i}", status="completed", created_at=float(i), updated_at=float(i)) for i in range(5)]
        for run in runs:
            await store.save(run, checkpoint=True)
        assert len(store._runs) == 2
        assert store.get(runs[0].id).request == "Question 0"
        assert len(store._runs) == 2
        restarted = SQLiteStore(db, max_runs=2)
        assert len(await restarted.load()) == 2
        assert restarted.get(runs[0].id).id == runs[0].id
        assert [r.id for r in restarted.list(5)] == [r.id for r in reversed(runs)]
        await restarted.delete_run(runs[0].id)
        assert restarted.get(runs[0].id) is None
    asyncio.run(scenario())


def test_checkpoint_failure_propagates_and_keeps_staged_outputs(tmp_path, monkeypatch):
    async def scenario():
        db = SQLiteDatabase(str(tmp_path / "history.sqlite3"))
        store = SQLiteStore(db)
        run = Run(request="A question")
        store.record_output(run.id, {"stage": "answer", "content": "saved when commit succeeds"})
        @contextmanager
        def fail():
            raise sqlite3.OperationalError("disk full")
            yield
        monkeypatch.setattr(db, "connect", fail)
        with pytest.raises(sqlite3.OperationalError):
            await store.save(run, checkpoint=True)
        assert run.id not in store._runs
        assert len(store.outputs[run.id]) == 1
    asyncio.run(scenario())


def test_restart_marks_running_work_interrupted_without_restarting_it(tmp_path):
    async def scenario():
        db = SQLiteDatabase(str(tmp_path / "history.sqlite3"))
        store = SQLiteStore(db)
        run = Run(request="Running", status="planning", clock_started_at=100, updated_at=120, used_s=3)
        await store.save(run, checkpoint=True)
        loaded = (await SQLiteStore(db).load())[0]
        assert loaded.status == "interrupted" and loaded.used_s == 23
        assert loaded.deadline_at is None and loaded.clock_started_at is None
    asyncio.run(scenario())


def test_files_use_bounded_cache_and_run_delete_removes_all_tables(tmp_path):
    async def scenario():
        db = SQLiteDatabase(str(tmp_path / "history.sqlite3"))
        store, files = SQLiteStore(db), SQLiteFileStore(db, cache_runs=1)
        runs = [Run(request=f"Question {i}") for i in range(2)]
        for i, run in enumerate(runs):
            await files.attach(run.id, [StoredFile(str(i), "a.txt", "text/plain", "text", 1, b"x", "x")])
            store.record_output(run.id, {"content": "output"})
            await store.save(run, checkpoint=True)
        assert len(files._runs) == 1
        assert (await files.get(runs[0].id))[0].data == b"x"
        assert len(files._runs) == 1
        await files.delete(runs[0].id)
        await store.delete_run(runs[0].id)
        assert not await files.get(runs[0].id)
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM outputs WHERE run_id=?", (runs[0].id,)).fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM files WHERE run_id=?", (runs[0].id,)).fetchone()[0] == 0
    asyncio.run(scenario())


def test_cancelled_checkpoint_finishes_before_delete_and_cannot_resurrect_run(tmp_path, monkeypatch):
    async def scenario():
        db = SQLiteDatabase(str(tmp_path / "history.sqlite3"))
        store = SQLiteStore(db)
        run = Run(request="Cancellation race")
        started, release = threading.Event(), threading.Event()
        original = db.connect
        calls = []
        @contextmanager
        def delayed():
            calls.append(1)
            if len(calls) == 1:
                started.set()
                assert release.wait(3)
            with original() as conn:
                yield conn
        monkeypatch.setattr(db, "connect", delayed)
        store.record_output(run.id, {"content": "once"})
        writing = asyncio.create_task(store.save(run, checkpoint=True))
        assert await asyncio.to_thread(started.wait, 2)
        writing.cancel()
        deleting = asyncio.create_task(store.delete_run(run.id))
        await asyncio.sleep(0.02)
        assert not deleting.done(), "delete must wait for the cancelled write's thread"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing
        await deleting
        assert store.get(run.id) is None
        with original() as conn:
            assert conn.execute("SELECT COUNT(*) FROM outputs").fetchone()[0] == 0
    asyncio.run(scenario())
