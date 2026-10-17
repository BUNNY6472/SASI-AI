"""Private, bounded Cloud Storage attachments with durable staging reservations.

The browser sends original bytes straight to a size-bound resumable GCS session.
Only small manifests, a 2 MiB text preview, or bounded complete media enter the
application process (8 MiB per media file, 64 MiB resident media per run).
The staging index uses generation preconditions, including across instances, so
parallel tabs cannot reserve more than the aggregate upload allowance.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict
from urllib.parse import urlsplit

from .. import extract
from ..files import (MAX_FILE_BYTES, MAX_FILES_PER_RUN, MAX_RUN_BYTES, PREVIEW_BYTES,
                     STAGE_TTL, FileRejected, FileStore, StoredFile, classify, safe_name)

log = logging.getLogger("sasi.objects")
INDEX = "metadata/staging.json"
CHUNK_SIZE = 8 * 1024 * 1024
MEDIA_BYTES = 8 * 1024 * 1024
RUN_MEDIA_BYTES = 64 * 1024 * 1024
CLAIM_TTL = 10 * 60
MANIFEST_LIMIT = 8 * 1024 * 1024
ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def checked_id(value: str) -> str:
    if not ID.fullmatch(value):
        raise FileRejected("Invalid attachment identifier.")
    return value


def prepare_preview(entry: dict, data: bytes) -> StoredFile:
    complete = len(data) == entry["size"]
    kind, mime = classify(entry["name"], entry["mime"])
    note = (f"Original preserved ({entry['size']:,} bytes). Model preview is limited to the first "
            f"{len(data):,} bytes; remaining contents have not been analyzed.") if not complete else ""
    f = StoredFile(id=entry["id"], name=entry["name"], mime=mime, kind=kind, size=entry["size"],
                   data=data if complete and kind != "text" else b"", created_at=entry["created_at"],
                   object_uri=entry["object_uri"], object_generation=entry["generation"],
                   preview_complete=complete, preview_bytes=len(data), coverage_note=note)
    if kind == "text":
        try:
            text, readable = extract.to_text(f.name, data, complete=complete, total_bytes=f.size)
            f.text = extract.clip(text)
            if not readable:
                f.kind = "other"
        except Exception as error:  # bounded preview errors never discard the original
            f.kind = "other"
            f.text = f"Preview unavailable: {type(error).__name__}. The original file is preserved for download."
    elif not complete:
        f.coverage_note += " This media file is not forwarded as an incomplete or empty attachment."
    if not complete and note not in f.text:
        f.text = note + ("\n" + f.text if f.text else "")
    return f


class ObjectFileStore(FileStore):
    resumable_uploads = True
    max_file_bytes = MAX_FILE_BYTES
    max_run_bytes = MAX_RUN_BYTES

    def __init__(self, bucket_name: str, project: str = "", legacy: FileStore | None = None, *, client=None):
        super().__init__()
        if client is None:
            from google.cloud import storage
            client = storage.Client(project=project or None)
        self.bucket = client.bucket(bucket_name)
        self.bucket_name = bucket_name
        self.legacy = legacy or FileStore()
        self._cleanup_lock = asyncio.Lock()
        self._preview_slots = asyncio.Semaphore(2)

    async def _prepare(self, entry: dict, data: bytes) -> StoredFile:
        async with self._preview_slots:
            task = asyncio.create_task(asyncio.to_thread(prepare_preview, entry, data))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            result = task.result()
            if cancelled:
                raise asyncio.CancelledError
            return result

    def _read_json(self, key: str) -> tuple[dict, int]:
        from google.api_core.exceptions import NotFound
        blob = self.bucket.blob(key)
        try:
            blob.reload(timeout=30)
        except NotFound:
            return {}, 0
        if blob.size > MANIFEST_LIMIT:
            raise RuntimeError("Attachment metadata exceeds its safe limit.")
        generation = int(blob.generation)
        raw = blob.download_as_bytes(if_generation_match=generation, raw_download=True, timeout=30)
        return json.loads(raw), generation

    def _write_json(self, key: str, value: dict, generation: int | None = None):
        kwargs = {"if_generation_match": generation} if generation is not None else {}
        self.bucket.blob(key).upload_from_string(json.dumps(value, ensure_ascii=False),
                                                 content_type="application/json", timeout=30, **kwargs)

    def _edit_index(self, change):
        from google.api_core.exceptions import PreconditionFailed
        for _ in range(12):
            try:
                index, generation = self._read_json(INDEX)
                result = change(index)
                self._write_json(INDEX, index, generation)
                return result
            except PreconditionFailed:
                continue
        raise FileRejected("Another upload is updating the workspace. Try again.")

    def _entry(self, file_id: str) -> dict:
        checked_id(file_id)
        row = self._read_json(INDEX)[0].get(file_id)
        if not row or row.get("state") not in ("pending", "ready"):
            raise FileRejected("This upload expired or was already attached. Attach it again.")
        if row["created_at"] + STAGE_TTL < time.time():
            raise FileRejected("This upload expired. Attach it again.")
        return row

    def _reserve(self, entry: dict):
        def change(index):
            existing = index.get(entry["id"])
            if existing:
                if existing["size"] != entry["size"]:
                    raise FileRejected("Attachment size changed.")
                return
            if len(index) >= MAX_FILES_PER_RUN:
                raise FileRejected(f"Attach at most {MAX_FILES_PER_RUN} files. Remove unused uploads first.")
            if sum(row["size"] for row in index.values()) + entry["size"] > MAX_RUN_BYTES:
                raise FileRejected("Pending and ready uploads together may total at most 2 GiB. Remove unused files first.")
            index[entry["id"]] = entry
        self._edit_index(change)

    async def initiate(self, name: str, mime: str, size: int, origin: str) -> dict:
        if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_FILE_BYTES:
            raise FileRejected("Choose a non-empty file of at most 2 GiB.")
        await self.cleanup()
        fid = uuid.uuid4().hex
        _, mime = classify(name, mime)
        entry = {"id": fid, "name": safe_name(name), "mime": mime, "size": size, "state": "pending",
                 "created_at": time.time(), "object_uri": f"gs://{self.bucket_name}/objects/{fid}"}
        await asyncio.to_thread(self._reserve, entry)
        try:
            blob = self.bucket.blob(f"objects/{fid}")
            blob.metadata = {"sasi_file_id": fid, "declared_size": str(size)}
            url = await asyncio.to_thread(blob.create_resumable_upload_session, content_type=mime,
                                          size=size, origin=origin, if_generation_match=0, timeout=30)
            def remember(index):
                row = index.get(fid)
                if not row or row["state"] != "pending":
                    raise FileRejected("This upload was cancelled.")
                row["session_url"] = url
            await asyncio.to_thread(self._edit_index, remember)
        except BaseException:
            # Retain failed reservations for cleanup if cloud cancellation fails.
            await self.unstage_async(fid)
            raise
        return {"id": fid, "upload_url": url, "chunk_size": CHUNK_SIZE,
                "expires_at": entry["created_at"] + STAGE_TTL, "headers": {"Content-Type": mime}}

    async def list_uploads(self) -> list[dict]:
        """Recover staged attachments after refresh without disclosing sessions."""
        await self.cleanup()
        index, _ = await asyncio.to_thread(self._read_json, INDEX)
        result = []
        for entry in index.values():
            if entry["state"] == "claimed":
                continue
            row = {"id": entry["id"], "name": entry["name"], "size": entry["size"],
                   "state": "ready" if entry["state"] == "ready" else "pending",
                   "expires_at": entry["created_at"] + STAGE_TTL}
            if row["state"] == "ready":
                metadata, _ = await asyncio.to_thread(self._read_json, f"previews/{checked_id(entry['id'])}.json")
                if metadata:
                    row["file"] = StoredFile(**metadata, data=b"").ref()
                else:
                    row["state"] = "pending"
            result.append(row)
        return result

    async def complete(self, file_id: str) -> StoredFile:
        from google.api_core.exceptions import NotFound
        entry = await asyncio.to_thread(self._entry, file_id)
        if entry["state"] == "ready":
            return await self._hydrate(entry)
        blob = self.bucket.blob(f"objects/{checked_id(file_id)}")
        try:
            await asyncio.to_thread(blob.reload, timeout=30)
        except NotFound as error:
            raise FileRejected("The upload is not complete yet. Resume it before finishing.") from error
        if (int(blob.size) != entry["size"] or blob.content_type != entry["mime"]
                or (blob.metadata or {}).get("sasi_file_id") != file_id):
            await self.unstage_async(file_id)
            raise FileRejected("Uploaded size or metadata does not match the reserved file. Upload it again.")
        entry["generation"] = str(blob.generation)
        kind, _ = classify(entry["name"], entry["mime"])
        read_size = (min(PREVIEW_BYTES, entry["size"]) if kind == "text"
                     else entry["size"] if entry["size"] <= MEDIA_BYTES else 0)
        data = (await asyncio.to_thread(blob.download_as_bytes, start=0, end=read_size - 1,
                                       raw_download=True, if_generation_match=int(blob.generation), timeout=30)
                if read_size else b"")
        f = await self._prepare(entry, data)
        await self._save_preview(f)
        def finish(index):
            current = index.get(file_id)
            if not current or current["state"] not in ("pending", "ready"):
                raise FileRejected("This upload was cancelled.")
            # Completed uploads no longer need their bearer session credential.
            entry.pop("session_url", None)
            index[file_id] = {**entry, "state": "ready"}
        # Another completion may already have attached this original to a run.
        # A lost staging reservation is never authority to delete its object.
        await asyncio.to_thread(self._edit_index, finish)
        return f

    async def _save_preview(self, f: StoredFile):
        metadata = asdict(f)
        metadata.pop("data")
        await asyncio.to_thread(self._write_json, f"previews/{checked_id(f.id)}.json", metadata)

    async def _hydrate(self, entry: dict, media_budget: int = MEDIA_BYTES) -> StoredFile:
        metadata, _ = await asyncio.to_thread(self._read_json, f"previews/{checked_id(entry['id'])}.json")
        if not metadata:
            raise FileRejected("The file preview is unavailable. Finish or upload it again.")
        f = StoredFile(**metadata, data=b"")
        self._object_key(f)  # validate provenance before any object read
        if f.kind not in ("text", "other") and f.preview_complete and f.size <= MEDIA_BYTES:
            if f.size <= media_budget:
                f.data = await self.read_range(f, 0, f.size - 1)
            else:
                f.coverage_note = "Original preserved. Inline media was omitted to stay within this run's 64 MiB media memory allowance."
        return f

    async def _hydrate_run(self, entries: list[dict]) -> list[StoredFile]:
        files, remaining = [], RUN_MEDIA_BYTES
        for row in entries:
            f = await self._hydrate(row, remaining)
            remaining -= len(f.data)
            files.append(f)
        return files

    def _cache(self, run_id: str, files: list[StoredFile]):
        self._runs.pop(run_id, None)
        self._runs[run_id] = files
        while len(self._runs) > 2:
            self._runs.pop(next(iter(self._runs)))

    async def stage_async(self, f: StoredFile) -> StoredFile:
        await self.cleanup()
        entry = {"id": checked_id(f.id), "name": f.name, "mime": f.mime, "size": f.size, "state": "pending",
                 "created_at": time.time(), "object_uri": f"gs://{self.bucket_name}/objects/{f.id}"}
        await asyncio.to_thread(self._reserve, entry)
        if f.object_uri:
            self._object_key(f)
            entry["generation"] = f.object_generation
            prepared = f
            await self._save_preview(prepared)
        else:
            blob = self.bucket.blob(f"objects/{f.id}")
            blob.metadata = {"sasi_file_id": f.id, "declared_size": str(f.size)}
            await asyncio.to_thread(blob.upload_from_string, f.data, content_type=f.mime, if_generation_match=0, timeout=60)
            entry["generation"] = str(blob.generation)
            kind, _ = classify(f.name, f.mime)
            data = f.data[:PREVIEW_BYTES] if kind == "text" else f.data if f.size <= MEDIA_BYTES else b""
            prepared = await self._prepare(entry, data)
            await self._save_preview(prepared)
        def finish(index):
            row = index.get(f.id)
            if not row or row["state"] not in ("pending", "ready"):
                raise FileRejected("This upload was cancelled.")
            index[f.id] = {**entry, "state": "ready"}
        await asyncio.to_thread(self._edit_index, finish)
        return prepared

    async def take_async(self, ids: list[str]) -> list[StoredFile]:
        ids = list(dict.fromkeys(ids))
        if len(ids) > MAX_FILES_PER_RUN:
            raise FileRejected(f"Attach at most {MAX_FILES_PER_RUN} files.")
        entries = [await asyncio.to_thread(self._entry, fid) for fid in ids]
        if any(row["state"] != "ready" for row in entries):
            raise FileRejected("Wait for every upload to finish before starting a run.")
        if sum(row["size"] for row in entries) > MAX_RUN_BYTES:
            raise FileRejected("Files for one run may total at most 2 GiB.")
        return await self._hydrate_run(entries)

    async def attach(self, run_id: str, files: list[StoredFile]) -> None:
        if not files:
            return
        checked_id(run_id)
        def claim(index):
            for f in files:
                row = index.get(f.id)
                if not row or row["state"] != "ready":
                    raise FileRejected("An attachment is already in use or expired. Upload it again.")
            for f in files:
                index[f.id].update(state="claimed", run_id=run_id, claimed_at=time.time())
        await asyncio.to_thread(self._edit_index, claim)
        try:
            await asyncio.to_thread(self._write_json, f"runs/{run_id}.json", {"files": [{"id": f.id} for f in files]}, 0)
        except BaseException:
            # A manifest may have reached GCS even if its response was lost. Keep
            # claimed data for recovery rather than offering it to a second run.
            raise
        def release(index):
            for f in files:
                if index.get(f.id, {}).get("run_id") == run_id:
                    index.pop(f.id, None)
        await asyncio.to_thread(self._edit_index, release)
        self._cache(run_id, files)

    async def get(self, run_id: str) -> list[StoredFile]:
        checked_id(run_id)
        if run_id in self._runs:
            return self._runs[run_id]
        metadata, _ = await asyncio.to_thread(self._read_json, f"runs/{run_id}.json")
        if not metadata:
            return await self.legacy.get(run_id)
        files = await self._hydrate_run(metadata["files"])
        self._cache(run_id, files)
        return files

    async def get_one(self, run_id: str, file_id: str) -> StoredFile | None:
        checked_id(run_id)
        checked_id(file_id)
        if run_id in self._runs:
            return next((f for f in self._runs[run_id] if f.id == file_id), None)
        metadata, _ = await asyncio.to_thread(self._read_json, f"runs/{run_id}.json")
        if not metadata:
            return await self.legacy.get_one(run_id, file_id)
        row = next((row for row in metadata["files"] if row["id"] == file_id), None)
        return await self._hydrate(row, 0) if row else None

    def _object_key(self, f: StoredFile) -> str:
        expected = f"gs://{self.bucket_name}/objects/{checked_id(f.id)}"
        if f.object_uri != expected or not f.object_generation.isdigit():
            raise FileRejected("Attachment storage reference is invalid.")
        return f"objects/{f.id}"

    async def read_range(self, f: StoredFile, start: int, end: int) -> bytes:
        if not 0 <= start <= end < f.size or end - start + 1 > CHUNK_SIZE:
            raise FileRejected("Requested file range exceeds its safe limit.")
        blob = self.bucket.blob(self._object_key(f), generation=int(f.object_generation))
        return await asyncio.to_thread(blob.download_as_bytes, start=start, end=end, raw_download=True,
                                       if_generation_match=int(f.object_generation), timeout=60)

    async def stream(self, f: StoredFile, start: int, end: int):
        while start <= end:
            stop = min(end, start + CHUNK_SIZE - 1)
            yield await self.read_range(f, start, stop)
            start = stop + 1

    def _delete_blob(self, key: str):
        from google.api_core.exceptions import NotFound
        try:
            self.bucket.blob(key).delete(timeout=30)
        except NotFound:
            pass

    def _delete_objects(self, file_id: str):
        checked_id(file_id)
        self._delete_blob(f"objects/{file_id}")
        self._delete_blob(f"previews/{file_id}.json")

    async def unstage_async(self, file_id: str, *, expired_before: float | None = None) -> None:
        checked_id(file_id)
        def cancel(index):
            entry = index.get(file_id)
            if (entry and expired_before is not None and entry["created_at"] >= expired_before
                    and entry["state"] != "cancelled"):
                return None
            if entry and entry["state"] == "claimed":
                raise FileRejected("This file already belongs to a run. Delete the run to remove it.")
            if entry:
                entry["state"] = "cancelled"
            return entry
        entry = await asyncio.to_thread(self._edit_index, cancel)
        if not entry:
            return
        url = entry.get("session_url", "")
        if url:
            import requests
            if urlsplit(url).scheme != "https" or urlsplit(url).hostname != "storage.googleapis.com":
                raise RuntimeError("Invalid upload session origin.")
            # requests does not emit the credential-bearing session URL in INFO logs.
            response = await asyncio.to_thread(requests.delete, url, timeout=20, allow_redirects=False)
            # GCS uses 499 for a successfully cancelled resumable session.
            if response.status_code not in (200, 204, 404, 410, 499):
                raise RuntimeError("The cloud upload could not be cancelled. Cleanup will retry.")
        await asyncio.to_thread(self._delete_objects, file_id)
        await asyncio.to_thread(self._edit_index, lambda index: index.pop(file_id, None))

    async def cleanup(self) -> None:
        async with self._cleanup_lock:
            index, _ = await asyncio.to_thread(self._read_json, INDEX)
            for fid, entry in index.items():
                if entry["created_at"] + STAGE_TTL >= time.time() and entry["state"] != "cancelled":
                    continue
                if entry["state"] == "claimed":
                    if entry.get("claimed_at", entry["created_at"]) + CLAIM_TTL >= time.time():
                        continue
                    # Attaching a run must never expire its accepted originals.
                    manifest, _ = await asyncio.to_thread(self._read_json, f"runs/{checked_id(entry['run_id'])}.json")
                    if manifest:
                        def release_claim(current, fid=fid, previous=entry):
                            row = current.get(fid, {})
                            if (row.get("state") == "claimed" and row.get("run_id") == previous["run_id"]
                                    and row.get("claimed_at") == previous.get("claimed_at")):
                                current.pop(fid, None)
                        await asyncio.to_thread(self._edit_index, release_claim)
                        continue
                    def unclaim(current, fid=fid):
                        row = current.get(fid, {})
                        if row.get("state") == "claimed" and row.get("claimed_at", row["created_at"]) + CLAIM_TTL < time.time():
                            current[fid]["state"] = "cancelled"
                    await asyncio.to_thread(self._edit_index, unclaim)
                try:
                    await self.unstage_async(fid, expired_before=time.time() - STAGE_TTL)
                except Exception as error:
                    log.warning("Attachment cleanup will retry (%s).", type(error).__name__)

    async def rollback_attach(self, run_id: str, files: list[StoredFile]):
        """Release an admission failure without deleting its uploaded originals."""
        manifest_key = f"runs/{checked_id(run_id)}.json"
        manifest, _ = await asyncio.to_thread(self._read_json, manifest_key)
        owned_ids = {row["id"] for row in manifest.get("files", [])}
        def restore(index):
            restoring = []
            for f in files:
                row = index.get(f.id)
                own_claim = row and row.get("state") == "claimed" and row.get("run_id") == run_id
                if own_claim or (f.id in owned_ids and (row is None or row.get("state") == "ready")):
                    restoring.append(f)
            if not restoring:
                return bool(owned_ids)
            additional = [f for f in restoring if f.id not in index]
            if (len(index) + len(additional) > MAX_FILES_PER_RUN
                    or sum(row["size"] for row in index.values()) + sum(f.size for f in additional) > MAX_RUN_BYTES):
                # Keep the run's manifest and originals together if another tab
                # has already used the freed staging quota.
                raise FileRejected("Could not return these files to staging because other uploads use the allowance.")
            for f in restoring:
                index[f.id] = {"id": f.id, "name": f.name, "mime": f.mime, "size": f.size, "state": "ready",
                               "created_at": time.time(), "object_uri": f.object_uri, "generation": f.object_generation}
            return True
        restored = await asyncio.to_thread(self._edit_index, restore)
        if restored:
            await asyncio.to_thread(self._delete_blob, manifest_key)
            self._runs.pop(run_id, None)

    async def delete(self, run_id: str) -> None:
        checked_id(run_id)
        metadata, _ = await asyncio.to_thread(self._read_json, f"runs/{run_id}.json")
        for row in metadata.get("files", []):
            await asyncio.to_thread(self._delete_objects, row["id"])
        await asyncio.to_thread(self._delete_blob, f"runs/{run_id}.json")
        self._runs.pop(run_id, None)
        await self.legacy.delete(run_id)
