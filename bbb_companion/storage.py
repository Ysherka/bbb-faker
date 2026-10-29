from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Config

TERMINAL = {"finished", "manual", "failed", "expired"}


def atomic_write(path: Path, text: str):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Storage:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.config_path = directory / "config.json"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / "state.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, person_id TEXT NOT NULL, occurrence_id TEXT NOT NULL,
                state TEXT NOT NULL, snapshot TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sends (
                run_id TEXT NOT NULL, wave_id TEXT NOT NULL, state TEXT NOT NULL, text TEXT NOT NULL,
                PRIMARY KEY (run_id, wave_id)
            );
            CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, level TEXT NOT NULL,
                kind TEXT NOT NULL, message TEXT NOT NULL, run_id TEXT, person_id TEXT,
                subject_id TEXT, details TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_filters ON events(person_id, subject_id, id);
        """)
        self.db.commit()
        self.config_error: str | None = None
        try:
            if self.config_path.exists():
                raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
                self.config = Config.model_validate(raw)
                if raw.get("version", 1) != self.config.version:
                    self.save_config(self.config)
                    self.event("info", "config_upgraded", "Конфигурация обновлена до версии 2; предыдущая копия сохранена в config.backup.json")
            else:
                self.config = Config()
                atomic_write(self.config_path, self.config.model_dump_json(indent=2))
        except Exception as exc:
            self.config = Config()
            self.config_error = f"Ошибка config.json: {exc}"
            self.event("error", "config_error", self.config_error)

    def save_config(self, config: Config):
        with self.lock:
            if self.config_path.exists():
                atomic_write(self.directory / "config.backup.json", self.config_path.read_text(encoding="utf-8-sig"))
            atomic_write(self.config_path, config.model_dump_json(indent=2))
            self.config = config
            self.config_error = None
        self.event("info", "config_saved", "Настройки сохранены; активные подключения продолжаются без изменений")

    def event(self, level: str, kind: str, message: str, *, run_id=None, person_id=None, subject_id=None, details=None):
        at = datetime.now(timezone.utc).isoformat()
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO events(at,level,kind,message,run_id,person_id,subject_id,details) VALUES(?,?,?,?,?,?,?,?)",
                (at, level, kind, message, run_id, person_id, subject_id, json.dumps(details or {}, ensure_ascii=False)),
            )
            return cursor.lastrowid

    def events(self, *, after: int = 0, before: int | None = None, limit: int = 200, person_id=None, subject_id=None, level=None):
        clauses, params = ["id > ?"], [after]
        for key, value in (("person_id", person_id), ("subject_id", subject_id), ("level", level)):
            if value:
                clauses.append(f"{key} = ?")
                params.append(value)
        if before is not None:
            clauses.append("id < ?")
            params.append(before)
        # Live tail is ascending; initial/history page returns the latest matching events.
        order = "ASC" if after else "DESC"
        with self.lock:
            rows = self.db.execute(f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id {order} LIMIT ?", [*params, limit]).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["details"] = json.loads(item["details"])
        return sorted(result, key=lambda item: item["id"])

    def run(self, key: str):
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (key,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        return result

    def runs(self, limit=200):
        with self.lock:
            rows = self.db.execute("SELECT id FROM runs ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [self.run(row["id"]) for row in rows]

    def unfinished(self):
        with self.lock:
            rows = self.db.execute("SELECT id FROM runs WHERE state NOT IN ('finished','manual','failed','expired') ORDER BY updated").fetchall()
        return [self.run(row["id"]) for row in rows]

    def create_run(self, key, person_id, occurrence_id, snapshot):
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO runs(id,person_id,occurrence_id,state,snapshot,updated) VALUES(?,?,?,'waiting',?,?)",
                (key, person_id, occurrence_id, json.dumps(snapshot, ensure_ascii=False), datetime.now().timestamp()),
            )
        return cursor.rowcount > 0

    def update_run(self, key, state=None, paused=None):
        with self.lock, self.db:
            if state is not None:
                self.db.execute("UPDATE runs SET state=?,updated=? WHERE id=?", (state, datetime.now().timestamp(), key))
            if paused is not None:
                self.db.execute("UPDATE runs SET paused=? WHERE id=?", (int(paused), key))

    def update_snapshot(self, key, snapshot):
        with self.lock, self.db:
            self.db.execute("UPDATE runs SET snapshot=? WHERE id=?", (json.dumps(snapshot, ensure_ascii=False), key))

    def claim_send(self, run_id, wave_id, text):
        with self.lock, self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO sends VALUES(?,?,'claimed',?)", (run_id, wave_id, text))
        return cursor.rowcount > 0

    def finish_send(self, run_id, wave_id, state):
        with self.lock, self.db:
            self.db.execute("UPDATE sends SET state=? WHERE run_id=? AND wave_id=?", (state, run_id, wave_id))

    def room(self, key):
        with self.lock:
            row = self.db.execute("SELECT payload FROM rooms WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_room(self, key, payload):
        with self.lock, self.db:
            self.db.execute("INSERT INTO rooms VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (key, json.dumps(payload, ensure_ascii=False)))

    def prune(self, days):
        cutoff = datetime.fromtimestamp(datetime.now().timestamp() - days * 86400, timezone.utc).isoformat()
        with self.lock, self.db:
            self.db.execute("DELETE FROM events WHERE at < ?", (cutoff,))

    def close(self):
        self.db.close()
