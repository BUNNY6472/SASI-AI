"""Project record in BigQuery (dataset sasi_data).

Three tables, all keyed by run_id so one delete removes a run completely:

  run_checkpoints  full run snapshot at every meaningful step (latest row = current state)
  run_outputs      every model's output: independent analyses, cross-checks, task work,
                   reviews and the final result, so the models' work can be compared
  run_files        the files attached to a run, split into chunks

Rows are written with load jobs (free, and deletable right away) rather than
streaming inserts, so "Delete" can remove a run as soon as it is finished.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import time

from ..files import FileStore, StoredFile
from ..models import TERMINAL, Run
from .memory import MemoryStore

log = logging.getLogger("sasi.store")

CHECKPOINTS = "run_checkpoints"
OUTPUTS = "run_outputs"
FILES = "run_files"
CHUNK = 4 * 1024 * 1024
TOMBSTONE_SEQ = 1_000_000_000


async def _finish_write(fn) -> bool:
    """Keep the caller's write lock until its cloud-write thread has finished."""
    task = asyncio.create_task(asyncio.to_thread(fn))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()  # surface a failed write even if cancellation arrived meanwhile
    return cancelled


class BigQuery:
    """Thin shared client with table setup and load-job writes."""

    def __init__(self, project: str, dataset: str):
        from google.cloud import bigquery  # imported lazily so local dev needs no GCP libs at import time

        self.bq = bigquery
        self.client = bigquery.Client(project=project or None)
        self.dataset = f"{self.client.project}.{dataset}"
        self._ready = False

    def table(self, name: str) -> str:
        return f"{self.dataset}.{name}"

    def schemas(self) -> dict[str, list]:
        F = self.bq.SchemaField
        return {
            CHECKPOINTS: [
                F("run_id", "STRING", mode="REQUIRED"), F("seq", "INT64", mode="REQUIRED"), F("status", "STRING"),
                F("profile", "STRING"), F("request", "STRING"), F("total_tokens", "INT64"), F("snapshot", "JSON"),
                F("created_at", "TIMESTAMP"),
            ],
            OUTPUTS: [
                F("run_id", "STRING", mode="REQUIRED"), F("stage", "STRING"), F("provider", "STRING"), F("model", "STRING"),
                F("task_id", "STRING"), F("title", "STRING"), F("content", "STRING"), F("verdict", "STRING"),
                F("score", "FLOAT64"), F("input_tokens", "INT64"), F("output_tokens", "INT64"), F("created_at", "TIMESTAMP"),
            ],
            FILES: [
                F("run_id", "STRING", mode="REQUIRED"), F("file_id", "STRING", mode="REQUIRED"), F("chunk", "INT64"),
                F("chunks", "INT64"), F("name", "STRING"), F("mime", "STRING"), F("kind", "STRING"), F("size", "INT64"),
                F("text", "STRING"), F("data", "BYTES"), F("created_at", "TIMESTAMP"),
            ],
        }

    def ensure(self) -> None:
        if self._ready:
            return
        bq = self.bq
        for name, schema in self.schemas().items():
            table = bq.Table(self.table(name), schema=schema)
            table.time_partitioning = bq.TimePartitioning(field="created_at")
            table.clustering_fields = ["run_id"]
            self.client.create_table(table, exists_ok=True)
        self._ready = True

    def load_rows(self, name: str, rows: list[dict]) -> None:
        if not rows:
            return
        self.ensure()
        for r in rows:  # load jobs want timestamps as text, not epoch numbers
            if isinstance(r.get("created_at"), (int, float)):
                r["created_at"] = datetime.datetime.fromtimestamp(r["created_at"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
        cfg = self.bq.LoadJobConfig(schema=self.schemas()[name], write_disposition="WRITE_APPEND",
                                    source_format="NEWLINE_DELIMITED_JSON")
        self.client.load_table_from_json(rows, self.table(name), job_config=cfg).result(timeout=300)

    def query(self, sql: str, params: list | None = None):
        cfg = self.bq.QueryJobConfig(query_parameters=params or [])
        return self.client.query(sql, job_config=cfg).result(timeout=300)

    def param(self, name: str, value: str):
        return self.bq.ScalarQueryParameter(name, "STRING", value)


class BigQueryStore(MemoryStore):
    def __init__(self, project: str, dataset: str, bq: BigQuery | None = None):
        super().__init__()
        self.db = bq or BigQuery(project, dataset)
        self._seq: dict[str, int] = {}
        self._pending_delete: set[str] = set()
        self._lock = asyncio.Lock()

    async def load(self) -> list[Run]:
        db = self.db

        def _load() -> list[Run]:
            db.ensure()
            sql = f"""
                SELECT run_id, seq, status, TO_JSON_STRING(snapshot) AS snapshot FROM (
                  SELECT * FROM `{db.table(CHECKPOINTS)}`
                  WHERE created_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
                  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY seq DESC) = 1
                ) ORDER BY created_at DESC LIMIT 200
            """
            runs = []
            for row in db.query(sql):
                if row["status"] == "deleted":
                    self._pending_delete.add(row["run_id"])
                    continue
                try:
                    data = json.loads(row["snapshot"])
                    if isinstance(data, str):
                        data = json.loads(data)
                    run = Run.model_validate(data)
                except Exception:  # noqa: BLE001 - skip unreadable history rows
                    continue
                self._seq[run.id] = row["seq"]
                runs.append(run)
            return runs

        try:
            runs = await asyncio.to_thread(_load)
        except Exception as e:
            log.warning("could not load history from BigQuery: %s", e)
            raise RuntimeError("Run history is unavailable; refusing to start with an empty history.") from e
        for run in runs:
            if run.status not in TERMINAL:
                # The process stopped mid-run. Keep accepted work; offer resume.
                if run.clock_started_at:
                    run.used_s += max(0, min(run.updated_at, time.time()) - run.clock_started_at)
                run.clock_started_at = run.deadline_at = None
                run.status = "interrupted"
            run.live = {}
            await super().save(run)
        if self._pending_delete:
            asyncio.create_task(self._retry_deletes())
        return runs

    async def save(self, run: Run, checkpoint: bool = False) -> None:
        await super().save(run)
        if not checkpoint:
            return
        async with self._lock:  # include actual thread lifetime, even after caller cancellation
            seq = self._seq.get(run.id, 0) + 1
            self._seq[run.id] = seq
            row = {
                "run_id": run.id,
                "seq": seq,
                "status": run.status,
                "profile": run.profile,
                "request": run.request[:2000],
                "total_tokens": run.usage.total_tokens,
                "snapshot": run.model_dump(mode="json", exclude={"live"}),
                "created_at": time.time(),
            }
            # Retain the queue until both writes succeed. An ambiguous cloud
            # response can still produce duplicate append rows on retry.
            outputs = list(self.outputs.get(run.id, []))

            def _write() -> None:
                self.db.load_rows(CHECKPOINTS, [row])
                self.db.load_rows(OUTPUTS, outputs)

            try:
                cancelled = await _finish_write(_write)
            except Exception as e:
                log.warning("could not checkpoint run %s: %s", run.id, e)
                raise RuntimeError("The checkpoint could not be saved. Work has stopped to preserve recoverability.") from e
            if outputs and run.id in self.outputs:
                del self.outputs[run.id][:len(outputs)]
        if cancelled:
            raise asyncio.CancelledError

    async def delete_run(self, run_id: str) -> str:
        await super().delete_run(run_id)
        self._seq.pop(run_id, None)
        db = self.db

        def _delete() -> None:
            p = [db.param("id", run_id)]
            for name in (CHECKPOINTS, OUTPUTS, FILES):
                db.query(f"DELETE FROM `{db.table(name)}` WHERE run_id = @id", p)

        try:
            async with self._lock:
                await asyncio.to_thread(_delete)
            self._pending_delete.discard(run_id)
            return "removed"
        except Exception as e:  # noqa: BLE001
            # Usually rows written by the old streaming path that BigQuery won't delete yet.
            log.warning("delete of run %s postponed: %s", run_id, e)
            self._pending_delete.add(run_id)
            tomb = {"run_id": run_id, "seq": TOMBSTONE_SEQ, "status": "deleted", "snapshot": {}, "created_at": time.time()}
            try:
                await asyncio.to_thread(db.load_rows, CHECKPOINTS, [tomb])
            except Exception as e2:  # noqa: BLE001
                log.warning("could not mark run %s deleted: %s", run_id, e2)
            asyncio.create_task(self._retry_deletes())
            return "pending"

    async def _retry_deletes(self) -> None:
        for _ in range(12):
            await asyncio.sleep(600)
            for run_id in list(self._pending_delete):
                if await self.delete_run(run_id) == "removed":
                    log.info("finished deleting run %s", run_id)
            if not self._pending_delete:
                return


class BigQueryFileStore(FileStore):
    """Files saved in BigQuery next to the run; cached in memory while the run is open."""

    def __init__(self, db: BigQuery, cache_runs: int = 3):
        super().__init__()
        self.db = db
        self._cache_runs = cache_runs

    def _cache(self, run_id: str, files: list[StoredFile]) -> None:
        self._runs.pop(run_id, None)
        self._runs[run_id] = files
        while len(self._runs) > self._cache_runs:
            self._runs.pop(next(iter(self._runs)))

    async def attach(self, run_id: str, files: list[StoredFile]) -> None:
        if not files:
            return
        self._cache(run_id, files)
        now = time.time()
        rows = []
        for f in files:
            chunks = max(1, -(-f.size // CHUNK))
            for i in range(chunks):
                part = f.data[i * CHUNK:(i + 1) * CHUNK]
                rows.append({"run_id": run_id, "file_id": f.id, "chunk": i, "chunks": chunks, "name": f.name, "mime": f.mime,
                             "kind": f.kind, "size": f.size, "text": f.text if i == 0 else None,
                             "data": base64.b64encode(part).decode(), "created_at": now})
        # One load job per few chunks keeps each request well under the upload limits.
        batches: list[list[dict]] = [[]]
        for r in rows:
            if len(batches[-1]) >= 4:
                batches.append([])
            batches[-1].append(r)
        for batch in batches:
            await asyncio.to_thread(self.db.load_rows, FILES, batch)

    async def get(self, run_id: str) -> list[StoredFile]:
        if run_id in self._runs:
            return self._runs[run_id]
        db = self.db

        def _read() -> list[StoredFile]:
            db.ensure()
            sql = f"SELECT * FROM `{db.table(FILES)}` WHERE run_id = @id ORDER BY file_id, chunk"
            files: dict[str, StoredFile] = {}
            parts: dict[str, list[bytes]] = {}
            for row in db.query(sql, [db.param("id", run_id)]):
                fid = row["file_id"]
                if fid not in files:
                    files[fid] = StoredFile(id=fid, name=row["name"], mime=row["mime"], kind=row["kind"], size=row["size"],
                                            data=b"", text=row["text"] or "", created_at=row["created_at"].timestamp())
                    parts[fid] = []
                parts[fid].append(row["data"] or b"")
            for fid, f in files.items():
                f.data = b"".join(parts[fid])
            return sorted(files.values(), key=lambda f: f.created_at)

        try:
            files = await asyncio.to_thread(_read)
        except Exception as e:  # noqa: BLE001
            log.warning("could not read files for run %s: %s", run_id, e)
            return []
        if files:
            self._cache(run_id, files)
        return files

    async def delete(self, run_id: str) -> None:
        # The run delete removes the rows from every table; just drop the cache here.
        self._runs.pop(run_id, None)
