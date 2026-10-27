"""Portable directory/schedule bundles, independent of machine settings and history."""
from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from pydantic import Field, model_validator

from .models import Config, Lesson, Model, Person, Subject


class Bundle(Model):
    format: Literal["bbb-companion-transfer"] = "bbb-companion-transfer"
    version: Literal[1] = 1
    people: list[Person] = Field(default_factory=list, max_length=100)
    subjects: list[Subject] = Field(default_factory=list, max_length=100)
    lessons: list[Lesson] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def references(self):
        Config(people=self.people, subjects=self.subjects, lessons=self.lessons)
        return self


def revision(config: Config) -> str:
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


def export_bundle(config: Config, sections: set[str]) -> Bundle:
    lessons = config.lessons if "lessons" in sections else []
    people_ids = {p for lesson in lessons for p in lesson.person_ids}
    subject_ids = {lesson.subject_id for lesson in lessons}
    return Bundle(
        people=[p for p in config.people if "people" in sections or p.id in people_ids],
        subjects=[s for s in config.subjects if "subjects" in sections or s.id in subject_ids],
        lessons=lessons,
    )


def import_bundle(config: Config, bundle: Bundle, mode: Literal["merge", "replace"]):
    next_data = config.model_dump()
    counts = {}
    maps = {}
    for section in ("people", "subjects", "lessons"):
        source = getattr(bundle, section)
        target = [] if mode == "replace" else list(getattr(config, section))
        used = {item.id: item for item in target}
        source_ids = {item.id for item in source}
        # Reserve incoming IDs too, so a generated ID can never alias another item.
        reserved = set(used) | source_ids
        mapping = {}
        added = reused = renamed = 0
        for original in source:
            item = original.model_copy(deep=True)
            if section == "lessons":
                item.subject_id = maps["subjects"][item.subject_id]
                item.person_ids = [maps["people"][p] for p in item.person_ids]
            existing = used.get(item.id)
            if existing and existing != item:
                # Reimporting the same colliding bundle should reuse its prior
                # renamed copy instead of accumulating another set of records.
                equivalent = next((candidate for candidate in target
                    if candidate.id not in source_ids and candidate.id not in mapping.values()
                    and candidate.model_dump(exclude={"id"}) == item.model_dump(exclude={"id"})), None)
                if equivalent:
                    mapping[original.id] = equivalent.id
                    reused += 1
                    continue
            if existing == item:
                mapping[original.id] = item.id
                reused += 1
                continue
            if existing:
                while True:
                    identifier = "import_" + uuid.uuid4().hex[:12]
                    if identifier not in reserved:
                        break
                item.id = identifier
                reserved.add(identifier)
                renamed += 1
            mapping[original.id] = item.id
            target.append(item)
            used[item.id] = item
            added += 1
        maps[section] = mapping
        next_data[section] = target
        counts[section] = {"incoming": len(source), "added": added, "reused": reused,
                           "renamed": renamed, "total": len(target)}
    # Revalidate limits and references for the complete result before any write.
    result = Config.model_validate(next_data)
    return result, counts
