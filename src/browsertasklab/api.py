import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Runner
from .config import Settings
from .demos import router
from .models import RunRequest, TaskState, TERMINAL
from .security import redact, reject_secrets
from .store import Store


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    digest: str


def create_app(settings=None):
    # Windows registry MIME associations can label .js as text/plain; nosniff then blocks it.
    import mimetypes
    mimetypes.init()
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    settings = settings or Settings()
    settings.prepare()
    store = Store(settings.runtime_dir / "lab.sqlite3")
    runners, tasks = {}, {}
    evaluation = {"task": None, "status": "idle", "reports": []}

    @asynccontextmanager
    async def lifespan(app):
        # Persisted browser sessions cannot be resumed safely after a server restart.
        for snapshot in store.list_runs():
            if snapshot["status"] not in TERMINAL:
                snapshot["status"] = "stopped"
                snapshot["phase"] = "server_restarted"
                with store.connect() as db:
                    import json
                    db.execute("UPDATE runs SET snapshot=? WHERE id=?", (json.dumps(snapshot), snapshot["id"]))
        yield
        for task in tasks.values():
            if not task.done():
                task.cancel()
        if evaluation["task"] and not evaluation["task"].done():
            evaluation["task"].cancel()
        await asyncio.gather(*tasks.values(), *([evaluation["task"]] if evaluation["task"] else []), return_exceptions=True)

    app = FastAPI(title="BrowserTaskLab", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    app.state.store, app.state.settings, app.state.runners = store, settings, runners
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")
    app.include_router(router(store))

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "Cross-origin mutation is forbidden"}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "JSON requests are required"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        return response

    @app.get("/")
    async def home():
        return FileResponse(static / "index.html")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "simulation_only": True}

    @app.get("/api/config")
    async def config():
        import os
        from dotenv import dotenv_values
        return {"provider": settings.provider, "model": settings.model,
                "llm_configured": bool(os.environ.get("OPENAI_API_KEY") or dotenv_values(".env").get("OPENAI_API_KEY")),
                "pricing": settings.pricing(), "simulation_only": True}

    @app.post("/api/runs", status_code=201)
    async def start(request: RunRequest):
        try:
            reject_secrets(request.task)
        except ValueError as error:
            raise HTTPException(422, str(error))
        if sum(not t.done() for t in tasks.values()) >= 2 or evaluation["status"] == "running":
            raise HTTPException(409, "Two runs are already active, or an evaluation is running")
        run_id = secrets.token_hex(12)
        store.create_demo(run_id, request.workflow, request.variant)
        state = TaskState(id=run_id, request=request)
        store.save_run(state)
        runner = Runner(state, settings, store)
        runners[run_id] = runner
        tasks[run_id] = asyncio.create_task(runner.run())
        return {"id": run_id}

    @app.get("/api/runs")
    async def list_runs():
        return [{k: row[k] for k in ["id", "status", "phase", "request", "elapsed_seconds"]} for row in store.list_runs()]

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        row = store.get_run(run_id)
        if not row:
            raise HTTPException(404, "Run not found")
        return row

    @app.post("/api/runs/{run_id}/stop")
    async def stop(run_id: str):
        runner = runners.get(run_id)
        if not runner:
            raise HTTPException(404, "Active run not found")
        if runner.state.status not in TERMINAL:
            runner.stop_event.set()
            if runner.state.status == "queued":
                runner.finish("stopped", "user_stopped", unresolved=["Stopped before browser startup"])
            tasks[run_id].cancel()
        return {"stop_requested": True}

    @app.get("/api/runs/{run_id}/review")
    async def get_review(run_id: str):
        runner = runners.get(run_id)
        if not runner or not runner.state.pending_review:
            raise HTTPException(409, "No pending action to review")
        # Concrete synthetic details are shown here; persisted trace snapshots redact emails.
        return runner.state.pending_review

    @app.post("/api/runs/{run_id}/review")
    async def review(run_id: str, decision: ReviewDecision):
        runner = runners.get(run_id)
        if not runner or runner.state.status != "awaiting_review" or not runner.state.pending_review:
            raise HTTPException(409, "No pending action to review")
        if decision.digest != runner.state.pending_review["digest"]:
            raise HTTPException(409, "The review does not match the pending action")
        runner.review_approved = decision.approve
        runner.review_event.set()
        return {"decision_received": True}

    @app.get("/api/screenshots/{filename}")
    async def screenshot(filename: str):
        import re
        if not re.fullmatch(r"[a-f0-9]{24}-\d{3}\.png", filename):
            raise HTTPException(404)
        path = settings.runtime_dir / "screenshots" / filename
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.get("/api/evaluations")
    async def evaluations():
        import json
        reports = []
        for file in sorted((settings.runtime_dir / "evaluations").glob("*.json"), reverse=True)[:10]:
            try:
                report = json.loads(file.read_text(encoding="utf-8"))
                reports.append({k: report[k] for k in ["id", "metadata", "summary", "rows"]})
            except (ValueError, KeyError):
                continue
        return {"status": evaluation["status"], "reports": reports}

    @app.post("/api/evaluations", status_code=202)
    async def run_evaluation(request: Request):
        from .evaluation.runner import evaluate_suite
        data = await request.json()
        if data.get("mode") not in {"mock", "baseline", "llm"} or data.get("split") not in {"development", "held_out"}:
            raise HTTPException(422, "Choose mode and split")
        if evaluation["status"] == "running" or any(not t.done() for t in tasks.values()):
            raise HTTPException(409, "Wait for active work to finish")
        evaluation["status"] = "running"

        async def evaluate():
            try:
                await evaluate_suite(settings, store, data["mode"], data["split"])
                evaluation["status"] = "finished"
            except asyncio.CancelledError:
                evaluation["status"] = "stopped"
                raise
            except Exception:
                evaluation["status"] = "failed"

        evaluation["task"] = asyncio.create_task(evaluate())
        return {"status": "running"}

    return app
