import asyncio
import ctypes
import json
import os
import time
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from bbb_companion.browser import BrowserAdapter, BrowserPool
from bbb_companion.engine import Engine
from bbb_companion.models import Config, Settings
from bbb_companion.schedule import SAMARA
from bbb_companion.storage import Storage
from test_browser import FIXTURE
from test_legacy_browser import LocalServer


async def until(predicate, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(.1)
    raise AssertionError('Timed out waiting for sessions')


def test_limit_and_legacy_configuration_upgrade(tmp_path):
    assert Settings().max_connections == 15
    old = {'version': 1, 'settings': {'max_connections': 5, 'dry_run': True}}
    upgraded = Config.model_validate(old)
    assert upgraded.version == 2 and upgraded.settings.max_connections == 15
    assert upgraded.settings.dry_run and old['settings']['max_connections'] == 5
    assert Config.model_validate({'version': 2, 'settings': {'max_connections': 5}}).settings.max_connections == 5
    assert Config.model_validate({'version': 1, 'settings': {'max_connections': 3}}).settings.max_connections == 3
    with pytest.raises(ValidationError): Settings(max_connections=16)
    data = dict(people=[dict(id=f'p{i}', name=f'Person {i}') for i in range(16)],
        subjects=[dict(id='math', title='Math', url='demo://math', adapter='demo')],
        lessons=[dict(id='math', subject_id='math', person_ids=[f'p{i}' for i in range(15)], weekdays=[0], start='10:00', end='11:00')])
    assert len(Config.model_validate(data).lessons[0].person_ids) == 15
    data['lessons'][0]['person_ids'].append('p15')
    with pytest.raises(ValidationError): Config.model_validate(data)
    (tmp_path/'config.json').write_text(json.dumps(old), encoding='utf-8')
    store = Storage(tmp_path)
    try:
        assert store.config.settings.max_connections == 15
        assert json.loads((tmp_path/'config.json').read_text(encoding='utf-8'))['version'] == 2
        assert json.loads((tmp_path/'config.backup.json').read_text(encoding='utf-8')) == old
    finally:
        store.close()


def test_fifteen_real_connections_share_two_subject_browsers_and_keep_isolation(tmp_path):
    async def scenario(url):
        store = Storage(tmp_path)
        now = datetime.now(SAMARA)
        start, end = now - timedelta(minutes=1), now + timedelta(minutes=30)
        store.save_config(Config.model_validate(dict(settings=dict(automation_enabled=True),
            people=[dict(id=f'p{i}', name=f'Person {i}') for i in range(16)],
            subjects=[dict(id=s, title=s, url=url) for s in ('math', 'physics')],
            lessons=[dict(id=s, subject_id=s, person_ids=[f'p{i}' for i in indices], date=start.date(),
                start=start.time().replace(tzinfo=None), end=end.time().replace(tzinfo=None))
                for s, indices in [('math', range(12)), ('physics', range(12,16))]])))
        engine = Engine(store)
        await engine.start()
        try:
            await until(lambda: len([s for s in engine.active() if s.healthy]) == 15)
            assert len(engine.active()) == 15 and len(engine.pool.browsers) == 2
            assert sorted(len(b.contexts) for b in engine.pool.browsers.values()) == [3,12]
            assert any(e['kind'] == 'schedule_conflict' for e in store.events(limit=1000))
            sessions = engine.active()
            for i, session in enumerate(sessions):
                adapter = session.adapter
                assert await adapter.page.locator('input').input_value() == session.snapshot['person']['name']
                await adapter.page.evaluate('(i) => localStorage.setItem("profile",String(i))', i)
                await adapter.context.add_cookies([dict(name='profile',value=str(i),url=url)])
            for i, session in enumerate(sessions):
                assert await session.adapter.page.evaluate('localStorage.profile') == str(i)
                assert (await session.adapter.context.cookies())[0]['value'] == str(i)
            math = engine.pool.groups['math']
            physics = engine.pool.groups['physics']
            if os.name == 'nt':
                user32 = ctypes.WinDLL('user32')
                from ctypes import wintypes
                user32.IsWindowVisible.argtypes = [wintypes.HWND]
                def shown(group):
                    return [a for a in group.members if any(user32.IsWindowVisible(h) for h in a.window.handles())]
                await engine.subject_browser('math', True)
                assert len(shown(math)) == 1 and not shown(physics)
                assert all(not s.snapshot['settings']['headless'] for s in sessions if s.snapshot['subject']['id']=='math')
                for a in math.members:
                    await a.page.evaluate("document.body.append(document.createElement('audio'))")
                await asyncio.sleep(.1)
                assert sum([not await a.page.locator('audio').evaluate('(a)=>a.muted') for a in math.members]) == 1
                await engine.subject_browser('physics', True)
                await engine.subject_browser('math', False)
                assert not shown(math) and len(shown(physics)) == 1
                await engine.subject_browser('math', True)
            selected = math.selected
            original_browser = math.browser
            leaving = next(s for s in sessions if s.adapter is selected)
            await engine.command(leaving.id, 'leave')
            await until(lambda: leaving.state=='manual' and len([s for s in engine.active() if s.healthy])==15)
            assert engine.pool.groups['math'].browser is original_browser
            assert len(math.members)==11 and len(physics.members)==4
            if os.name == 'nt': assert len(shown(math))==1 and math.selected is not selected
            # Closing all math contexts leaves the other subject intact.
            for s in list(engine.active()):
                if s.snapshot['subject']['id']=='math': await engine.command(s.id,'leave')
            await until(lambda: 'math' not in engine.pool.groups)
            assert physics.browser.is_connected() and len(physics.members)==4
            assert all(s.healthy for s in engine.active() if s.snapshot['subject']['id']=='physics')
        finally:
            await engine.stop()
            assert not engine.pool.browsers
            store.close()
    with LocalServer(FIXTURE) as server:
        asyncio.run(scenario(server.url))


def test_replacing_crashed_subject_browser_does_not_close_its_replacement():
    async def scenario(url):
        pool = BrowserPool()
        old = BrowserAdapter(pool,url,'Old',True,'math')
        other = BrowserAdapter(pool,url,'Other',True,'physics')
        new = BrowserAdapter(pool,url,'New',True,'math')
        try:
            await old.connect()
            await other.connect()
            await old.context.browser.close()
            await new.connect()
            await old.close()
            assert pool.groups['math'].browser is new.context.browser
            assert (await new.observe()).healthy and (await other.observe()).healthy
        finally:
            await old.close()
            await new.close()
            await other.close()
            await pool.close()
    with LocalServer(FIXTURE) as server:
        asyncio.run(scenario(server.url))
