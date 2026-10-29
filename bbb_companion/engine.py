from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .browser import BrowserAdapter, BrowserPool, MeetingEnded
from .demo import DemoAdapter, DemoRoom
from .models import Person, Settings, Subject
from .rules import ChatDetector, LeaveDetector, normalize
from .schedule import SAMARA, due, occurrences
from .storage import TERMINAL, Storage


@dataclass
class Session:
    id: str
    snapshot: dict
    state: str = "waiting"
    paused: bool = False
    adapter: object | None = None
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    joined_at: float = 0
    healthy: bool = False
    listen_only: bool = False
    own_id: str | None = None
    total: int | None = None
    finish_reason: str | None = None
    finish_state: str = "finished"
    error: str | None = None
    warning: str | None = None
    chat_layout: str | None = None

    @property
    def room_key(self):
        return self.snapshot["subject"]["url"].rstrip("/")

    @property
    def settings(self):
        return Settings.model_validate(self.snapshot["settings"])

    def public(self):
        return {"id": self.id, "person_id": self.snapshot["person"]["id"], "name": self.snapshot["person"]["name"],
                "subject_id": self.snapshot["subject"]["id"], "subject": self.snapshot["subject"]["title"],
                "adapter": self.snapshot["subject"]["adapter"], "state": self.state, "paused": self.paused,
                "healthy": self.healthy, "listen_only": self.listen_only, "total": self.total,
                "window_supported": bool(getattr(self.adapter, "window", None)),
                "browser_visible": bool(getattr(self.adapter, "browser_visible", False)),
                "start": self.snapshot["start"], "end": self.snapshot["end"], "error": self.error, "warning": self.warning}


@dataclass
class Room:
    chat: ChatDetector
    leave: LeaveDetector
    owner: str | None = None


class Engine:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.pool = BrowserPool()
        self.sessions: dict[str, Session] = {}
        self.rooms: dict[str, Room] = {}
        self.demo_rooms: dict[str, DemoRoom] = {}
        self.running = False
        self.loop_task = None
        self.reported_blocks = set()

    def event(self, session, level, kind, message, details=None):
        return self.storage.event(level, kind, message, run_id=session.id, person_id=session.snapshot["person"]["id"],
                                  subject_id=session.snapshot["subject"]["id"], details=details)

    def state(self, session, value):
        if session.state != value:
            session.state = value
            self.storage.update_run(session.id, state=value)
            self.event(session, "info", "state", value)

    async def start(self):
        self.running = True
        self.storage.prune(self.storage.config.settings.log_retention_days)
        self.storage.event("info", "started", "Приложение запущено")
        if not self.storage.config_error:
            for row in self.storage.unfinished():
                if time.time() >= row["snapshot"]["hard_end"]:
                    self.storage.update_run(row["id"], state="expired")
                    continue
                if len(self.active()) < self.storage.config.settings.max_connections:
                    self.spawn(row)
        self.loop_task = asyncio.create_task(self.loop())

    async def stop(self):
        self.running = False
        self.pool.stopping = True
        if self.loop_task:
            await self.loop_task
        # Close contexts first to release any pending navigation/audio waits.
        await asyncio.gather(*(s.adapter.close() for s in self.active() if s.adapter), return_exceptions=True)
        tasks = [s.task for s in self.sessions.values() if s.task and not s.task.done()]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=8)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self.pool.close()
        self.storage.event("info", "stopped", "Приложение остановлено; незавершённые занятия сохранены для восстановления")

    def active(self):
        return [s for s in self.sessions.values() if s.state not in TERMINAL and s.state != "interrupted"]

    def spawn(self, row):
        if row["id"] in self.sessions:
            return
        session = Session(row["id"], row["snapshot"], paused=bool(row["paused"]))
        self.sessions[session.id] = session
        if session.room_key not in self.rooms:
            settings = session.settings
            self.rooms[session.room_key] = Room(ChatDetector(settings.chat, self.storage.room(session.room_key)), LeaveDetector(settings.leave))
        session.task = asyncio.create_task(self.worker(session))

    async def loop(self):
        while self.running:
            try:
                await self.tick()
            except Exception as exc:
                self.storage.event("error", "scheduler", f"Ошибка планировщика: {exc}")
            await asyncio.sleep(1)

    async def tick(self):
        config = self.storage.config
        if not config.settings.automation_enabled or self.storage.config_error:
            return
        now = datetime.now(SAMARA)
        people = {p.id: p for p in config.people if p.enabled}
        subjects = {s.id: s for s in config.subjects}
        for occurrence in due(config, now):
            for person_id in occurrence.lesson.person_ids:
                if person_id not in people:
                    continue
                key = f"{occurrence.id}/{person_id}"
                row = self.storage.run(key)
                if row:
                    if row["state"] not in TERMINAL and key not in self.sessions and len(self.active()) < config.settings.max_connections:
                        self.spawn(row)
                    continue
                if len(self.active()) >= config.settings.max_connections:
                    self.blocked(key, "Достигнут лимит подключений")
                    continue
                if any(s.snapshot["person"]["id"] == person_id for s in self.active()):
                    self.blocked(key, "У человека уже идёт другое занятие")
                    continue
                snapshot = self.make_snapshot(people[person_id], subjects[occurrence.lesson.subject_id], config.settings,
                                              occurrence.start, occurrence.end)
                self.storage.create_run(key, person_id, occurrence.id, snapshot)
                self.spawn(self.storage.run(key))

    def blocked(self, key, message):
        if key not in self.reported_blocks:
            self.reported_blocks.add(key)
            self.storage.event("warning", "schedule_conflict", message, details={"run": key})

    @staticmethod
    def make_snapshot(person, subject, settings, start, end):
        now = time.time()
        return {"person": person.model_dump(mode="json"), "subject": subject.model_dump(mode="json"),
                "settings": settings.model_dump(mode="json"), "start": start.isoformat(), "end": end.isoformat(),
                "hard_end": end.timestamp() + settings.grace_minutes * 60,
                "join_deadline": min(end.timestamp() + settings.grace_minutes * 60, max(start.timestamp() + settings.wait_minutes * 60, now + settings.wait_minutes * 60)),
                "ever_joined": False}

    async def worker(self, session):
        failures = 0
        unhealthy = 0
        ready = False
        try:
            while self.running and not session.finish_reason:
                settings = session.settings
                if time.time() >= session.snapshot["hard_end"]:
                    session.finish_reason = "Достигнуто предельное время пребывания"
                    break
                try:
                    if session.adapter is None:
                        self.state(session, "joining" if not session.snapshot["ever_joined"] else "recovering")
                        subject = session.snapshot["subject"]
                        if subject["adapter"] == "demo":
                            room = self.demo_rooms.setdefault(session.room_key, DemoRoom())
                            session.adapter = DemoAdapter(room, session.snapshot["person"]["name"], session.id)
                        else:
                            session.adapter = BrowserAdapter(self.pool, subject["url"], session.snapshot["person"]["name"], settings.headless, subject["id"])
                        async with session.lock:
                            result = await session.adapter.connect()
                    elif not ready:
                        async with session.lock:
                            result = await session.adapter.advance_join()
                    else:
                        result = True
                    if result is False:
                        async with session.lock:
                            pending = await session.adapter.observe()
                        if pending.ended:
                            self.finish_room(session.room_key, "Встреча завершена преподавателем")
                            break
                        if time.time() >= session.snapshot["join_deadline"]:
                            session.finish_state = "failed"
                            session.finish_reason = "Истекло время ожидания начала занятия"
                            break
                        self.state(session, "waiting" if pending.waiting else "joining")
                        if pending.waiting_reason != session.warning:
                            session.warning = pending.waiting_reason
                            if session.warning:
                                self.event(session, "info", "waiting_room", session.warning + "; ожидаем в той же вкладке")
                        await asyncio.sleep(1)
                        continue
                    if not ready:
                        ready = True
                        session.joined_at = time.time()
                        session.snapshot["ever_joined"] = True
                        self.storage.update_snapshot(session.id, session.snapshot)
                        session.error = None
                        failures = unhealthy = 0
                        self.state(session, "active")
                        self.event(session, "info", "joined", "Вход выполнен; выбран режим «Только слушать»")
                    async with session.lock:
                        observation = await session.adapter.observe()
                    session.healthy, session.listen_only = observation.healthy, observation.listen_only
                    session.total, session.own_id = observation.total, observation.own_id
                    if observation.chat_layout is not None and observation.chat_layout != session.chat_layout and not observation.ended:
                        session.chat_layout = observation.chat_layout
                        self.event(session, "info", "chat_ready", f"Чат: формат {observation.chat_layout}, распознано {observation.chat_parsed} из {observation.chat_count} сообщений",
                                   {"layout": observation.chat_layout, "parsed": observation.chat_parsed, "found": observation.chat_count})
                    if observation.warning != session.warning:
                        session.warning = observation.warning
                        if session.warning:
                            self.event(session, "warning", "adapter_warning", session.warning)
                    if observation.ended:
                        self.finish_room(session.room_key, "Встреча завершена сервером")
                        break
                    elif not observation.healthy or not observation.listen_only:
                        unhealthy += 1
                        room = self.rooms[session.room_key]
                        if room.owner == session.id:
                            room.owner = None
                            room.leave.reset_observation()
                        if unhealthy >= 3:
                            raise RuntimeError("Потеря соединения или режима «Только слушать»")
                    else:
                        unhealthy = 0
                        await self.process(session, observation)
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise
                except MeetingEnded:
                    self.finish_room(session.room_key, "Встреча завершена преподавателем")
                    break
                except Exception as exc:
                    if not self.running or session.finish_reason:
                        break
                    failures += 1
                    session.healthy = session.listen_only = False
                    session.error = str(exc)
                    self.event(session, "warning", "connection_error", str(exc), {"attempt": failures})
                    if session.adapter:
                        if failures == 1:
                            await self.diagnostics(session, automatic=True)
                        try:
                            async with session.lock:
                                await session.adapter.close()
                        except Exception:
                            pass
                        session.adapter = None
                        ready = False
                    room = self.rooms[session.room_key]
                    if room.owner == session.id:
                        room.owner = None
                        room.leave.reset_observation()
                    exhausted = failures >= settings.max_retries if session.snapshot["ever_joined"] else time.time() >= session.snapshot["join_deadline"]
                    if exhausted:
                        session.finish_state = "failed"
                        session.finish_reason = "Исчерпаны попытки восстановления" if session.snapshot["ever_joined"] else "Истекло время ожидания входа"
                        break
                    self.state(session, "recovering" if session.snapshot["ever_joined"] else "waiting")
                    # Small sleeps keep shutdown and manual stop responsive.
                    for _ in range(settings.retry_seconds):
                        if not self.running or session.finish_reason:
                            break
                        await asyncio.sleep(1)
        finally:
            if session.adapter:
                try:
                    await session.adapter.close()
                except Exception:
                    pass
            session.healthy = session.listen_only = False
            if session.finish_reason:
                self.state(session, session.finish_state)
                self.event(session, "info", "left", session.finish_reason)
            else:
                self.state(session, "interrupted")

    async def process(self, session, observation):
        room = self.rooms[session.room_key]
        members = [s for s in self.active() if s.room_key == session.room_key]
        eligible = [s for s in members if s.healthy and s.listen_only and s.state == "active"]
        if room.owner not in {s.id for s in eligible}:
            room.owner = session.id
            room.leave.reset_observation()
        if room.owner != session.id:
            return
        own_ids = {s.own_id for s in members if s.own_id}
        own_names = {normalize(s.snapshot["person"]["name"]) for s in self.sessions.values() if s.room_key == session.room_key}
        received = [message for message in observation.messages if message.author not in own_ids and normalize(message.name) not in own_names]
        for message in observation.messages:
            for reaction in room.chat.add(message, own_ids, own_names):
                self.event(session, "info", "wave", f"{reaction.group}: {reaction.authors} из {reaction.total} авторов", {"wave": reaction.id, "text": reaction.text})
                # Persist before sending, so a restart cannot repeat this wave.
                self.storage.save_room(session.room_key, room.chat.dump())
                for target in eligible:
                    if target.paused or target.joined_at > reaction.at or target.finish_reason:
                        continue
                    if not self.storage.claim_send(target.id, reaction.id, reaction.text):
                        continue
                    if target.settings.dry_run:
                        self.storage.finish_send(target.id, reaction.id, "dry_run")
                        self.event(target, "info", "would_send", f"Наблюдение: было бы отправлено «{reaction.text}»")
                        continue
                    try:
                        async with target.lock:
                            confirmed = await target.adapter.send(reaction.text)
                        result = "confirmed" if confirmed else "unknown"
                        self.storage.finish_send(target.id, reaction.id, result)
                        self.event(target, "info" if confirmed else "warning", "sent", f"Сообщение «{reaction.text}»: {'отправлено' if confirmed else 'результат не подтверждён, повтор отключён'}")
                    except Exception as exc:
                        self.storage.finish_send(target.id, reaction.id, "unknown")
                        self.event(target, "warning", "send_unknown", f"Результат отправки неизвестен: {exc}; повтор отключён")
        if observation.messages:
            self.storage.save_room(session.room_key, room.chat.dump())
        if received:
            progress = room.chat.progress(time.time())
            summary = "; ".join(f"{group}: {item['authors']}/{item['total']} ({item['ratio']:.0%})" for group, item in progress.items() if item["authors"])
            self.event(session, "info", "chat_observed", f"Чат: новых сообщений {len(received)}" + ("; " + summary if summary else "; разрешённых реакций нет"),
                       {"messages": [{"author": message.name, "text": message.text[:300]} for message in received],
                        "groups": progress, "minimum_authors": room.chat.settings.min_authors, "minimum_ratio": room.chat.settings.author_ratio})
        # While other profiles are still entering, the total can already include
        # them before their audio is ready. Do not mistake our own arrivals for
        # external attendees and then interpret audio readiness as a mass exit.
        unstable_members = any((not s.snapshot["ever_joined"] and s.state != "active") or s.finish_reason for s in members)
        external = max(0, observation.total - len(eligible)) if observation.total is not None and not unstable_members else None
        before = room.leave.candidate
        reason = room.leave.sample(time.time(), external, True)
        if not before and room.leave.candidate:
            self.event(session, "info", "exit_candidate", "Обнаружен массовый уход; начата проверка", room.leave.status)
        elif before and not room.leave.candidate and not reason:
            self.event(session, "info", "exit_cancelled", "Численность восстановилась; выход отменён")
        if reason:
            self.finish_room(session.room_key, reason)

    def finish_room(self, key, reason):
        for session in self.active():
            if session.room_key == key:
                session.finish_reason = reason
                self.storage.update_run(session.id, state="finished")

    async def command(self, key, action):
        session = self.sessions.get(key)
        if not session or session.state in TERMINAL or session.state == "interrupted":
            raise ValueError("Активное подключение не найдено")
        if action == "leave":
            session.finish_reason = "Ручной выход"
            session.finish_state = "manual"
            # Persist immediately; a crash before context.close must not undo manual exit.
            self.storage.update_run(key, state="manual")
            if session.adapter:
                # Closing a context interrupts pending navigation instead of waiting
                # for the full join timeout while a user has requested a leave.
                try:
                    await session.adapter.close()
                except Exception:
                    pass
        elif action == "pause":
            session.paused = not session.paused
            self.storage.update_run(key, paused=session.paused)
            self.event(session, "info", "pause", "Повторение сообщений приостановлено" if session.paused else "Повторение сообщений возобновлено")
        elif action == "diagnostics":
            await self.diagnostics(session)
        elif action in {"show_browser", "hide_browser"}:
            await self.subject_browser(session.snapshot["subject"]["id"], action == "show_browser")
        else:
            raise ValueError("Неизвестная команда")

    async def subject_browser(self, subject_id, visible):
        members = [s for s in self.active() if s.snapshot["subject"]["id"] == subject_id]
        leader = next((s for s in members if getattr(s.adapter, "window", None)), None)
        if not leader:
            raise ValueError("Окно предмета пока недоступно")
        await leader.adapter.set_visible(visible)
        # Persist for every member, including sessions not yet connected.
        for member in members:
            member.snapshot["settings"]["headless"] = not visible
            self.storage.update_snapshot(member.id, member.snapshot)
        self.event(leader, "info", "browser_visibility", "Окно предмета показано" if visible else "Окно предмета скрыто")

    async def diagnostics(self, session, automatic=False):
        if not session.adapter:
            return
        folder = self.storage.directory / "diagnostics" / hashlib.sha256(session.id.encode()).hexdigest()[:16]
        try:
            async with session.lock:
                await session.adapter.diagnostics(folder)
            self.event(session, "info", "diagnostics", "Диагностика сохранена", {"directory": str(folder)})
        except Exception as exc:
            self.event(session, "warning", "diagnostics_error", f"Не удалось сохранить диагностику: {exc}")

    async def start_demo(self, count=2):
        if not 1 <= count <= 15:
            raise ValueError("Выберите от одного до пятнадцати профилей")
        if len(self.active()) + count > self.storage.config.settings.max_connections:
            raise ValueError("Недостаточно свободных подключений; завершите другие сессии")
        key = "demo://sandbox"
        if not any(s.room_key == key for s in self.active()):
            self.demo_rooms[key] = DemoRoom()
            self.rooms.pop(key, None)
        now = datetime.now(SAMARA)
        instance = "demo:" + uuid.uuid4().hex[:10]
        for index in range(count):
            person = Person(id=f"demo_{index+1}", name=f"Демо-профиль {index+1}")
            subject = Subject(id="demo", title="Демонстрационная лекция", url=key, adapter="demo")
            snapshot = self.make_snapshot(person, subject, self.storage.config.settings, now, now + timedelta(hours=1))
            run_id = f"{instance}/{person.id}"
            self.storage.create_run(run_id, person.id, instance, snapshot)
            self.spawn(self.storage.run(run_id))
        self.storage.event("info", "demo_started", f"Локальная демонстрация: {count} профилей. Внешние сайты не открываются")

    def status(self):
        subjects = {}
        for session in self.active():
            subject = session.snapshot["subject"]
            entry = subjects.setdefault(subject["id"], {"subject_id": subject["id"], "title": subject["title"],
                "connections": 0, "window_supported": False, "browser_visible": False, "viewing_as": None})
            entry["connections"] += 1
            if getattr(session.adapter, "window", None):
                entry["window_supported"] = True
                entry["browser_visible"] = session.adapter.browser_visible
                group = session.adapter.group
                entry["viewing_as"] = group.selected.name if group and group.selected else None
        return {"sessions": [s.public() for s in self.sessions.values()], "subject_browsers": list(subjects.values()),
                "rooms": {key: room.leave.status for key, room in self.rooms.items()},
                "automation_enabled": self.storage.config.settings.automation_enabled,
                "config_error": self.storage.config_error,
                "real_bbb_verified": False,
                "upcoming": [{"id": o.id, "subject_id": o.lesson.subject_id, "start": o.start.isoformat(), "end": o.end.isoformat(), "person_ids": o.lesson.person_ids}
                             for o in occurrences(self.storage.config, datetime.now(SAMARA).date()) if o.end.timestamp() > time.time()][:40]}
