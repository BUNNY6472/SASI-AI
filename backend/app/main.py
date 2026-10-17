"""SASI application backend: one API for the web app (and future mobile clients)."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import Settings, get_settings
from .gateway.registry import ModelRegistry
from .models import TERMINAL, ApprovePlan, CreateRun, Login, Run
from .store.memory import MemoryStore
from .workflow.controller import Controller
from .workflow.profiles import PROFILES
from .workflow.router import PerformanceHistory

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("sasi")

COOKIE = "sasi_session"
SESSION_MAX_AGE = 7 * 24 * 3600
OPEN_PATHS = {"/api/health", "/api/login", "/api/session", "/api/logout"}


def build_store(settings: Settings):
    if settings.storage_backend == "bigquery":
        from .store.bigquery import BigQueryStore

        return BigQueryStore(settings.gcp_project, settings.bigquery_dataset)
    return MemoryStore()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    registry = ModelRegistry(settings)
    store = build_store(settings)
    history = PerformanceHistory()
    controller = Controller(settings, registry, store, history)
    signer = URLSafeTimedSerializer(settings.session_secret, salt="sasi-session")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
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
        return {"providers": registry.describe(), "demo_mode": registry.live_count == 0, "checked_at": registry.checked_at}

    @app.get("/api/profiles")
    async def profiles():
        return [{"key": p.key, "name": p.name, "description": p.description} for p in PROFILES.values()]

    @app.get("/api/limits")
    async def limits():
        s = settings
        return {
            "max_model_calls_per_run": s.max_model_calls_per_run,
            "max_tokens_per_run": s.max_tokens_per_run,
            "max_output_tokens_per_call": s.max_output_tokens_per_call,
            "max_tasks_per_plan": s.max_tasks_per_plan,
            "max_revisions_per_task": s.max_revisions_per_task,
            "max_concurrent_runs": s.max_concurrent_runs,
            "storage": s.storage_backend,
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

    @app.post("/api/runs")
    async def create_run(body: CreateRun):
        if controller.active_count() >= settings.max_concurrent_runs:
            raise HTTPException(429, f"Up to {settings.max_concurrent_runs} runs can be active at once. Wait for one to finish.")
        run = Run(request=body.request.strip(), materials=body.materials, profile=body.profile if body.profile in PROFILES else "general",
                  require_plan_approval=body.require_plan_approval)
        await controller.start(run)
        return run.model_dump(mode="json")

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
