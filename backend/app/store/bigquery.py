"""Project record in BigQuery (dataset sasi_data).

Append-only checkpoints: every meaningful step writes a full snapshot row, and
the latest row per run is the current state. This keeps an audit history and
lets accepted work survive restarts, at free-tier scale.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from ..models import TERMINAL, Run
from .memory import MemoryStore

log = logging.getLogger("sasi.store")

TABLE = "run_checkpoints"


class BigQueryStore(MemoryStore):
    def __init__(self, project: str, dataset: str):
        super().__init__()
        from google.cloud import bigquery  # imported lazily so local dev needs no GCP libs at import time

        self._bq = bigquery
        self._client = bigquery.Client(project=project or None)
        self._table_id = f"{self._client.project}.{dataset}.{TABLE}"
        self._seq: dict[str, int] = {}

    def _ensure_table(self) -> None:
        bq = self._bq
        schema = [
            bq.SchemaField("run_id", "STRING", mode="REQUIRED"),
            bq.SchemaField("seq", "INT64", mode="REQUIRED"),
            bq.SchemaField("status", "STRING"),
            bq.SchemaField("profile", "STRING"),
            bq.SchemaField("request", "STRING"),
            bq.SchemaField("total_tokens", "INT64"),
            bq.SchemaField("snapshot", "JSON"),
            bq.SchemaField("created_at", "TIMESTAMP"),
        ]
        table = bq.Table(self._table_id, schema=schema)
        table.time_partitioning = bq.TimePartitioning(field="created_at")
        table.clustering_fields = ["run_id"]
        self._client.create_table(table, exists_ok=True)

    async def load(self) -> list[Run]:
        def _load() -> list[Run]:
            self._ensure_table()
            sql = f"""
                SELECT run_id, seq, TO_JSON_STRING(snapshot) AS snapshot FROM `{self._table_id}`
                WHERE created_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
                QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY seq DESC) = 1
                ORDER BY created_at DESC LIMIT 200
            """
            runs = []
            for row in self._client.query(sql).result():
                try:
                    run = Run.model_validate_json(row["snapshot"])
                except Exception:  # noqa: BLE001 - skip unreadable history rows
                    continue
                self._seq[run.id] = row["seq"]
                runs.append(run)
            return runs

        try:
            runs = await asyncio.to_thread(_load)
        except Exception as e:  # noqa: BLE001 - app still works without history
            log.warning("could not load history from BigQuery: %s", e)
            return []
        for run in runs:
            if run.status not in TERMINAL:
                # The process stopped mid-run. Keep accepted work; offer resume.
                run.status = "interrupted"
            await super().save(run)
        return runs

    async def save(self, run: Run, checkpoint: bool = False) -> None:
        await super().save(run)
        if not checkpoint:
            return
        seq = self._seq.get(run.id, 0) + 1
        self._seq[run.id] = seq
        row = {
            "run_id": run.id,
            "seq": seq,
            "status": run.status,
            "profile": run.profile,
            "request": run.request[:2000],
            "total_tokens": run.usage.total_tokens,
            "snapshot": json.dumps(run.model_dump(mode="json")),
            "created_at": time.time(),
        }

        def _insert() -> None:
            errors = self._client.insert_rows_json(self._table_id, [row])
            if errors:
                log.warning("BigQuery insert errors: %s", errors)

        try:
            await asyncio.to_thread(_insert)
        except Exception as e:  # noqa: BLE001 - never fail a run because history failed
            log.warning("could not checkpoint run %s: %s", run.id, e)
