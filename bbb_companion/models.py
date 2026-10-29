from __future__ import annotations

from datetime import date as Date, time as Time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Person(Model):
    id: Identifier
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Имя не может быть пустым")
        return value


class Subject(Model):
    id: Identifier
    title: str = Field(min_length=1, max_length=150)
    url: str = Field(min_length=1, max_length=2048)
    adapter: Literal["bbb", "demo"] = "bbb"

    @model_validator(mode="after")
    def check_url(self):
        parsed = urlsplit(self.url)
        if self.adapter == "demo":
            if parsed.scheme != "demo" or not parsed.netloc:
                raise ValueError("Адрес демонстрации должен иметь вид demo://lecture")
        elif parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Нужна HTTP(S)-ссылка без учётных данных")
        if parsed.fragment:
            raise ValueError("Ссылка комнаты не должна содержать фрагмент #")
        return self


class Move(Model):
    original_date: Date
    date: Date
    start: Time
    end: Time


class Lesson(Model):
    id: Identifier
    subject_id: Identifier
    person_ids: list[Identifier] = Field(min_length=1, max_length=15)
    start: Time
    end: Time
    weekdays: list[int] = Field(default_factory=list)
    interval_weeks: Literal[1, 2] = 1
    anchor_date: Date | None = None
    date: Date | None = None
    valid_from: Date | None = None
    valid_until: Date | None = None
    excluded_dates: list[Date] = Field(default_factory=list)
    moves: list[Move] = Field(default_factory=list)
    enabled: bool = True

    @model_validator(mode="after")
    def check_schedule(self):
        if bool(self.weekdays) == bool(self.date):
            raise ValueError("Укажите либо дни недели, либо дату разового занятия")
        if self.interval_weeks == 2 and (self.date or not self.anchor_date):
            raise ValueError("Для занятия раз в две недели укажите опорную дату активной недели")
        if len(set(self.person_ids)) != len(self.person_ids):
            raise ValueError("Люди в занятии не должны повторяться")
        if any(day not in range(7) for day in self.weekdays) or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Дни недели: уникальные числа от 0 (пн) до 6 (вс)")
        if self.start == self.end or any(move.start == move.end for move in self.moves):
            raise ValueError("Начало и окончание не должны совпадать")
        if self.start.tzinfo or self.end.tzinfo or any(m.start.tzinfo or m.end.tzinfo for m in self.moves):
            raise ValueError("Время указывается локально для Самары, без UTC-смещения")
        if self.valid_from and self.valid_until and self.valid_from > self.valid_until:
            raise ValueError("Конец периода раньше начала")
        if len({m.original_date for m in self.moves}) != len(self.moves):
            raise ValueError("На одну исходную дату допустим один перенос")
        return self


class ChatSettings(Model):
    window_seconds: int = Field(default=60, ge=5, le=600)
    min_authors: int = Field(default=5, ge=2, le=100)
    author_ratio: float = Field(default=.6, gt=0, le=1)
    quiet_seconds: int = Field(default=120, ge=60, le=1800)
    groups: dict[str, list[str]] = Field(default_factory=lambda: {
        "Приветствие": ["Здравствуйте", "Добрый день", "Доброе утро"],
        "Прощание": ["До свидания", "Всего доброго"],
        "Плюс": ["+", "++"],
    })

    @model_validator(mode="after")
    def check_groups(self):
        from .rules import normalize
        seen = set()
        for group, variants in self.groups.items():
            if not group.strip() or not variants:
                raise ValueError("У каждой группы нужны имя и варианты")
            for variant in variants:
                normalized = normalize(variant)
                if not normalized or len(variant) > 300 or normalized in seen:
                    raise ValueError("Варианты должны быть непустыми и не повторяться между группами")
                seen.add(normalized)
        if self.quiet_seconds < self.window_seconds:
            raise ValueError("Пауза между волнами должна быть не меньше окна чата")
        return self


class LeaveSettings(Model):
    window_seconds: int = Field(default=120, ge=10, le=600)
    drop_ratio: float = Field(default=.6, gt=0, lt=1)
    confirm_seconds: int = Field(default=30, ge=1, le=300)
    empty_seconds: int = Field(default=15, ge=1, le=300)


class Settings(Model):
    automation_enabled: bool = False
    timezone: Literal["Europe/Samara"] = "Europe/Samara"
    max_connections: int = Field(default=15, ge=1, le=15)
    early_minutes: int = Field(default=2, ge=0, le=30)
    wait_minutes: int = Field(default=20, ge=1, le=180)
    grace_minutes: int = Field(default=60, ge=0, le=180)
    retry_seconds: int = Field(default=15, ge=2, le=300)
    max_retries: int = Field(default=5, ge=1, le=30)
    log_retention_days: int = Field(default=30, ge=1, le=365)
    headless: bool = True
    dry_run: bool = False
    chat: ChatSettings = Field(default_factory=ChatSettings)
    leave: LeaveSettings = Field(default_factory=LeaveSettings)


class Config(Model):
    version: Literal[2] = 2
    settings: Settings = Field(default_factory=Settings)
    people: list[Person] = Field(default_factory=list, max_length=100)
    subjects: list[Subject] = Field(default_factory=list, max_length=100)
    lessons: list[Lesson] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def upgrade_v1(cls, value):
        if isinstance(value, dict) and value.get("version", 1) == 1:
            value = {**value, "version": 2}
            settings = value.get("settings")
            if isinstance(settings, dict) and settings.get("max_connections") == 5:
                value["settings"] = {**settings, "max_connections": 15}
        return value

    @model_validator(mode="after")
    def references(self):
        for items in (self.people, self.subjects, self.lessons):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Идентификаторы в одном разделе должны быть уникальны")
        people = {p.id for p in self.people}
        subjects = {s.id for s in self.subjects}
        for lesson in self.lessons:
            if lesson.subject_id not in subjects or not set(lesson.person_ids) <= people:
                raise ValueError(f"Занятие {lesson.id}: неизвестный предмет или человек")
        return self
