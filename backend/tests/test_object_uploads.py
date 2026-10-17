"""Large-upload admission, durable metadata, private downloads, and bounded reads.

The fake stores only small prefixes even when declared size is 2 GiB. No cloud
credentials, large fixture allocation, or provider calls are needed.
"""
import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound, PreconditionFailed

from app.files import MAX_FILE_BYTES, MAX_RUN_BYTES, FileRejected, FileStore, StoredFile
from app.main import download_range
from app.store import objects
from app.store.objects import INDEX, ObjectFileStore
from tests.test_access_token import OWNER_TOKEN, settings


class FakeBlob:
    def __init__(self, bucket, key, generation=None):
        self.bucket, self.key, self.generation = bucket, key, generation
        self.metadata, self.content_type, self.size = None, None, None

    def reload(self, **kwargs):
        if self.key not in self.bucket.rows:
            raise NotFound("missing")
        row = self.bucket.rows[self.key]
        for key in ("generation", "metadata", "content_type", "size"):
            setattr(self, key, copy.deepcopy(row[key]))

    def download_as_bytes(self, **kwargs):
        self.bucket.reads.append((self.key, kwargs))
        row = self.bucket.rows.get(self.key)
        if not row:
            raise NotFound("missing")
        if kwargs.get("if_generation_match", row["generation"]) != row["generation"]:
            raise PreconditionFailed("changed")
        data = row["data"]
        return data[kwargs.get("start", 0):kwargs.get("end", len(data) - 1) + 1]

    def upload_from_string(self, data, content_type, **kwargs):
        current = self.bucket.rows.get(self.key, {}).get("generation", 0)
        if kwargs.get("if_generation_match", current) != current:
            raise PreconditionFailed("changed")
        if self.bucket.conflict_next and self.key == INDEX:
            self.bucket.conflict_next = False
            raise PreconditionFailed("concurrent writer")
        self.bucket.seq += 1
        self.generation = self.bucket.seq
        data = data.encode() if isinstance(data, str) else data
        self.bucket.rows[self.key] = {"generation": self.generation, "data": data, "size": len(data),
                                      "content_type": content_type, "metadata": self.metadata}

    def create_resumable_upload_session(self, **kwargs):
        self.bucket.sessions[self.key] = {**kwargs, "metadata": self.metadata}
        return "https://storage.googleapis.com/upload/" + self.key

    def delete(self, **kwargs):
        if self.key not in self.bucket.rows:
            raise NotFound("missing")
        self.bucket.rows.pop(self.key)


class FakeBucket:
    def __init__(self):
        self.rows, self.sessions, self.reads = {}, {}, []
        self.seq, self.conflict_next = 0, False

    def blob(self, key, generation=None):
        return FakeBlob(self, key, generation)

    def finish(self, fid, data=b"a,b\n1,2\n", size=None):
        key = "objects/" + fid
        request = self.sessions[key]
        blob = self.blob(key)
        blob.metadata = request["metadata"]
        blob.upload_from_string(data, request["content_type"])
        self.rows[key]["size"] = request["size"] if size is None else size


@pytest.fixture
def storage(monkeypatch):
    bucket = FakeBucket()
    client = SimpleNamespace(bucket=lambda name: bucket)
    store = ObjectFileStore("private-uploads", client=client)
    monkeypatch.setattr("requests.delete", lambda *a, **k: SimpleNamespace(status_code=499))
    return store, bucket, client


def test_maximum_file_reserves_whole_aggregate_before_bytes_arrive(storage):
    store, bucket, _ = storage
    async def run():
        first = await store.initiate("../../data.csv", "text/csv", MAX_FILE_BYTES, "https://sasi.test")
        request = bucket.sessions["objects/" + first["id"]]
        assert request["size"] == 2 * 1024**3
        assert request["if_generation_match"] == 0 and request["origin"] == "https://sasi.test"
        with pytest.raises(FileRejected, match="total at most 2 GiB"):
            await store.initiate("another.csv", "text/csv", 1, "https://sasi.test")
        assert len(bucket.sessions) == 1
        row = json.loads(bucket.rows[INDEX]["data"])[first["id"]]
        assert "/" not in row["name"] and row["state"] == "pending"
        await store.unstage_async(first["id"])
        assert json.loads(bucket.rows[INDEX]["data"]) == {}
    asyncio.run(run())


def test_aggregate_is_shared_across_instances_and_cas_retries(storage):
    first, bucket, client = storage
    second = ObjectFileStore("private-uploads", client=client)
    async def run():
        bucket.conflict_next = True
        await first.initiate("first.csv", "text/csv", MAX_RUN_BYTES - 1, "https://sasi.test")
        await second.initiate("last.txt", "text/plain", 1, "https://sasi.test")
        with pytest.raises(FileRejected):
            await first.initiate("overflow.txt", "text/plain", 1, "https://sasi.test")
        assert len(json.loads(bucket.rows[INDEX]["data"])) == 2
    asyncio.run(run())


@pytest.mark.parametrize("size", [0, -1, MAX_FILE_BYTES + 1, True])
def test_invalid_size_never_reserves_cloud_session(storage, size):
    store, bucket, _ = storage
    with pytest.raises(FileRejected):
        asyncio.run(store.initiate("bad.bin", "", size, "https://sasi.test"))
    assert not bucket.sessions


def test_complete_requires_actual_exact_size_and_cleans_rejected_object(storage):
    store, bucket, _ = storage
    async def run():
        upload = await store.initiate("data.csv", "text/csv", 100, "https://sasi.test")
        with pytest.raises(FileRejected, match="not complete"):
            await store.complete(upload["id"])
        bucket.finish(upload["id"], size=99)
        with pytest.raises(FileRejected, match="does not match"):
            await store.complete(upload["id"])
        assert "objects/" + upload["id"] not in bucket.rows
        assert upload["id"] not in json.loads(bucket.rows[INDEX]["data"])
    asyncio.run(run())


def test_large_upload_reads_only_prefix_and_is_durable_and_idempotent(storage, monkeypatch):
    store, bucket, client = storage
    monkeypatch.setattr(objects, "PREVIEW_BYTES", 32)
    async def run():
        upload = await store.initiate("data.csv", "text/csv", MAX_FILE_BYTES, "https://sasi.test")
        bucket.finish(upload["id"], b"a,b\n" + b"1,2\n" * 10)
        f = await store.complete(upload["id"])
        assert f.size == MAX_FILE_BYTES and f.data == b""
        assert f.preview_bytes == 32 and not f.preview_complete
        assert "remaining contents have not been analyzed" in f.coverage_note
        assert all(call[1].get("end") == 31 for call in bucket.reads if call[0].startswith("objects/"))
        restarted = ObjectFileStore("private-uploads", client=client)
        repeated = await restarted.complete(upload["id"])
        assert repeated.ref() == f.ref()
        index = json.loads(bucket.rows[INDEX]["data"])
        assert "session_url" not in index[f.id]
        assert "data" not in json.loads(bucket.rows[f"previews/{f.id}.json"]["data"])
        assert len(bucket.rows[INDEX]["data"]) < 1500
        selected = await restarted.take_async([f.id, f.id])
        await restarted.attach("run-1", selected)
        assert json.loads(bucket.rows[INDEX]["data"]) == {}
        again = ObjectFileStore("private-uploads", client=client)
        assert (await again.get("run-1"))[0].ref() == f.ref()
        with pytest.raises(FileRejected):
            await again.take_async([f.id])
        await again.delete("run-1")
        assert not any(key.startswith(("objects/", "previews/", "runs/")) for key in bucket.rows)
    asyncio.run(run())


def test_large_media_is_never_an_empty_inline_attachment(storage, monkeypatch):
    store, bucket, _ = storage
    monkeypatch.setattr(objects, "MEDIA_BYTES", 16)
    async def run():
        upload = await store.initiate("movie.mp4", "video/mp4", 100, "https://sasi.test")
        bucket.finish(upload["id"], b"x" * 100)
        f = await store.complete(upload["id"])
        assert f.kind == "video" and f.data == b"" and not f.preview_complete
        from app.gateway.base import ModelCall, split_attachments
        media, notes = split_attachments(ModelCall(system="", prompt="", kind="task", attachments=[f]), "gemini")
        assert not media and "movie.mp4" in notes
    asyncio.run(run())


def test_small_media_keeps_full_bytes_but_run_media_memory_is_bounded(storage, monkeypatch):
    store, bucket, _ = storage
    monkeypatch.setattr(objects, "MEDIA_BYTES", 16)
    monkeypatch.setattr(objects, "RUN_MEDIA_BYTES", 20)
    async def run():
        ids = []
        for _ in range(3):
            upload = await store.initiate("image.png", "image/png", 10, "https://sasi.test")
            bucket.finish(upload["id"], b"x" * 10)
            f = await store.complete(upload["id"])
            assert f.preview_complete and f.data == b"x" * 10
            ids.append(f.id)
        selected = await store.take_async(ids)
        assert sum(len(f.data) for f in selected) == 20
        assert not selected[-1].data and "memory allowance" in selected[-1].coverage_note
        await store.attach("run-1", selected)
        store.forget("run-1")
        again = await store.get("run-1")
        assert sum(len(f.data) for f in again) == 20
    asyncio.run(run())


def test_cleanup_does_not_cancel_a_current_manifest_write(storage):
    store, bucket, _ = storage
    async def run():
        upload = await store.initiate("data.csv", "text/csv", 8, "https://sasi.test")
        bucket.finish(upload["id"])
        await store.complete(upload["id"])
        def claim(index):
            index[upload["id"]].update(state="claimed", run_id="run-1", claimed_at=time.time(),
                                       created_at=time.time() - objects.STAGE_TTL - 1)
        store._edit_index(claim)
        await store.cleanup()
        assert "objects/" + upload["id"] in bucket.rows
        assert json.loads(bucket.rows[INDEX]["data"])[upload["id"]]["state"] == "claimed"
    asyncio.run(run())


def test_stale_cleanup_cannot_remove_a_refreshed_staging_reservation(storage):
    store, bucket, _ = storage
    async def run():
        upload = await store.initiate("data.csv", "text/csv", 8, "https://sasi.test")
        bucket.finish(upload["id"])
        await store.complete(upload["id"])
        await store.unstage_async(upload["id"], expired_before=time.time() - objects.STAGE_TTL)
        assert "objects/" + upload["id"] in bucket.rows
        assert upload["id"] in json.loads(bucket.rows[INDEX]["data"])
    asyncio.run(run())


def test_attach_failure_can_restore_upload_without_losing_original(storage):
    store, bucket, _ = storage
    async def run():
        upload = await store.initiate("data.csv", "text/csv", 8, "https://sasi.test")
        bucket.finish(upload["id"])
        f = await store.complete(upload["id"])
        await store.attach("run-1", [f])
        await store.rollback_attach("run-1", [f])
        assert "runs/run-1.json" not in bucket.rows
        assert "objects/" + f.id in bucket.rows
        assert (await store.take_async([f.id]))[0].size == 8
        await store.attach("run-2", [f])
        assert "runs/run-2.json" in bucket.rows
    asyncio.run(run())


def test_failed_second_run_cannot_restage_an_original_owned_by_first(storage):
    store, bucket, client = storage
    second = ObjectFileStore("private-uploads", client=client)
    async def run():
        upload = await store.initiate("data.csv", "text/csv", 8, "https://sasi.test")
        bucket.finish(upload["id"])
        f = await store.complete(upload["id"])
        # Both admission requests read staging before either claims it.
        files1, files2 = await store.take_async([f.id]), await second.take_async([f.id])
        await store.attach("winner", files1)
        with pytest.raises(FileRejected):
            await second.attach("loser", files2)
        await second.rollback_attach("loser", files2)
        assert f.id not in json.loads(bucket.rows[INDEX]["data"])
        assert "objects/" + f.id in bucket.rows
        assert (await store.get("winner"))[0].id == f.id
    asyncio.run(run())


def test_late_duplicate_completion_cannot_delete_a_run_owned_original(storage, monkeypatch):
    store, bucket, client = storage
    second = ObjectFileStore("private-uploads", client=client)
    async def run():
        upload = await store.initiate("data.csv", "text/csv", 8, "https://sasi.test")
        bucket.finish(upload["id"])
        paused, release = asyncio.Event(), asyncio.Event()
        save = second._save_preview
        async def pause_after_read(f):
            paused.set()
            await release.wait()
            await save(f)
        monkeypatch.setattr(second, "_save_preview", pause_after_read)
        duplicate = asyncio.create_task(second.complete(upload["id"]))
        await paused.wait()
        f = await store.complete(upload["id"])
        await store.attach("winner", [f])
        release.set()
        with pytest.raises(FileRejected):
            await duplicate
        assert "objects/" + f.id in bucket.rows
        store.forget("winner")
        assert (await store.get("winner"))[0].id == f.id
    asyncio.run(run())


def test_attach_cache_is_bounded_and_generation_changes_fail_closed(storage):
    store, bucket, _ = storage
    async def run():
        for number in range(3):
            upload = await store.initiate("data.csv", "text/csv", 8, "https://sasi.test")
            bucket.finish(upload["id"])
            f = await store.complete(upload["id"])
            await store.attach(f"run-{number}", [f])
        assert list(store._runs) == ["run-1", "run-2"]
        bucket.rows["objects/" + f.id]["generation"] += 1
        with pytest.raises(PreconditionFailed):
            await store.read_range(f, 0, 3)
    asyncio.run(run())


def test_file_count_reservation_rejects_twenty_first_upload(storage):
    store, bucket, _ = storage
    async def run():
        for _ in range(20):
            await store.initiate("tiny.txt", "text/plain", 1, "https://sasi.test")
        with pytest.raises(FileRejected, match="at most 20"):
            await store.initiate("extra.txt", "text/plain", 1, "https://sasi.test")
        assert len(bucket.sessions) == 20
    asyncio.run(run())


def test_staged_list_recovers_ready_files_without_session_credentials_or_original_bytes(storage):
    store, bucket, _ = storage
    async def run():
        pending = await store.initiate("pending.csv", "text/csv", 8, "https://sasi.test")
        done = await store.initiate("done.csv", "text/csv", 8, "https://sasi.test")
        bucket.finish(done["id"])
        f = await store.complete(done["id"])
        bucket.reads.clear()
        listed = await store.list_uploads()
        assert listed[0]["id"] == pending["id"] and listed[0]["state"] == "pending"
        assert listed[1]["file"] == f.ref() and listed[1]["state"] == "ready"
        assert "session_url" not in json.dumps(listed) and "upload_url" not in json.dumps(listed)
        assert not any(key.startswith("objects/") for key, _ in bucket.reads)
    asyncio.run(run())


def test_cleanup_removes_expired_pending_but_preserves_attached_originals(storage):
    store, bucket, _ = storage
    async def run():
        upload = await store.initiate("data.csv", "text/csv", 8, "https://sasi.test")
        bucket.finish(upload["id"])
        f = await store.complete(upload["id"])
        await store.attach("run-1", [f])
        pending = await store.initiate("pending.csv", "text/csv", 8, "https://sasi.test")
        bucket.finish(pending["id"])
        def expire(index):
            index[pending["id"]]["created_at"] = time.time() - objects.STAGE_TTL - 1
        store._edit_index(expire)
        await store.cleanup()
        assert "objects/" + pending["id"] not in bucket.rows
        assert "objects/" + f.id in bucket.rows
        assert "runs/run-1.json" in bucket.rows
    asyncio.run(run())


def test_cloud_store_reads_existing_legacy_files_and_rejects_arbitrary_paths(storage):
    store, _, _ = storage
    legacy = StoredFile("old", "old.txt", "text/plain", "text", 3, b"old")
    async def run():
        await store.legacy.attach("legacy", [legacy])
        assert await store.get("legacy") == [legacy]
        with pytest.raises(FileRejected, match="identifier"):
            await store.get("../other")
        evil = StoredFile("file", "x", "text/plain", "text", 3, b"", object_uri="gs://elsewhere/private", object_generation="1")
        with pytest.raises(FileRejected, match="reference"):
            await store.read_range(evil, 0, 2)
    asyncio.run(run())


@pytest.mark.parametrize("header,expected", [(None, (0, 9, False)), ("bytes=3-5", (3, 5, True)),
                                            ("bytes=7-", (7, 9, True)), ("bytes=-3", (7, 9, True)),
                                            ("bytes=1-999", (1, 9, True))])
def test_download_single_ranges(header, expected):
    assert download_range(header, 10) == expected


@pytest.mark.parametrize("header", ["bytes=10-", "bytes=5-4", "bytes=-0", "bytes=", "bytes=1-2,5-6"])
def test_invalid_download_ranges(header):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        download_range(header, 10)
    assert error.value.status_code == 416


def test_new_routes_are_private_and_local_limits_remain_honest():
    from app.main import create_app
    with TestClient(create_app(settings()), base_url="https://testserver") as client:
        for path in ("/api/files/uploads", "/api/files/uploads/x/complete"):
            assert client.post(path, json={"name": "x", "size": 1}).status_code == 401
        assert client.get("/api/runs/x/files/y/preview").status_code == 401
        assert client.get("/api/files/uploads").status_code == 401
        client.post("/api/login", json={"code": OWNER_TOKEN})
        limits = client.get("/api/limits").json()
        assert limits["max_file_bytes"] == 25 * 1024**2 and not limits["resumable_uploads"]
        assert client.post("/api/files/uploads", json={"name": "x", "size": 1}).status_code == 503


def test_authenticated_cloud_stream_and_bounded_text_preview(storage, monkeypatch):
    from app.main import create_app
    from app.models import FileRef, Run
    from app.store.memory import MemoryStore
    store, bucket, _ = storage
    memory = MemoryStore()
    monkeypatch.setattr("app.main.build_store", lambda _settings: (memory, store))
    with TestClient(create_app(settings()), base_url="https://testserver") as client:
        client.post("/api/login", json={"code": OWNER_TOKEN})
        limits = client.get("/api/limits").json()
        assert limits["resumable_uploads"] and limits["max_run_bytes"] == MAX_RUN_BYTES
        assert limits["max_file_bytes"] == MAX_FILE_BYTES and limits["max_files_per_run"] == 20
        assert client.post("/api/files/uploads", json={"name": "x", "size": 1},
                           headers={"Origin": "https://other.test"}).status_code == 403
        response = client.post("/api/files/uploads", json={"name": "data.csv", "mime": "text/csv", "size": 8},
                               headers={"Origin": "https://testserver"})
        assert response.status_code == 200
        fid = response.json()["id"]
        bucket.finish(fid)
        complete = client.post(f"/api/files/uploads/{fid}/complete")
        assert complete.status_code == 200
        assert complete.json()["storage"] == "cloud"
        files = client.portal.call(store.take_async, [fid])
        run = Run(id="run-1", request="Inspect this data", status="completed", files=[FileRef(**files[0].ref())])
        client.portal.call(store.attach, run.id, files)
        client.portal.call(memory.save, run)
        result = client.get(f"/api/runs/{run.id}/files/{fid}", headers={"Range": "bytes=4-6"})
        assert result.status_code == 206 and result.content == b"1,2"
        assert result.headers["content-range"] == "bytes 4-6/8"
        assert result.headers["cache-control"] == "no-store"
        preview = client.get(f"/api/runs/{run.id}/files/{fid}/preview?offset=4&max_bytes=4").json()
        assert preview["text"] == "1,2\n" and preview["next_offset"] is None
        assert client.get(f"/api/runs/{run.id}/files/{fid}/preview?max_bytes=262145").status_code == 400
        assert all(kwargs.get("raw_download") for key, kwargs in bucket.reads if key.startswith("objects/"))
