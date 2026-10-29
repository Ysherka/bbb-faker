from __future__ import annotations

import hashlib
import unicodedata
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ChatSettings, LeaveSettings


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split()).rstrip(" .,!?:;…")


def identical_key(text: str) -> str:
    # Punctuation carries meaning in answers (e.g. "да" versus "да?").
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


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
    identical: bool = False


class ChatDetector:
    def __init__(self, settings: ChatSettings, saved: dict | None = None):
        self.settings = settings
        self.lookup = {normalize(v): group for group, variants in settings.groups.items() for v in variants}
        self.events: deque[Message] = deque()
        self.seen: set[str] = set()
        self.seen_order: deque[str] = deque()
        self.waves = (saved or {}).get("waves", {})
        self.identical_waves = (saved or {}).get("identical_waves", {})

    def dump(self) -> dict:
        return {"waves": self.waves, "identical_waves": self.identical_waves}

    def identical(self, event: Message) -> str | None:
        if self.settings.repeat_identical and len(event.text) <= 300 and normalize(event.text) not in self.lookup:
            return identical_key(event.text) or None
        return None

    def buckets(self, events):
        known, identical = defaultdict(list), defaultdict(list)
        for event in events:
            group = self.lookup.get(normalize(event.text))
            if group:
                known[group].append(event)
            else:
                key = self.identical(event)
                if key:
                    identical[key].append(event)
        return known, identical

    def progress(self, now: float) -> dict:
        events = [event for event in self.events if event.at >= now - self.settings.window_seconds]
        total = len({event.author for event in events})
        known, identical = self.buckets(events)
        groups = {}
        for group in self.settings.groups:
            authors = {event.author for event in known[group]}
            groups[group] = {"authors": len(authors), "total": total,
                             "ratio": len(authors) / total if total else 0,
                             "already_answered": bool(self.waves.get(group, {}).get("fired"))}
        for key, matching in identical.items():
            authors = {event.author for event in matching}
            groups["identical:" + hashlib.sha256(key.encode()).hexdigest()] = {
                "label": f"Одинаковый ответ «{key}»", "authors": len(authors), "total": total,
                "ratio": len(authors) / total if total else 0,
                "already_answered": bool(self.identical_waves.get(key, {}).get("fired"))}
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
        self.identical_waves = {key: wave for key, wave in self.identical_waves.items()
                                if now - wave["last"] < self.settings.quiet_seconds}
        group = self.lookup.get(normalize(message.text))
        identical = self.identical(message)
        collection, key = (self.waves, group) if group else (self.identical_waves, identical)
        if key:
            wave = collection.get(key)
            if not wave or now - wave["last"] >= self.settings.quiet_seconds:
                wave = {"id": uuid.uuid4().hex, "last": now, "fired": False, "start": now}
                collection[key] = wave
            wave["last"] = now
        self.events.append(message)
        total = len({event.author for event in self.events})
        reactions = []
        known, identical_matches = self.buckets(self.events)
        candidates = [(name, wave, False) for name, wave in self.waves.items()]
        candidates += [(name, wave, True) for name, wave in self.identical_waves.items()]
        for name, wave, dynamic in candidates:
            if wave["fired"] or now - wave["last"] >= self.settings.quiet_seconds:
                continue
            matching = [event for event in (identical_matches if dynamic else known).get(name, []) if event.at >= wave["start"]]
            # One most recent vote per author; repeated messages cannot skew the selected text.
            votes = {event.author: event.text for event in matching}
            if len(votes) >= self.settings.min_authors and len(votes) / total >= self.settings.author_ratio:
                text = Counter(votes.values()).most_common(1)[0][0]
                wave["fired"] = True
                reactions.append(Reaction(wave["id"], "Одинаковый ответ" if dynamic else name, text, len(votes), total, now, identical=dynamic))
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
