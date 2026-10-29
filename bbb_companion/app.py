from __future__ import annotations

import asyncio
import json
import secrets
from datetime import date, datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic import ValidationError

from .engine import Engine
from .models import Config
from .desktop import Desktop
from .schedule import SAMARA, occurrences
from .storage import Storage
from .transfer import Bundle, export_bundle, import_bundle, revision

STATIC = Path(__file__).with_name("static")


class Command(BaseModel):
    id: str
    action: Literal["leave", "pause", "diagnostics", "show_browser", "hide_browser"]


class ImportCommand(BaseModel):
    bundle: Bundle
    mode: Literal["merge", "replace"] = "merge"
    revision: str | None = None


class StartupCommand(BaseModel):
    enabled: bool


class SubjectBrowserCommand(BaseModel):
    subject_id: str
    visible: bool


class DemoCommand(BaseModel):
    action: Literal["start", "wave", "count", "disconnect", "reconnect", "end"]
    count: int = Field(default=2, ge=0, le=200)
    text: str = Field(default="Здравствуйте", min_length=1, max_length=300)


def create_app(data_dir: Path, *, run_engine=True, desktop=None):
    storage = Storage(data_dir)
    engine = Engine(storage)
    token = secrets.token_urlsafe(32)
    desktop = desktop or Desktop(data_dir)

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
    app.state.desktop = desktop

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

    @app.get("/api/week")
    def week(start: date | None = None):
        start = start or datetime.now(SAMARA).date()
        monday = start - timedelta(days=start.weekday())
        return {"start": monday.isoformat(), "days": [(monday + timedelta(days=i)).isoformat() for i in range(7)],
                "lessons": [{"id": o.id, "lesson_id": o.lesson.id, "subject_id": o.lesson.subject_id,
                             "start": o.start.isoformat(), "end": o.end.isoformat(), "person_ids": o.lesson.person_ids,
                             "enabled": o.lesson.enabled}
                            for o in occurrences(storage.config, monday, 7, include_disabled=True) if o.end.date() >= monday]}

    @app.get("/api/desktop")
    def desktop_status():
        return desktop.status()

    @app.put("/api/desktop/autostart")
    def autostart(command: StartupCommand):
        try:
            result = desktop.set_autostart(command.enabled)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        storage.event("info", "autostart", "Автозапуск включён" if command.enabled else "Автозапуск выключен")
        return result

    @app.get("/api/export/data")
    def export_data(sections: str = "people,subjects,lessons"):
        selected = set(sections.split(","))
        if not selected or not selected <= {"people", "subjects", "lessons"}:
            raise HTTPException(400, "Выберите людей, предметы или расписание")
        bundle = export_bundle(storage.config, selected)
        return Response(bundle.model_dump_json(indent=2), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="bbb-schedule.json"'})

    def prepare_import(command):
        try:
            return import_bundle(storage.config, command.bundle, command.mode)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/import/preview")
    def preview_import(command: ImportCommand):
        with storage.lock:
            _, counts = prepare_import(command)
            return {"counts": counts, "revision": revision(storage.config)}

    @app.post("/api/import/data")
    def apply_import(command: ImportCommand):
        with storage.lock:
            if command.revision != revision(storage.config):
                raise HTTPException(409, "Данные изменились. Обновите предварительный просмотр импорта.")
            result, counts = prepare_import(command)
            storage.save_config(result)
        storage.event("info", "data_imported", "Данные импортированы", details={"mode": command.mode, "counts": counts})
        return result.model_dump(mode="json")

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

    @app.post("/api/subject-browser")
    async def subject_browser(command: SubjectBrowserCommand):
        try:
            await engine.subject_browser(command.subject_id, command.visible)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/demo")
    async def demo(command: DemoCommand):
        if command.action == "start":
            if not 1 <= command.count <= 15:
                raise HTTPException(400, "Выберите от одного до пятнадцати профилей")
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
