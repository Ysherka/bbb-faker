from __future__ import annotations

import unicodedata
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ChatSettings, LeaveSettings


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split()).rstrip(" .,!?:;…")


@dataclass(frozen=True)
class Message:
    id: str
    author: str
    name: str
    text: str
    at: float


@dataclass(frozen=True)
class Reaction:
    id: str
    group: str
    text: str
    authors: int
    total: int
    at: float


class ChatDetector:
    def __init__(self, settings: ChatSettings, saved: dict | None = None):
        self.settings = settings
        self.lookup = {normalize(v): group for group, variants in settings.groups.items() for v in variants}
        self.events: deque[Message] = deque()
        self.seen: set[str] = set()
        self.seen_order: deque[str] = deque()
        self.waves = (saved or {}).get("waves", {})

    def dump(self) -> dict:
        return {"waves": self.waves}

    def progress(self, now: float) -> dict:
        events = [event for event in self.events if event.at >= now - self.settings.window_seconds]
        total = len({event.author for event in events})
        groups = {}
        for group in self.settings.groups:
            authors = {event.author for event in events if self.lookup.get(normalize(event.text)) == group}
            groups[group] = {"authors": len(authors), "total": total,
                             "ratio": len(authors) / total if total else 0,
                             "already_answered": bool(self.waves.get(group, {}).get("fired"))}
        return groups

    def add(self, message: Message, own_ids: set[str], own_names: set[str]) -> list[Reaction]:
        if message.id in self.seen:
            return []
        self.seen.add(message.id)
        self.seen_order.append(message.id)
        if len(self.seen_order) > 10000:
            self.seen.discard(self.seen_order.popleft())
        if message.author in own_ids or normalize(message.name) in own_names:
            return []
        now = message.at
        while self.events and self.events[0].at < now - self.settings.window_seconds:
            self.events.popleft()
        group = self.lookup.get(normalize(message.text))
        if group:
            wave = self.waves.get(group)
            if not wave or now - wave["last"] >= self.settings.quiet_seconds:
                wave = {"id": uuid.uuid4().hex, "last": now, "fired": False, "start": now}
                self.waves[group] = wave
            wave["last"] = now
        self.events.append(message)
        total = len({event.author for event in self.events})
        reactions = []
        for name, wave in self.waves.items():
            if wave["fired"] or now - wave["last"] >= self.settings.quiet_seconds:
                continue
            matching = [event for event in self.events if event.at >= wave["start"] and self.lookup.get(normalize(event.text)) == name]
            # One most recent vote per author; repeated messages cannot skew the selected text.
            votes = {event.author: event.text for event in matching}
            if len(votes) >= self.settings.min_authors and len(votes) / total >= self.settings.author_ratio:
                text = Counter(votes.values()).most_common(1)[0][0]
                wave["fired"] = True
                reactions.append(Reaction(wave["id"], name, text, len(votes), total, now))
        return reactions


class LeaveDetector:
    def __init__(self, settings: LeaveSettings):
        self.settings = settings
        self.samples: deque[tuple[float, int]] = deque()
        self.candidate: tuple[float, int] | None = None
        self.empty_since: float | None = None
        self.had_external = False
        self.last_at: float | None = None
        self.status: dict = {}

    def reset_observation(self):
        self.samples.clear()
        self.candidate = None
        self.empty_since = None
        self.last_at = None
        self.status = {}

    def sample(self, now: float, count: int | None, healthy: bool) -> str | None:
        if not healthy or count is None or count < 0:
            self.reset_observation()
            return None
        if self.last_at is not None and now - self.last_at > 10:
            self.reset_observation()
        self.last_at = now
        if count > 0:
            self.had_external = True
            self.empty_since = None
        elif self.had_external:
            if self.empty_since is None:
                self.empty_since = now
            if now - self.empty_since >= self.settings.empty_seconds:
                return "В комнате не осталось внешних участников"
        while self.samples and self.samples[0][0] < now - self.settings.window_seconds:
            self.samples.popleft()
        baseline = max((value for _, value in self.samples), default=count)
        threshold = 1 - self.settings.drop_ratio
        if self.candidate:
            since, original = self.candidate
            if count > original * threshold + 1e-8:
                self.candidate = None
            elif now - since >= self.settings.confirm_seconds:
                return f"Массовый уход: {original} → {count}, подтверждение {self.settings.confirm_seconds} с"
        if self.candidate is None and baseline > 0 and count <= baseline * threshold + 1e-8:
            self.candidate = (now, baseline)
        self.samples.append((now, count))
        self.status = {"external": count, "baseline": baseline, "exit_in": None}
        if self.candidate:
            self.status.update(baseline=self.candidate[1], exit_in=max(0, self.settings.confirm_seconds - (now - self.candidate[0])))
        return None
