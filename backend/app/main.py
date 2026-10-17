"""SASI application backend: one API for the web app (and future mobile clients)."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import extract
from . import files as files_mod
from .config import Settings, get_settings
from .files import FileRejected, FileStore
from .gateway.registry import PROVIDERS, ModelRegistry
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


def build_store(settings: Settings):
    """Run store and file store. On Cloud Run both live in BigQuery."""
    if settings.storage_backend == "bigquery":
        from .store.bigquery import BigQuery, BigQueryFileStore, BigQueryStore

        db = BigQuery(settings.gcp_project, settings.bigquery_dataset)
        return BigQueryStore(settings.gcp_project, settings.bigquery_dataset, db), BigQueryFileStore(db)
    return MemoryStore(), FileStore()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    registry = ModelRegistry(settings)
    key_store = build_key_store(settings)
    store, file_store = build_store(settings)
    history = PerformanceHistory()
    controller = Controller(settings, registry, store, history, file_store)
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
        asyncio.create_task(registry.refresh())
        yield

    app = FastAPI(title="SASI", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.controller = controller
    app.state.store = store
    app.state.key_store = key_store

    def authed(request: Request) -> bool:
        if not settings.app_access_code:
            return True
        token = request.cookies.get(COOKIE)
        if not token:
            return False
        try:
            signer.loads(token, max_age=SESSION_MAX_AGE)
            return True
        except BadSignature:
            return False

    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_PATHS and not authed(request):
            return JSONResponse({"detail": "Enter the access code to continue."}, status_code=401)
        response = await call_next(request)
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
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
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
        return {"providers": registry.describe(), "needs_keys": registry.live_count == 0 and not settings.allow_simulated, "demo_mode": registry.live_count == 0 and settings.allow_simulated, "checked_at": registry.checked_at}

    @app.get("/api/keys")
    async def keys_status():
        """Which providers have a key. Never returns the keys themselves."""
        return [{"provider": p, "configured": p in registry.hints, "hint": registry.hints.get(p)} for p in PROVIDERS]

    @app.put("/api/keys/{provider}")
    async def save_key(provider: str, body: SaveKey):
        if provider not in PROVIDERS:
            raise HTTPException(404, "Unknown provider")
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
        # The key works, so use it right away even if saving it fails.
        registry.set_adapter(provider, key, model)
        result = {"provider": provider, "configured": True, "hint": registry.hints.get(provider), "model": model, "saved": True}
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
        if provider not in PROVIDERS:
            raise HTTPException(404, "Unknown provider")
        return await remove_key(provider)

    @app.delete("/api/keys")
    async def delete_all_keys():
        """Remove every key, for example before pasting a fresh set."""
        results = [await remove_key(p) for p in PROVIDERS]
        warnings = [r["warning"] for r in results if r.get("warning")]
        return {"removed": [r["provider"] for r in results], "saved": not warnings, "warning": warnings[0] if warnings else None}

    @app.get("/api/profiles")
    async def profiles():
        return [{"key": p.key, "name": p.name, "description": p.description} for p in PROFILES.values()]

    @app.get("/api/limits")
    async def limits():
        s = settings
        return {
            "max_model_calls_per_run": s.max_model_calls_per_run,
            "max_tokens_per_run": s.max_tokens_per_run,
            "eris_max_tokens_per_run": s.eris_max_tokens_per_run,
            "max_output_tokens_per_call": s.max_output_tokens_per_call,
            "max_tasks_per_plan": s.max_tasks_per_plan,
            "max_revisions_per_task": s.max_revisions_per_task,
            "max_concurrent_runs": s.max_concurrent_runs,
            "storage": s.storage_backend,
            "max_file_mb": files_mod.MAX_FILE_BYTES // (1024 * 1024),
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

    @app.post("/api/files")
    async def upload_file(request: Request):
        """Upload one file as the raw request body. The name comes in the X-File-Name header."""
        name = unquote(request.headers.get("x-file-name", "file"))
        declared = int(request.headers.get("content-length") or 0)
        if declared > files_mod.MAX_FILE_BYTES:
            raise HTTPException(413, f"'{name}' is larger than {files_mod.MAX_FILE_BYTES // (1024 * 1024)} MB.")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > files_mod.MAX_FILE_BYTES:
                raise HTTPException(413, f"'{name}' is larger than {files_mod.MAX_FILE_BYTES // (1024 * 1024)} MB.")
            chunks.append(chunk)
        try:
            f = files_mod.prepare(name, request.headers.get("content-type", ""), b"".join(chunks))
            file_store.stage(f)
        except FileRejected as e:
            raise HTTPException(400, str(e)) from e
        return f.ref()

    @app.delete("/api/files/{file_id}")
    async def remove_staged_file(file_id: str):
        file_store.unstage(file_id)
        return {"ok": True}

    @app.get("/api/runs/{run_id}/files/{file_id}")
    async def read_file(run_id: str, file_id: str):
        get_run(run_id)
        f = await file_store.get_one(run_id, file_id)
        if not f:
            raise HTTPException(404, "File not found")
        inline = f.kind in ("image", "video", "audio", "pdf")
        media = f.mime if inline else "text/plain; charset=utf-8" if extract.looks_like_text(f.data) else "application/octet-stream"
        return Response(f.data, media_type=media, headers={
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(f.name)}",
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'",
            "Cache-Control": "private, max-age=3600",
        })

    # ------------------------------------------------------------------ runs

    @app.post("/api/runs")
    async def create_run(body: CreateRun):
        if not registry.eligible():
            raise HTTPException(400, "Add at least one API key on the Connections page first.")
        if controller.active_count() >= settings.max_concurrent_runs:
            raise HTTPException(429, f"Up to {settings.max_concurrent_runs} runs can be active at once. Wait for one to finish.")
        try:
            files = file_store.take(body.file_ids)
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
                  files=[FileRef(**f.ref()) for f in files])
        try:
            await file_store.attach(run.id, files)
        except Exception as e:  # noqa: BLE001
            log.warning("could not save files for run %s: %s", run.id, e)
            raise HTTPException(500, "The files could not be saved. Try again.") from e
        await controller.start(run)
        return run.model_dump(mode="json")

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
        return [r.summary() for r in store.list(min(limit, 200))]

    @app.get("/api/runs/{run_id}")
    async def read_run(run_id: str):
        return get_run(run_id).model_dump(mode="json")

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
                yield f"data: {json.dumps(run.model_dump(mode='json'))}\n\n"
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
