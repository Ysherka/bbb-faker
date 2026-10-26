from datetime import datetime

import pytest
from pydantic import ValidationError

from bbb_companion.models import Config
from bbb_companion.schedule import SAMARA, due, occurrences
from bbb_companion.storage import Storage


def sample(**changes):
    lesson = {"id":"math", "subject_id":"math", "person_ids":["ivan"], "start":"10:00", "end":"11:30", "weekdays":[0]}
    lesson.update(changes)
    return Config.model_validate({"people":[{"id":"ivan", "name":"Иван"}], "subjects":[{"id":"math", "title":"Математика", "url":"https://example.test/b/math"}], "lessons":[lesson]})


def test_mid_lesson_early_entry_and_timezone():
    config = sample()
    assert due(config, datetime.fromisoformat("2026-10-19T06:30:00+00:00"))
    assert due(config, datetime(2026,10,19,9,58,tzinfo=SAMARA))
    assert not due(config, datetime(2026,10,19,9,57,tzinfo=SAMARA))
    assert not due(config, datetime(2026,10,19,11,30,tzinfo=SAMARA))


def test_cancel_move_and_overnight():
    config = sample(excluded_dates=["2026-10-19"])
    assert not due(config,datetime(2026,10,19,10,30,tzinfo=SAMARA))
    config = sample(moves=[{"original_date":"2026-10-19","date":"2026-10-21","start":"12:00","end":"13:30"}])
    assert not due(config,datetime(2026,10,19,10,30,tzinfo=SAMARA))
    assert due(config,datetime(2026,10,21,12,30,tzinfo=SAMARA))[0].id == "math:2026-10-19"
    config = sample(start="23:00",end="01:00")
    assert due(config,datetime(2026,10,20,0,30,tzinfo=SAMARA))
    assert len(occurrences(config,datetime(2026,10,19).date(),8)) == 2


def test_invalid_references_and_ambiguous_groups_rejected():
    with pytest.raises(ValidationError): sample(person_ids=["missing"])
    with pytest.raises(ValidationError): sample(weekdays=[])
    with pytest.raises(ValidationError):
        Config.model_validate({"settings":{"chat":{"groups":{"a":["Здравствуйте"],"b":["здравствуйте!"]}}}})


def test_persistence_prevents_duplicate_runs_and_sends(tmp_path):
    store=Storage(tmp_path)
    cfg=sample()
    store.save_config(cfg)
    assert store.create_run("run/ivan","ivan","run",{"value":1})
    assert not store.create_run("run/ivan","ivan","run",{"value":2})
    assert store.claim_send("run/ivan","wave","+")
    store.update_run("run/ivan",state="manual",paused=True)
    store.save_room("room",{"waves":{"plus":{"id":"wave","fired":True}}})
    store.close()
    restored=Storage(tmp_path)
    assert restored.config == cfg
    assert restored.run("run/ivan")["state"] == "manual"
    assert restored.run("run/ivan")["paused"] == 1
    assert not restored.claim_send("run/ivan","wave","+")
    assert not restored.unfinished()
    assert restored.room("room")["waves"]["plus"]["fired"]
    restored.close()


def test_broken_config_is_preserved_and_blocks_automation(tmp_path):
    (tmp_path/"config.json").write_text('{broken',encoding="utf-8")
    store=Storage(tmp_path)
    assert store.config_error and not store.config.settings.automation_enabled
    assert (tmp_path/"config.json").read_text() == '{broken'
    store.save_config(Config())
    assert store.config_error is None
    assert (tmp_path/"config.backup.json").read_text() == '{broken'
    store.close()
