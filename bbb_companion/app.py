from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import Engine
from .models import Config
from .storage import Storage

STATIC = Path(__file__).with_name("static")


class Command(BaseModel):
    id: str
    action: Literal["leave", "pause", "diagnostics"]


class DemoCommand(BaseModel):
    action: Literal["start", "wave", "count", "disconnect", "reconnect", "end"]
    count: int = Field(default=2, ge=0, le=200)
    text: str = Field(default="Здравствуйте", min_length=1, max_length=300)


def create_app(data_dir: Path, *, run_engine=True):
    storage = Storage(data_dir)
    engine = Engine(storage)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        if run_engine:
            await engine.start()
        try:
            yield
        finally:
            if run_engine:
                await engine.stop()
            storage.close()

    app = FastAPI(title="BBB Companion", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.storage = storage
    app.state.token = token
    app.state.shutdown = None

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return Response("Локальный доступ", status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(request.headers.get("x-app-token", ""), token):
                return Response("Откройте панель приложения заново", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8").replace("__APP_TOKEN__", token)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/config")
    def config():
        return storage.config.model_dump(mode="json")

    @app.put("/api/config")
    def save_config(config: Config):
        storage.save_config(config)
        return config.model_dump(mode="json")

    @app.get("/api/status")
    def status():
        return engine.status()

    @app.get("/api/events")
    def events(after: int = Query(0, ge=0), before: int | None = Query(None, ge=1),
               limit: int = Query(200, ge=1, le=1000), person_id: str | None = None,
               subject_id: str | None = None, level: str | None = None):
        return storage.events(after=after, before=before, limit=limit, person_id=person_id, subject_id=subject_id, level=level)

    @app.get("/api/export/log")
    def export_log():
        lines = []
        cursor = 0
        while True:
            rows = storage.events(after=cursor, limit=1000)
            if not rows:
                break
            lines.extend(json.dumps(row, ensure_ascii=False) for row in rows)
            cursor = rows[-1]["id"]
        return Response("\n".join(lines), media_type="application/x-ndjson", headers={"Content-Disposition": 'attachment; filename="bbb-log.jsonl"'})

    @app.post("/api/action")
    async def action(command: Command):
        try:
            await engine.command(command.id, command.action)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/demo")
    async def demo(command: DemoCommand):
        if command.action == "start":
            if not 1 <= command.count <= 5:
                raise HTTPException(400, "Выберите от одного до пяти профилей")
            try:
                await engine.start_demo(command.count)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return {"ok": True}
        room = engine.demo_rooms.get("demo://sandbox")
        if room is None:
            raise HTTPException(400, "Сначала запустите демонстрацию")
        if command.action == "wave":
            for index in range(6):
                room.post(f"external_{index}", f"Участник {index+1}", command.text)
        elif command.action == "count":
            room.external = command.count
        elif command.action == "disconnect":
            room.healthy = False
        elif command.action == "reconnect":
            room.healthy = True
        elif command.action == "end":
            room.ended = True
            engine.finish_room("demo://sandbox", "Демонстрационная встреча завершена")
        return {"ok": True}

    @app.post("/api/shutdown")
    async def shutdown():
        if app.state.shutdown:
            asyncio.get_running_loop().call_later(.3, app.state.shutdown)
        return {"ok": True}

    return app
