from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .rules import Message


@dataclass
class Observation:
    healthy: bool
    listen_only: bool
    total: int | None
    messages: list[Message]
    own_id: str | None = None
    ended: bool = False
    warning: str | None = None
    waiting: bool = False
    waiting_reason: str | None = None
    chat_count: int = 0
    chat_parsed: int = 0
    chat_layout: str | None = None


@dataclass
class DemoRoom:
    external: int = 40
    healthy: bool = True
    ended: bool = False
    members: dict[str, str] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)

    def post(self, author: str, name: str, text: str):
        self.messages.append(Message(uuid.uuid4().hex, author, name, text, time.time()))
        self.messages = self.messages[-2000:]


class DemoAdapter:
    def __init__(self, room: DemoRoom, name: str, key: str):
        self.room, self.name, self.key = room, name, key
        self.cursor = 0

    async def connect(self):
        if not self.room.healthy:
            raise RuntimeError("Демонстрационный обрыв соединения")
        self.room.members[self.key] = self.name
        self.cursor = len(self.room.messages)

    async def observe(self):
        messages = self.room.messages[self.cursor:]
        self.cursor = len(self.room.messages)
        return Observation(self.room.healthy, self.room.healthy, self.room.external + len(self.room.members), messages, self.key, self.room.ended)

    async def send(self, text):
        if not self.room.healthy:
            raise RuntimeError("Демонстрационный обрыв соединения")
        self.room.post(self.key, self.name, text)
        return True

    async def diagnostics(self, directory):
        return None

    async def close(self):
        self.room.members.pop(self.key, None)
