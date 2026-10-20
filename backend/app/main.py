"""SASI application backend: one API for the web app (and future mobile clients)."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from . import extract
from . import files as files_mod
from .config import Settings, get_settings
from .files import FileRejected, FileStore
from .gateway.catalog import name
from .gateway.registry import KEY_SEATS, ModelRegistry
from .keys import build_key_store
from .models import TERMINAL, ApprovePlan, CreateRun, FileRef, Login, Run, SaveKey
from .store.memory import MemoryStore
from .workflow.controller import Controller
from .workflow.profiles import AUTO_FIELDS, PROFILES, detect_profile
from .workflow.router import PerformanceHistory

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("sasi")

COOKIE = "sasi_session"
SESSION_MAX_AGE = 7 * 24 * 3600
OPEN_PATHS = {"/api/health", "/api/login", "/api/session", "/api/logout"}
_EXTRACTION_WORKERS: set[asyncio.Task] = set()


class BeginUpload(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    mime: str = Field(default="application/octet-stream", max_length=150)
    size: int = Field(strict=True, ge=1, le=files_mod.MAX_FILE_BYTES)


def download_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()):
        raise HTTPException(416, "Only one valid byte range is supported.", headers={"Content-Range": f"bytes */{size}"})
    first, last = match.groups()
    if first:
        start, end = int(first), min(size - 1, int(last)) if last else size - 1
    else:
        suffix = int(last)
        start, end = max(0, size - suffix), size - 1
    if start > end or start >= size or (not first and int(last) == 0):
        raise HTTPException(416, "The requested range is outside this file.", headers={"Content-Range": f"bytes */{size}"})
    return start, end, True


async def _prepare_upload(slots: asyncio.Semaphore, name: str, mime: str, data: bytes,
                          timeout_s: float = 30):
    """Bound response wait and parser concurrency; a timed-out thread is not killed.

    The resource slot belongs to the actual worker, including after its caller
    times out or disconnects. New uploads cannot exceed the parser limit.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    await asyncio.wait_for(slots.acquire(), timeout=timeout_s)

    async def work():
        thread = asyncio.create_task(asyncio.to_thread(files_mod.prepare, name, mime, data))
        cancelled = False
        try:
            while not thread.done():
                try:
                    await asyncio.shield(thread)
                except asyncio.CancelledError:
                    cancelled = True
            result = thread.result()
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            slots.release()

    task = asyncio.create_task(work())
    _EXTRACTION_WORKERS.add(task)

    def finished(done):
        _EXTRACTION_WORKERS.discard(done)
        if not done.cancelled():
            done.exception()  # consume errors if the HTTP waiter already left

    task.add_done_callback(finished)
    return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.001, deadline - loop.time()))


def build_store(settings: Settings):
    """Cloud attachments use private objects; the legacy store remains readable."""
    if settings.storage_backend == "bigquery":
        from .store.bigquery import BigQuery, BigQueryFileStore, BigQueryStore

        db = BigQuery(settings.gcp_project, settings.bigquery_dataset)
        stores = BigQueryStore(settings.gcp_project, settings.bigquery_dataset, db), BigQueryFileStore(db)
    elif settings.storage_backend == "sqlite":
        from .store.sqlite import SQLiteDatabase, SQLiteFileStore, SQLiteStore
        db = SQLiteDatabase(settings.sqlite_path)
        stores = SQLiteStore(db), SQLiteFileStore(db)
    elif settings.storage_backend != "memory":
        raise ValueError("Unknown storage backend")
    else:
        stores = MemoryStore(), FileStore()
    if settings.upload_bucket:
        from .store.objects import ObjectFileStore
        return stores[0], ObjectFileStore(settings.upload_bucket, settings.gcp_project, stores[1])
    return stores


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.require_access_token:
        if not settings.app_access_code.strip():
            raise ValueError("REQUIRE_ACCESS_TOKEN requires a non-empty APP_ACCESS_CODE.")
        if not settings.session_secret.strip() or settings.session_secret.strip() == "dev-only-change-me":
            raise ValueError("REQUIRE_ACCESS_TOKEN requires a non-default SESSION_SECRET.")
    registry = ModelRegistry(settings)
    key_store = build_key_store(settings)
    store, file_store = build_store(settings)
    history = PerformanceHistory()
    controller = Controller(settings, registry, store, history, file_store)
    admission = asyncio.Lock()
    extraction_slots = asyncio.Semaphore(2)
    raw_upload_slots = asyncio.Semaphore(2)
    signer = URLSafeTimedSerializer(settings.session_secret, salt="sasi-session")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            registry.apply_keys(await key_store.load())
        except Exception as e:  # noqa: BLE001 - app still starts; Connections shows what is missing
            log.warning("could not load API keys: %s", type(e).__name__)
        runs = await store.load()
        # Rebuild routing history from accepted work on past runs.
        for run in runs:
            for t in run.plan.tasks if run.plan else []:
                for a in t.attempts:
                    if a.review and a.review.verdict in ("pass", "revise", "block"):
                        history.record(a.provider, run.profile, a.review.verdict == "pass")
        refresh_task = asyncio.create_task(registry.refresh())
        async def cleanup_uploads():
            while True:
                try:
                    await file_store.cleanup()
                except Exception as error:
                    log.warning("Upload cleanup will retry (%s).", type(error).__name__)
                await asyncio.sleep(900)
        upload_cleanup_task = asyncio.create_task(cleanup_uploads())
        try:
            yield
        finally:
            refresh_task.cancel()
            upload_cleanup_task.cancel()
            await asyncio.gather(refresh_task, upload_cleanup_task, return_exceptions=True)
            if hasattr(controller, "shutdown"):
                await controller.shutdown()

    app = FastAPI(title="SASI", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.controller = controller
    app.state.store = store
    app.state.key_store = key_store

    @app.exception_handler(FileRejected)
    async def file_rejected(_request, error: FileRejected):
        return JSONResponse({"detail": str(error)}, status_code=400)

    def authed(request: Request) -> bool:
        if not settings.app_access_code:
            return True
        token = request.cookies.get(COOKIE)
        if not token:
            return False
        try:
            return signer.loads(token, max_age=SESSION_MAX_AGE) == "ok"
        except BadSignature:
            return False

    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path
        api_path = path == "/api" or path.startswith("/api/")
        private_docs = settings.require_access_token and path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
        if (api_path or private_docs) and path not in OPEN_PATHS and not authed(request):
            response = JSONResponse({"detail": "Enter the access code to continue."}, status_code=401)
        else:
            response = await call_next(request)
        if api_path or private_docs:
            # Run data and attachments must not be reused from browser/proxy
            # caches after logout or an owner-token/session-secret rotation.
            response.headers["Cache-Control"] = "no-store"
        elif response.headers.get("content-type", "").startswith("text/html"):
            # A fresh navigation must load the current hashed JS bundle and its
            # upload protocol. Do not keep a pre-resumable HTML entry point.
            response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # ------------------------------------------------------------------ session

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": authed(request), "gate_enabled": bool(settings.app_access_code)}

    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
        if not settings.app_access_code:
            return {"authenticated": True}
        if not hmac.compare_digest(body.code.strip().encode(), settings.app_access_code.strip().encode()):
            await asyncio.sleep(1)  # slow down guessing
            raise HTTPException(401, "That access code is not right.")
        secure = settings.require_access_token or request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, signer.dumps("ok"), max_age=SESSION_MAX_AGE, httponly=True, samesite="strict", secure=secure)
        return {"authenticated": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"authenticated": False}

    # ------------------------------------------------------------------ providers and settings

    @app.get("/api/providers")
    async def providers(refresh: bool = False):
        if refresh or not registry.status:
            await registry.refresh()
        return {"providers": registry.describe(), "needs_keys": registry.live_count == 0 and not settings.allow_simulated
                and any(registry.key_route_allowed(p) for p in KEY_SEATS),
                "demo_mode": registry.live_count == 0 and settings.allow_simulated and not settings.trial_credits_only, "checked_at": registry.checked_at,
                "trial_credits_only": settings.trial_credits_only,
                "claude_api_key_allowed": registry.key_route_allowed("claude"),
                "trial_credit_expires_at": settings.trial_credit_expires_at.strip() or None,
                "trial_credit_window_error": registry.trial_credit_window_error(),
                "vertex": {"on": registry.uses_vertex, "project": settings.gcp_project, "location": settings.vertex_location},
                "team_size": settings.team_size}

    @app.get("/api/keys")
    async def keys_status():
        """Which providers have a key. Never returns the keys themselves."""
        return [{"provider": p, "configured": p in registry.hints, "hint": registry.hints.get(p)} for p in KEY_SEATS]

    @app.put("/api/keys/{provider}")
    async def save_key(provider: str, body: SaveKey):
        if provider not in KEY_SEATS:
            raise HTTPException(404, "SASI takes API keys for Claude and Gemini only. The open models run through Google Cloud.")
        if error := registry.key_route_error(provider):
            raise HTTPException(403, error)
        key = body.key.strip()
        if not 10 <= len(key) <= 500 or any(c.isspace() for c in key):
            raise HTTPException(400, "That does not look like an API key. Paste the whole key.")
        try:
            model = await registry.test_key(provider, key)
        except Exception as e:  # noqa: BLE001 - tell the user the key did not work, without echoing it
            raw = str(e).replace(key, "***")
            low = raw.lower()
            if any(t in low for t in ("401", "403", "authentication", "api key not valid", "invalid x-api-key", "incorrect api key", "permission")):
                msg = "the provider rejected it. Check that you copied the whole key and that it is active."
            elif any(t in low for t in ("429", "quota", "billing", "credit")):
                msg = "the key is valid but the account has no credit or quota left. Add billing in the provider's console."
            elif "timeout" in low or "timed out" in low:
                msg = "the provider did not answer in time. Try again."
            else:
                msg = raw[:200]
            raise HTTPException(400, f"That key did not work: {msg}") from e
        # The expiry may have passed during the provider check.
        if error := registry.key_route_error(provider):
            raise HTTPException(403, error)
        # The key works, so use it right away even if saving it fails.
        registry.set_adapter(provider, key, model)
        result = {"provider": provider, "configured": True, "hint": registry.hints.get(provider), "model": model, "saved": True,
                  "via": registry.status.get(provider, {}).get("via")}  # "vertex" when Google Cloud stays first and the key is the backup
        try:
            await key_store.save(provider, key)
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {str(e).replace(key, '***')[:300]}"
            log.warning("could not store key for %s: %s", provider, reason)
            result["saved"] = False
            result["warning"] = save_failure_message(type(e).__name__, reason)
        return result

    def save_failure_message(kind: str, reason: str) -> str:
        if kind == "PermissionDenied":
            how = ("Google Cloud did not let SASI save keys. In Cloud Shell run: "
                   "cd ~/SASI-AI && git pull && bash deploy/setup-gcp.sh , wait 2 minutes, then save the key again.")
        elif kind == "NotFound":
            how = "The place for this key does not exist in Secret Manager yet. In Cloud Shell run: cd ~/SASI-AI && bash deploy/setup-gcp.sh , then save the key again."
        else:
            how = "Try saving it again in a minute."
        return f"The key works and SASI is using it now, but it was not saved, so it will be lost when the app restarts. {how} (Details: {reason})"

    async def remove_key(provider: str) -> dict:
        """Stop using the key at once, then remove the stored copy. Removing never fails for the user."""
        registry.remove(provider)
        result = {"provider": provider, "configured": False, "removed": True, "saved": True}
        try:
            await key_store.delete(provider)
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("removed key for %s from the app, but not from storage: %s", provider, reason)
            result["saved"] = False
            result["warning"] = remove_failure_message(type(e).__name__, reason)
        return result

    def remove_failure_message(kind: str, reason: str) -> str:
        how = ("In Cloud Shell run: cd ~/SASI-AI && git pull && bash deploy/setup-gcp.sh , wait 2 minutes, then remove it again."
               if kind == "PermissionDenied" else "Remove it again in a minute.")
        return ("Removed. SASI stopped using this key, but Google Cloud did not let it delete the stored copy, so the key "
                f"would come back if the app restarts. {how} (Details: {reason})")

    @app.delete("/api/keys/{provider}")
    async def delete_key(provider: str):
        if provider not in KEY_SEATS:
            raise HTTPException(404, "Unknown provider")
        return await remove_key(provider)

    @app.delete("/api/keys")
    async def delete_all_keys():
        """Remove every key, for example before pasting a fresh set."""
        results = [await remove_key(p) for p in KEY_SEATS]
        warnings = [r["warning"] for r in results if r.get("warning")]
        return {"removed": [r["provider"] for r in results], "saved": not warnings, "warning": warnings[0] if warnings else None}

    @app.get("/api/profiles")
    async def profiles():
        return [{"key": p.key, "name": p.name, "description": p.description} for p in PROFILES.values()]

    @app.get("/api/limits")
    async def limits():
        s = settings
        return {
            "workflow": "bridge",
            "time_limit_minutes": 0,
            "wrap_up_minutes": 0,
            "timing_policy": "question_driven",
            "minimum_time_target_minutes": 0,
            "max_total_run_minutes": 0,
            "model_phase_min_minutes": 0,
            "model_phase_grace_minutes": 0,
            "provider_request_timeout_seconds": s.provider_request_timeout_seconds,
            "team_size": s.team_size,
            "max_model_calls_per_run": s.max_model_calls_per_run,  # 0: no cap
            "max_tokens_per_run": s.max_tokens_per_run,  # 0: no cap
            "eris_max_tokens_per_run": s.eris_max_tokens_per_run,  # 0: no cap
            "max_output_tokens_per_call": s.max_output_tokens_per_call,
            "eris_max_output_tokens_per_call": s.eris_max_output_tokens_per_call,
            "max_tasks_per_plan": s.max_tasks_per_plan,
            "max_revisions_per_task": s.max_revisions_per_task,
            "max_concurrent_runs": s.max_concurrent_runs,
            "storage": s.storage_backend,
            "max_file_mb": file_store.max_file_bytes // (1024 * 1024),
            "max_file_bytes": file_store.max_file_bytes,
            "max_run_bytes": file_store.max_run_bytes,
            "raw_upload_max_bytes": files_mod.RAW_UPLOAD_MAX_BYTES,
            "resumable_uploads": file_store.resumable_uploads,
            "upload_storage": "cloud" if file_store.resumable_uploads else "local",
            "preview_max_bytes": files_mod.PREVIEW_BYTES,
            "max_files_per_run": files_mod.MAX_FILES_PER_RUN,
        }

    @app.get("/api/stats")
    async def stats():
        return {"routing_history": history.snapshot()}

    # ------------------------------------------------------------------ runs

    def get_run(run_id: str) -> Run:
        run = store.get(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return run

    # ------------------------------------------------------------------ files

    @app.get("/api/files/uploads")
    async def list_uploads():
        if not file_store.resumable_uploads:
            return []
        try:
            return await file_store.list_uploads()
        except Exception as error:
            log.warning("Could not list staged uploads (%s).", type(error).__name__)
            raise HTTPException(503, "Saved uploads are temporarily unavailable. Try again.") from error

    @app.post("/api/files/uploads")
    async def initiate_upload(body: BeginUpload, request: Request):
        if not file_store.resumable_uploads:
            raise HTTPException(503, "Large uploads require the private cloud upload bucket to be configured.")
        origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
        parsed = urlsplit(origin)
        if parsed.netloc != request.url.netloc or parsed.scheme not in ("http", "https") or parsed.path:
            raise HTTPException(403, "Upload sessions must be created from this SASI website.")
        try:
            return await file_store.initiate(body.name, body.mime, body.size, origin)
        except FileRejected as error:
            raise HTTPException(400, str(error)) from error
        except Exception as error:
            log.warning("Could not start upload (%s).", type(error).__name__)
            raise HTTPException(503, "Cloud upload is temporarily unavailable. Try again.") from error

    @app.post("/api/files/uploads/{file_id}/complete")
    async def complete_upload(file_id: str):
        if not file_store.resumable_uploads:
            raise HTTPException(503, "Cloud uploads are not configured.")
        try:
            return (await file_store.complete(file_id)).ref()
        except FileRejected as error:
            raise HTTPException(400, str(error)) from error
        except Exception as error:
            log.warning("Could not finish upload (%s).", type(error).__name__)
            raise HTTPException(503, "The original upload may be saved, but processing did not finish. Retry finishing it.") from error

    async def _upload_file(request: Request):
        """Upload one file as the raw request body. The name comes in the X-File-Name header."""
        name = unquote(request.headers.get("x-file-name", "file"))
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            raise HTTPException(400, "Invalid content length") from None
        if declared > files_mod.RAW_UPLOAD_MAX_BYTES:
            raise HTTPException(413, "Files above 25 MiB require a resumable cloud upload.")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > files_mod.RAW_UPLOAD_MAX_BYTES:
                raise HTTPException(413, "Files above 25 MiB require a resumable cloud upload.")
            chunks.append(chunk)
        try:
            f = await _prepare_upload(extraction_slots, name, request.headers.get("content-type", ""), b"".join(chunks))
            f = await file_store.stage_async(f)
        except TimeoutError:
            raise HTTPException(422, "File processing exceeded its time limit. Try a smaller file.") from None
        except FileRejected as e:
            raise HTTPException(400, str(e)) from e
        return f.ref()

    @app.post("/api/files")
    async def upload_file(request: Request):
        # Bound body buffering as well as parsing; simultaneous small uploads
        # must not exhaust the Cloud Run process before acquiring a parser slot.
        try:
            async with asyncio.timeout(300):
                async with raw_upload_slots:
                    return await _upload_file(request)
        except TimeoutError:
            raise HTTPException(408, "Upload timed out. Please retry.") from None

    @app.delete("/api/files/{file_id}")
    async def remove_staged_file(file_id: str):
        try:
            await file_store.unstage_async(file_id)
        except FileRejected as error:
            raise HTTPException(400, str(error)) from error
        except Exception as error:
            log.warning("Upload cleanup postponed (%s).", type(error).__name__)
            raise HTTPException(503, "Cloud cleanup will retry. Try removing the upload again.") from error
        return {"ok": True}

    @app.get("/api/runs/{run_id}/files/{file_id}")
    async def read_file(run_id: str, file_id: str, request: Request):
        get_run(run_id)
        f = await file_store.get_one(run_id, file_id)
        if not f:
            raise HTTPException(404, "File not found")
        inline = f.kind in ("image", "video", "audio", "pdf")
        start, end, partial = download_range(request.headers.get("range"), f.size)
        headers = {"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(f.name)}",
                   "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'",
                   "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{f.size}"
        if f.object_uri:
            # Uvicorn must use chunked transfer for large HTTP/1 responses to
            # bypass Cloud Run's 32 MiB non-streaming response limit.
            if end - start + 1 > files_mod.RAW_UPLOAD_MAX_BYTES:
                headers.pop("Content-Length", None)
            return StreamingResponse(file_store.stream(f, start, end), media_type=f.mime if inline else "application/octet-stream",
                                     status_code=206 if partial else 200, headers=headers)
        media = f.mime if inline else "text/plain; charset=utf-8" if extract.looks_like_text(f.data) else "application/octet-stream"
        return Response(f.data[start:end + 1], media_type=media, status_code=206 if partial else 200, headers=headers)

    @app.get("/api/runs/{run_id}/files/{file_id}/preview")
    async def read_file_preview(run_id: str, file_id: str, offset: int = 0, max_bytes: int = 262144):
        get_run(run_id)
        f = await file_store.get_one(run_id, file_id)
        if not f:
            raise HTTPException(404, "File not found")
        if offset < 0 or offset >= f.size or not 1 <= max_bytes <= 262144:
            raise HTTPException(400, "Choose a valid byte offset and a preview of 1 to 262144 bytes.")
        end = min(f.size - 1, offset + max_bytes - 1)
        data = await file_store.read_range(f, offset, end) if f.object_uri else f.data[offset:end + 1]
        readable = f.kind == "text" and extract.looks_like_text(data)
        return {"id": f.id, "name": f.name, "size": f.size, "offset": offset, "bytes": len(data),
                "next_offset": end + 1 if end + 1 < f.size else None,
                "text": data.decode("utf-8", errors="replace") if readable else None,
                "coverage_note": ("Bounded original byte range only; partial lines and UTF-8 characters may be cut at its edges."
                                  if readable else "Binary contents are not rendered as text. Download the original to inspect it.")}

    # ------------------------------------------------------------------ runs

    async def _create_run(body: CreateRun):
        window_error = registry.trial_credit_window_error()
        if window_error:
            raise HTTPException(403, window_error)
        await registry.recheck_failed()  # a key that failed before may work again
        if not registry.eligible():
            if settings.trial_credits_only:
                if registry.key_route_allowed("claude"):
                    raise HTTPException(400, "Connect your own Claude API key or a working Vertex Gemini connection on the Connections page. Other provider routes are disabled by trial-credit-only policy.")
                raise HTTPException(400, "Trial-credit-only policy requires a working Vertex Gemini connection. Check the Google Cloud project and Vertex AI settings.")
            if registry.live_count:
                why = "; ".join(f"{name(p)}: {r}" for p, r in registry.unavailable().items())
                if registry.uses_vertex:
                    raise HTTPException(400, f"None of the models is working right now ({why}). See the Connections page.")
                raise HTTPException(400, f"None of your API keys is working right now ({why}). Replace them on the Connections page.")
            raise HTTPException(400, "Add at least one API key on the Connections page first.")
        if controller.active_count() >= settings.max_concurrent_runs:
            raise HTTPException(429, f"Up to {settings.max_concurrent_runs} runs can be active at once. Wait for one to finish.")
        try:
            files = await file_store.take_async(body.file_ids)
        except FileRejected as e:
            raise HTTPException(400, str(e)) from e
        eris = body.eris or body.profile == "eris"
        if eris:
            profile = "eris"
        elif body.profile in AUTO_FIELDS:
            profile = body.profile
        else:
            profile = detect_profile(body.request, body.materials, [f.name for f in files])
        run = Run(request=body.request.strip(), materials=body.materials, profile=profile, eris=eris,
                  require_plan_approval=body.require_plan_approval, compare_all=body.compare_all,
                  mode=body.mode, workflow="bridge", timing_policy="question_driven",
                  time_limit_minutes=None,
                  files=[FileRef(**f.ref()) for f in files])
        try:
            await file_store.attach(run.id, files)
        except Exception as e:  # noqa: BLE001
            log.warning("could not save files for run %s: %s", run.id, e)
            if hasattr(file_store, "rollback_attach"):
                await file_store.rollback_attach(run.id, files)
            else:
                for f in files:
                    await file_store.stage_async(f)
            raise HTTPException(500, "The files could not be saved. Try again.") from e
        try:
            await controller.start(run)
        except Exception as e:
            run.status = "failed"
            run.error = "The initial checkpoint could not be saved. No model work was started."
            if hasattr(file_store, "rollback_attach"):
                await file_store.rollback_attach(run.id, files)
            else:
                for f in files:
                    await file_store.stage_async(f)
            log.warning("could not start run %s: %s", run.id, type(e).__name__)
            raise HTTPException(503, run.error) from e
        return run.model_dump(mode="json")

    @app.post("/api/runs")
    async def create_run(body: CreateRun):
        # Reserve capacity through asynchronous attachment/checkpoint writes.
        async with admission:
            return await _create_run(body)

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str):
        """Delete a finished run with its files and every model output, from BigQuery too."""
        run = get_run(run_id)
        try:
            state = await controller.delete(run)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"deleted": True, "bigquery": state}

    @app.get("/api/runs")
    async def list_runs(limit: int = 50):
        return [r.summary() for r in store.list(max(1, min(limit, 200)))]

    @app.get("/api/runs/{run_id}")
    async def read_run(run_id: str):
        return get_run(run_id).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/solution")
    async def download_solution(run_id: str):
        """The code file in the run's final result (solution.py for Project Eris runs), as a download."""
        sol = get_run(run_id).solution
        if not sol:
            raise HTTPException(404, "This run's result has no code file.")
        return Response(sol["code"].encode(), media_type="text/x-python; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(sol['name'])}",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "no-store",
        })

    @app.post("/api/runs/{run_id}/approve")
    async def approve(run_id: str, body: ApprovePlan | None = None):
        run = get_run(run_id)
        try:
            await controller.approve(run, (body.note if body else "").strip()[:500])
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return run.model_dump(mode="json")

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        run = get_run(run_id)
        await controller.cancel(run)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/resume")
    async def resume(run_id: str):
        run = get_run(run_id)
        try:
            async with admission:
                if controller.active_count() >= settings.max_concurrent_runs:
                    raise HTTPException(429, "All run slots are occupied. Wait for a run to finish.")
                await controller.resume(run)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return run.model_dump(mode="json")

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        run = get_run(run_id)
        queue = controller.subscribe(run_id)

        async def stream():
            try:
                initial_terminal = run.status in TERMINAL and not controller.is_active(run.id)
                yield f"data: {json.dumps(run.model_dump(mode='json'))}\n\n"
                if initial_terminal:
                    return
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    current = store.get(run_id)
                    if current is None:
                        break
                    yield f"data: {json.dumps(current.model_dump(mode='json'))}\n\n"
                    if current.status in TERMINAL and current.status != "interrupted":
                        break
            finally:
                controller.unsubscribe(run_id, queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ web app

    static = Path(settings.static_dir)
    if static.is_dir():
        if (static / "_next").is_dir():
            app.mount("/_next", StaticFiles(directory=static / "_next"), name="next")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            candidate = (static / path).resolve()
            if path and candidate.is_file() and static.resolve() in candidate.parents:
                return FileResponse(candidate)
            html = (static / f"{path}.html").resolve() if path else None
            if html and html.is_file() and static.resolve() in html.parents:
                return FileResponse(html)
            return FileResponse(static / "index.html")

    return app


app = create_app()
