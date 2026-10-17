"""Cloud checkpoint lifetime tests with a local fake database; no cloud calls."""
import asyncio
import threading

import pytest

from app.models import Run
from app.store.bigquery import BigQueryStore, CHECKPOINTS, OUTPUTS


@pytest.mark.parametrize("fail", [False, True])
def test_cancelled_checkpoint_keeps_lock_and_outputs_until_actual_write_finishes(fail):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        writes = []

        class FakeDB:
            def load_rows(self, table, rows):
                if table == CHECKPOINTS:
                    entered.set()
                    assert release.wait(3)
                    if fail:
                        raise OSError("cloud write unavailable")
                writes.append((table, list(rows)))

        store = BigQueryStore("", "", FakeDB())
        run = Run(request="Preserve accepted work")
        store.record_output(run.id, {"content": "accepted output"})
        writing = asyncio.create_task(store.save(run, checkpoint=True))
        assert await asyncio.to_thread(entered.wait, 2)
        writing.cancel()
        waiting = asyncio.create_task(store._lock.acquire())
        try:
            await asyncio.sleep(0.02)
            assert not writing.done() and not waiting.done()
            assert len(store.outputs[run.id]) == 1
            # A newer result may arrive while the prior batch is being saved.
            store.record_output(run.id, {"content": "new output"})
        finally:
            release.set()
        if fail:
            with pytest.raises(RuntimeError, match="checkpoint could not be saved") as error:
                await writing
            assert isinstance(error.value.__cause__, OSError)
            assert len(store.outputs[run.id]) == 2
        else:
            with pytest.raises(asyncio.CancelledError):
                await writing
            assert store.outputs[run.id] == [{"content": "new output"}]
            assert [rows for table, rows in writes if table == OUTPUTS] == [[{"content": "accepted output"}]]
        await asyncio.wait_for(waiting, 1)
        store._lock.release()
    asyncio.run(scenario())


def test_reload_freezes_interrupted_clock_at_last_checkpoint():
    async def scenario():
        run = Run(request="Long research task", status="running", used_s=20,
                  clock_started_at=100, updated_at=115, deadline_at=3700)

        class FakeDB:
            def ensure(self):
                pass
            def table(self, name):
                return name
            def query(self, sql):
                return [{"run_id": run.id, "seq": 1, "status": "running", "snapshot": run.model_dump_json()}]

        restored = (await BigQueryStore("", "", FakeDB()).load())[0]
        assert restored.status == "interrupted"
        assert restored.used_s == restored.elapsed_s == 35
        assert restored.clock_started_at is None and restored.deadline_at is None
    asyncio.run(scenario())
