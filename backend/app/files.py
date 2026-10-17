"""Files people attach to a run. Any type is accepted.

Images, PDFs, video and audio go to the models that can open them directly.
Data and documents (CSV, JSON, Parquet, Excel, Word, PowerPoint, ZIP, code, text)
are converted to text in extract.py. Other binary files are kept and described.

Cloud uploads stream directly to private object storage; only bounded previews
reach models. The memory/legacy stores retain their small-file fallback. A run
owns its saved originals and deletes them with the rest of its records.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import time
import uuid
from dataclasses import dataclass, field

from . import extract

log = logging.getLogger("sasi.files")

MAX_FILE_BYTES = 10 * 1024 * 1024 * 1024
MAX_RUN_BYTES = MAX_FILE_BYTES
MAX_FILES_PER_RUN = 20
RAW_UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # never send large uploads through Cloud Run
LOCAL_RUN_BYTES = 60 * 1024 * 1024
PREVIEW_BYTES = 2 * 1024 * 1024
TEXT_LIMIT = 150_000  # characters of a data file that go into a prompt
STAGE_TTL = 3 * 3600

IMAGE = {"image/png", "image/jpeg", "image/webp", "image/gif"}
VIDEO = {"video/mp4", "video/quicktime", "video/webm", "video/mpeg", "video/x-msvideo", "video/3gpp"}
AUDIO = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg", "audio/aac", "audio/flac", "audio/mp4", "audio/x-m4a"}
PDF = {"application/pdf"}
EXTRA_TYPES = {".csv": "text/csv", ".tsv": "text/tab-separated-values", ".md": "text/markdown", ".jsonl": "application/jsonl",
               ".m4a": "audio/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".yaml": "text/yaml", ".yml": "text/yaml"}

# Which kinds each model can read directly is in gateway/catalog.py. Others get the analyses of the models that can.

# Per-file size limits for sending a file inline to each provider's API.
DIRECT_LIMIT = {
    "claude": {"image": 5 * 1024 * 1024, "pdf": 30 * 1024 * 1024},
    "deepseek": {"image": 5 * 1024 * 1024},
}


class FileRejected(ValueError):
    pass


@dataclass
class StoredFile:
    id: str
    name: str
    mime: str
    kind: str  # image | video | audio | pdf | text | other
    size: int
    data: bytes
    text: str = ""  # what goes into a prompt for text files (with a data profile for CSV)
    created_at: float = field(default_factory=time.time)
    object_uri: str = ""
    object_generation: str = ""
    preview_complete: bool = True
    preview_bytes: int = 0
    coverage_note: str = ""

    def ref(self) -> dict:
        return {"id": self.id, "name": self.name, "mime": self.mime, "kind": self.kind, "size": self.size,
                "preview_complete": self.preview_complete, "preview_bytes": self.preview_bytes,
                "coverage_note": self.coverage_note, "storage": "cloud" if self.object_uri else "local"}

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode()


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def classify(name: str, mime: str) -> tuple[str, str]:
    """Return (kind, mime). Media the models read natively keeps its own kind; everything else is converted later."""
    ext = _ext(name)
    mime = (mime or "").split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        mime = EXTRA_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if mime in IMAGE:
        return "image", mime
    if mime in VIDEO or ext in (".mp4", ".mov", ".webm", ".mpeg", ".avi", ".3gp"):
        return "video", mime if mime in VIDEO else "video/mp4"
    if mime in AUDIO or ext in (".mp3", ".wav", ".ogg", ".aac", ".flac", ".m4a"):
        return "audio", "audio/mp4" if mime in ("audio/x-m4a",) else (mime if mime in AUDIO else "audio/mpeg")
    if mime in PDF or ext == ".pdf":
        return "pdf", "application/pdf"
    return "text", mime


def safe_name(name: str) -> str:
    return "".join(c for c in (name or "file").replace("/", "_").replace("\\", "_") if ord(c) >= 32).strip()[:180] or "file"


def prepare(name: str, mime: str, data: bytes) -> StoredFile:
    """Accept any file. Data and documents become text the models read; unknown binary files are kept and described."""
    name = safe_name(name)
    if not data:
        raise FileRejected(f"'{name}' is empty.")
    if len(data) > RAW_UPLOAD_MAX_BYTES:
        raise FileRejected(f"'{name}' requires a resumable cloud upload (raw uploads allow 25 MiB).")
    kind, mime = classify(name, mime)
    f = StoredFile(id=uuid.uuid4().hex[:16], name=name, mime=mime, kind=kind, size=len(data), data=data,
                   preview_bytes=len(data))
    if kind != "text":
        return f
    try:
        text, readable = extract.to_text(name, data)
    except Exception as e:  # noqa: BLE001 - a damaged file is still kept, and the models are told
        log.info("could not convert %s: %s", name, e)
        f.kind = "other"
        f.text = f"[{name} could not be read: {type(e).__name__}: {str(e)[:200]}]"
        return f
    if not readable:
        f.kind = "other"
        return f
    f.text = extract.clip(text)
    return f


def describe(files: list[StoredFile]) -> str:
    if not files:
        return ""
    label = {"text": "data/text, converted for reading", "other": "binary, no model can open it"}
    lines = [f"- {f.name} ({label.get(f.kind, f.kind)}, {f.size / 1024:.0f} KB)" for f in files]
    return "Attached files:\n" + "\n".join(lines)


class FileStore:
    """In-memory file store. Used locally and as the cache in front of BigQuery."""

    def __init__(self):
        self._staged: dict[str, StoredFile] = {}
        self._runs: dict[str, list[StoredFile]] = {}

    resumable_uploads = False
    max_file_bytes = RAW_UPLOAD_MAX_BYTES
    max_run_bytes = LOCAL_RUN_BYTES

    async def stage_async(self, f: StoredFile) -> StoredFile:
        self.stage(f)
        return f

    async def unstage_async(self, file_id: str) -> None:
        self.unstage(file_id)

    async def take_async(self, ids: list[str]) -> list[StoredFile]:
        return self.take(ids)

    async def cleanup(self) -> None:
        return None

    def stage(self, f: StoredFile) -> None:
        cutoff = time.time() - STAGE_TTL
        for fid in [k for k, v in self._staged.items() if v.created_at < cutoff]:
            self._staged.pop(fid, None)
        if sum(x.size for x in self._staged.values()) + f.size > 4 * self.max_run_bytes:
            raise FileRejected("Too many files are waiting to be used. Start a run or remove some files first.")
        self._staged[f.id] = f

    def unstage(self, file_id: str) -> None:
        self._staged.pop(file_id, None)

    def take(self, ids: list[str]) -> list[StoredFile]:
        files = [self._staged[i] for i in dict.fromkeys(ids) if i in self._staged]
        if len(files) != len(set(ids)):
            raise FileRejected("Some attached files expired. Attach them again.")
        if len(files) > MAX_FILES_PER_RUN:
            raise FileRejected(f"Attach at most {MAX_FILES_PER_RUN} files to one run.")
        if sum(f.size for f in files) > self.max_run_bytes:
            raise FileRejected(f"Files for one run must total at most {self.max_run_bytes // (1024 * 1024)} MiB.")
        for f in files:
            self._staged.pop(f.id, None)
        return files

    async def attach(self, run_id: str, files: list[StoredFile]) -> None:
        if files:
            self._runs[run_id] = files

    async def get(self, run_id: str) -> list[StoredFile]:
        return self._runs.get(run_id, [])

    async def get_one(self, run_id: str, file_id: str) -> StoredFile | None:
        return next((f for f in await self.get(run_id) if f.id == file_id), None)

    async def delete(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    def forget(self, run_id: str) -> None:
        """Drop the cached copy (BigQuery keeps the file)."""
        self._runs.pop(run_id, None)
