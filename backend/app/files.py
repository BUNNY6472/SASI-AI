"""Files people attach to a run: images, video, audio, PDFs and data (CSV, text, JSON).

Uploads are staged in memory until the run starts, then saved with the run
(in BigQuery on Cloud Run) so every model reads the same files and a deleted
run takes its files with it.
"""
from __future__ import annotations

import base64
import csv
import io
import logging
import mimetypes
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("sasi.files")

MAX_FILE_BYTES = 25 * 1024 * 1024  # Cloud Run caps a request at 32 MiB
MAX_RUN_BYTES = 60 * 1024 * 1024
MAX_FILES_PER_RUN = 10
TEXT_LIMIT = 60_000  # characters of a data file that go into a prompt
STAGE_TTL = 3 * 3600

IMAGE = {"image/png", "image/jpeg", "image/webp", "image/gif"}
VIDEO = {"video/mp4", "video/quicktime", "video/webm", "video/mpeg", "video/x-msvideo", "video/3gpp"}
AUDIO = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg", "audio/aac", "audio/flac", "audio/mp4", "audio/x-m4a"}
PDF = {"application/pdf"}
TEXT_EXT = {".csv", ".tsv", ".txt", ".md", ".json", ".jsonl", ".xml", ".log", ".yaml", ".yml", ".py", ".js", ".ts", ".html", ".sql"}
EXTRA_TYPES = {".csv": "text/csv", ".tsv": "text/tab-separated-values", ".md": "text/markdown", ".jsonl": "application/jsonl",
               ".m4a": "audio/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".yaml": "text/yaml", ".yml": "text/yaml"}

# Which kinds each model can read directly. Others get the analyses of the models that can.
CAN_READ = {
    "claude": {"image", "pdf", "text"},
    "chatgpt": {"image", "pdf", "text"},
    "gemini": {"image", "pdf", "text", "video", "audio"},
}

# Per-file size limits for sending a file inline to each provider's API.
DIRECT_LIMIT = {
    "claude": {"image": 5 * 1024 * 1024, "pdf": 30 * 1024 * 1024},
    "chatgpt": {"image": 20 * 1024 * 1024, "pdf": 30 * 1024 * 1024},
}


class FileRejected(ValueError):
    pass


@dataclass
class StoredFile:
    id: str
    name: str
    mime: str
    kind: str  # image | video | audio | pdf | text
    size: int
    data: bytes
    text: str = ""  # what goes into a prompt for text files (with a data profile for CSV)
    created_at: float = field(default_factory=time.time)

    def ref(self) -> dict:
        return {"id": self.id, "name": self.name, "mime": self.mime, "kind": self.kind, "size": self.size}

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode()


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def classify(name: str, mime: str) -> tuple[str, str]:
    """Return (kind, mime) or raise FileRejected."""
    ext = _ext(name)
    mime = (mime or "").split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        mime = EXTRA_TYPES.get(ext) or mimetypes.guess_type(name)[0] or ""
    if ext == ".svg" or mime in ("image/svg+xml", "text/html") and ext not in TEXT_EXT:
        raise FileRejected("SVG and HTML files are not accepted. Upload a PNG, JPG or PDF instead.")
    if mime in IMAGE:
        return "image", mime
    if mime in VIDEO or ext in (".mp4", ".mov", ".webm", ".mpeg", ".avi", ".3gp"):
        return "video", mime if mime in VIDEO else "video/mp4"
    if mime in AUDIO or ext in (".mp3", ".wav", ".ogg", ".aac", ".flac", ".m4a"):
        return "audio", "audio/mp4" if mime in ("audio/x-m4a",) else (mime if mime in AUDIO else "audio/mpeg")
    if mime in PDF or ext == ".pdf":
        return "pdf", "application/pdf"
    if ext in TEXT_EXT or mime.startswith("text/") or mime in ("application/json", "application/jsonl", "application/xml"):
        return "text", EXTRA_TYPES.get(ext) or (mime if mime.startswith("text/") else "text/plain")
    raise FileRejected(
        f"'{name}' is not a supported type. Use images (PNG, JPG, WEBP), video (MP4, MOV, WEBM), audio (MP3, WAV, M4A), "
        "PDF, or data files (CSV, TSV, TXT, MD, JSON)."
    )


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _num(v: str) -> float | None:
    try:
        return float(v.replace(",", "")) if v.strip() else None
    except ValueError:
        return None


def profile_table(text: str, delimiter: str) -> str:
    """A short data profile so models know the whole table even when only part fits in a prompt."""
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return ""
    if len(rows) < 2:
        return ""
    header, body = rows[0], rows[1:]
    lines = [f"Rows: {len(body)} (plus header). Columns: {len(header)}."]
    for i, col in enumerate(header[:60]):
        vals = [r[i] for r in body if i < len(r)]
        filled = [v for v in vals if v.strip()]
        nums = [n for n in (_num(v) for v in filled) if n is not None]
        if filled and len(nums) >= 0.9 * len(filled):
            mean = sum(nums) / len(nums)
            lines.append(f"- {col}: numeric, {len(filled)} values, min {min(nums):g}, max {max(nums):g}, mean {mean:.4g}, empty {len(vals) - len(filled)}")
        else:
            distinct = len(set(filled))
            sample = ", ".join(sorted(set(filled))[:5])
            lines.append(f"- {col}: text, {len(filled)} values, {distinct} distinct (e.g. {sample[:120]}), empty {len(vals) - len(filled)}")
    return "\n".join(lines)


def prepare(name: str, mime: str, data: bytes) -> StoredFile:
    name = (name or "file").replace("/", "_").replace("\\", "_").strip()[:180] or "file"
    if not data:
        raise FileRejected(f"'{name}' is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise FileRejected(f"'{name}' is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB. Trim or compress it first.")
    kind, mime = classify(name, mime)
    f = StoredFile(id=uuid.uuid4().hex[:16], name=name, mime=mime, kind=kind, size=len(data), data=data)
    if kind == "text":
        text = _decode(data)
        ext = _ext(name)
        profile = profile_table(text, "\t" if ext == ".tsv" else ",") if ext in (".csv", ".tsv") else ""
        body = text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + f"\n[... truncated: showing {TEXT_LIMIT:,} of {len(text):,} characters]"
        f.text = (f"Data profile (computed from the whole file):\n{profile}\n\n" if profile else "") + body
    return f


def describe(files: list[StoredFile]) -> str:
    if not files:
        return ""
    lines = [f"- {f.name} ({f.kind}, {f.size / 1024:.0f} KB)" for f in files]
    return "Attached files:\n" + "\n".join(lines)


class FileStore:
    """In-memory file store. Used locally and as the cache in front of BigQuery."""

    def __init__(self):
        self._staged: dict[str, StoredFile] = {}
        self._runs: dict[str, list[StoredFile]] = {}

    def stage(self, f: StoredFile) -> None:
        cutoff = time.time() - STAGE_TTL
        for fid in [k for k, v in self._staged.items() if v.created_at < cutoff]:
            self._staged.pop(fid, None)
        if sum(x.size for x in self._staged.values()) + f.size > 4 * MAX_RUN_BYTES:
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
        if sum(f.size for f in files) > MAX_RUN_BYTES:
            raise FileRejected(f"Files for one run must total under {MAX_RUN_BYTES // (1024 * 1024)} MB.")
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
