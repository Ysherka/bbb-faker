from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Config, Lesson

SAMARA = ZoneInfo("Europe/Samara")


@dataclass(frozen=True)
class Occurrence:
    id: str
    lesson: Lesson
    start: datetime
    end: datetime


def occurs(lesson: Lesson, day: date) -> bool:
    if not lesson.enabled:
        return False
    if lesson.valid_from and day < lesson.valid_from or lesson.valid_until and day > lesson.valid_until:
        return False
    return lesson.date == day if lesson.date else day.weekday() in lesson.weekdays


def occurrences(config: Config, start_day: date, days: int = 8) -> list[Occurrence]:
    end_day = start_day + timedelta(days=days)
    result = []
    for lesson in config.lessons:
        sources = {start_day + timedelta(days=i) for i in range(-1, days)}
        sources.update(m.original_date for m in lesson.moves if start_day - timedelta(days=1) <= m.date < end_day)
        moves = {m.original_date: m for m in lesson.moves}
        for original in sources:
            if not occurs(lesson, original) or original in lesson.excluded_dates:
                continue
            move = moves.get(original)
            day = move.date if move else original
            begin = move.start if move else lesson.start
            finish = move.end if move else lesson.end
            if not start_day - timedelta(days=1) <= day < end_day:
                continue
            start = datetime.combine(day, begin, SAMARA)
            end = datetime.combine(day, finish, SAMARA)
            if end <= start:
                end += timedelta(days=1)
            result.append(Occurrence(f"{lesson.id}:{original.isoformat()}", lesson, start, end))
    return sorted(result, key=lambda item: (item.start, item.id))


def due(config: Config, now: datetime) -> list[Occurrence]:
    now = now.astimezone(SAMARA)
    early = timedelta(minutes=config.settings.early_minutes)
    return [item for item in occurrences(config, now.date(), 2) if item.start - early <= now < item.end]
