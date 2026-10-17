"""Transactional persistence for a single-machine installation.

Use a persistent local disk and one application process. An ephemeral Cloud Run
filesystem is not durable, and SQLite does not provide distributed worker leases.
Production multi-worker execution needs a transactional shared store and queue.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..files import FileStore, StoredFile
from ..models import TERMINAL, Run
from .memory import MemoryStore


class SQLiteDatabase:
    def __init__(self, path: str):
        self.path = str(Path(path).resolve())
        self.write_lock = asyncio.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, updated_at REAL NOT NULL, snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outputs (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS outputs_run ON outputs(run_id);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT NOT NULL, run_id TEXT NOT NULL, name TEXT, mime TEXT, kind TEXT,
                    size INTEGER, data BLOB, text TEXT, created_at REAL, PRIMARY KEY(run_id,id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()


async def _finish_write(fn):
    """Cancellation must not release the write lock while its thread commits.

    Return the cancellation flag so callers can update caches/output queues after
    a successful commit, before propagating cancellation to the workflow.
    """
    task = asyncio.create_task(asyncio.to_thread(fn))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


class SQLiteStore(MemoryStore):
    def __init__(self, db: SQLiteDatabase, max_runs: int = 200):
        super().__init__(max_runs=max_runs)
        self.db = db
        self._write_lock = db.write_lock

    def _restore(self, snapshot: str) -> Run:
        run = Run.model_validate_json(snapshot)
        if run.status not in TERMINAL:
            if run.clock_started_at:
                run.used_s += max(0, run.updated_at - run.clock_started_at)
            run.clock_started_at = run.deadline_at = None
            run.status = "interrupted"
        run.live = {}
        return run

    def _remember(self, run: Run) -> None:
        self._runs[run.id] = run
        excess = len(self._runs) - self._max
        if excess > 0:
            oldest = sorted((r for r in self._runs.values() if r.status in TERMINAL), key=lambda r: r.updated_at)
            for old in oldest[:excess]:
                self._runs.pop(old.id, None)

    async def load(self) -> list[Run]:
        def read():
            with self.db.connect() as db:
                return db.execute("SELECT snapshot FROM runs ORDER BY updated_at DESC LIMIT ?", (self._max,)).fetchall()
        runs = []
        for (snapshot,) in await asyncio.to_thread(read):
            run = self._restore(snapshot)
            self._remember(run)
            runs.append(run)
        return runs

    def get(self, run_id: str) -> Run | None:
        if run_id in self._runs:
            return self._runs[run_id]
        # The API's synchronous store contract permits a small indexed local read.
        with self.db.connect() as db:
            row = db.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        run = self._restore(row[0])
        self._remember(run)
        return run

    def list(self, limit: int = 50) -> list[Run]:
        limit = max(0, min(limit, 200))
        with self.db.connect() as db:
            rows = db.execute("SELECT id,snapshot FROM runs ORDER BY json_extract(snapshot,'$.created_at') DESC LIMIT ?", (limit,)).fetchall()
        # Merge durable history with live uncheckpointed state. Cache eviction never deletes history.
        combined = {rid: self._runs.get(rid) or self._restore(snapshot) for rid, snapshot in rows}
        combined.update(self._runs)
        return sorted(combined.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    async def save(self, run: Run, checkpoint: bool = False) -> None:
        if not checkpoint:
            await super().save(run)
            return
        # Snapshot before crossing the thread boundary; keep staged outputs on failure.
        snapshot = run.model_dump_json(exclude={"live", "solution"})
        async with self._write_lock:
            rows = list(self.outputs.get(run.id, []))
            def write():
                with self.db.connect() as db:
                    db.execute("INSERT INTO runs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                               "updated_at=excluded.updated_at,snapshot=excluded.snapshot",
                               (run.id, run.updated_at, snapshot))
                    db.executemany("INSERT INTO outputs(run_id,payload) VALUES (?,?)",
                                   [(run.id, json.dumps(row)) for row in rows])
            _, cancelled = await _finish_write(write)
            if rows:
                del self.outputs.setdefault(run.id, [])[:len(rows)]
        await super().save(run)
        if cancelled:
            raise asyncio.CancelledError

    async def delete_run(self, run_id: str) -> str:
        async with self._write_lock:
            def delete():
                with self.db.connect() as db:
                    for table in ("runs", "outputs", "files"):
                        key = "id" if table == "runs" else "run_id"
                        db.execute(f"DELETE FROM {table} WHERE {key}=?", (run_id,))
            _, cancelled = await _finish_write(delete)
            result = await super().delete_run(run_id)
        if cancelled:
            raise asyncio.CancelledError
        return result


class SQLiteFileStore(FileStore):
    def __init__(self, db: SQLiteDatabase, cache_runs: int = 3):
        super().__init__()
        self.db = db
        self._cache_runs = max(1, cache_runs)

    def _cache(self, run_id: str, files: list[StoredFile]) -> None:
        self._runs.pop(run_id, None)
        self._runs[run_id] = files
        while len(self._runs) > self._cache_runs:
            self._runs.pop(next(iter(self._runs)))

    async def attach(self, run_id: str, files: list[StoredFile]) -> None:
        def write():
            with self.db.connect() as db:
                db.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)", [
                    (f.id, run_id, f.name, f.mime, f.kind, f.size, f.data, f.text, f.created_at) for f in files
                ])
        async with self.db.write_lock:
            _, cancelled = await _finish_write(write)
            self._cache(run_id, files)
        if cancelled:
            raise asyncio.CancelledError

    async def get(self, run_id: str) -> list[StoredFile]:
        if run_id in self._runs:
            files = self._runs[run_id]
            self._cache(run_id, files)
            return files
        def read():
            with self.db.connect() as db:
                return db.execute("SELECT id,name,mime,kind,size,data,text,created_at FROM files "
                                  "WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        files = [StoredFile(*row) for row in await asyncio.to_thread(read)]
        self._cache(run_id, files)
        return files

    async def delete(self, run_id: str) -> None:
        # Controller deletes through SQLiteStore next, in one transaction spanning
        # the run, outputs and files. Do not partially delete files before it commits.
        await super().delete(run_id)
