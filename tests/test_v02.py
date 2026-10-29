import asyncio
import ctypes
import os
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bbb_companion.app import create_app
from bbb_companion.browser import BrowserAdapter, BrowserPool
from bbb_companion.models import Config
from bbb_companion.schedule import occurs, occurrences
from bbb_companion.transfer import Bundle, export_bundle, import_bundle
from test_legacy_browser import LocalServer, LEGACY_FIXTURE


def config(**changes):
    lesson = dict(id='math', subject_id='math', person_ids=['p'], weekdays=[0, 2],
                  start='10:00', end='11:30', interval_weeks=2, anchor_date='2026-12-30')
    lesson.update(changes)
    return Config.model_validate(dict(people=[dict(id='p', name='Иван')],
        subjects=[dict(id='math', title='Математика', url='https://example.test/b/math')], lessons=[lesson]))


def test_biweekly_anchor_spans_years_and_handles_moves_exclusions():
    cfg = config()
    lesson = cfg.lessons[0]
    assert occurs(lesson, date(2026, 12, 28))  # Monday of anchor week
    assert occurs(lesson, date(2026, 12, 30))
    assert not occurs(lesson, date(2027, 1, 4))
    assert occurs(lesson, date(2027, 1, 11))
    assert occurs(lesson, date(2026, 12, 14))  # Weeks before the anchor too
    cfg = config(excluded_dates=['2026-12-28'], moves=[dict(original_date='2026-12-30',
        date='2027-01-05', start='12:00', end='13:00')])
    entries = occurrences(cfg, date(2027, 1, 4), 7)
    assert len(entries) == 1 and entries[0].id == 'math:2026-12-30'
    assert entries[0].start.date() == date(2027, 1, 5)
    assert not occurrences(cfg, date(2026, 12, 28), 7)
    with pytest.raises(ValidationError): config(anchor_date=None)
    with pytest.raises(ValidationError): config(date='2026-12-30', weekdays=[])


def test_transfer_conflicts_remap_references_and_keep_settings():
    cfg = config()
    cfg.settings.dry_run = True
    bundle = export_bundle(cfg, {'lessons'})
    assert bundle.people and bundle.subjects  # Required dependencies included
    assert 'settings' not in bundle.model_dump()
    identical, counts = import_bundle(cfg, bundle, 'merge')
    assert identical == cfg and counts['lessons']['reused'] == 1
    incoming = bundle.model_copy(deep=True)
    incoming.people[0].name = 'Другой человек'
    incoming.subjects[0].title = 'Другой предмет'
    merged, counts = import_bundle(cfg, incoming, 'merge')
    assert len(merged.people) == len(merged.subjects) == len(merged.lessons) == 2
    assert merged.lessons[1].person_ids == [merged.people[1].id]
    assert merged.lessons[1].subject_id == merged.subjects[1].id
    assert counts['people']['renamed'] == counts['lessons']['renamed'] == 1
    repeated, counts = import_bundle(merged, incoming, 'merge')
    assert repeated == merged and counts['lessons']['reused'] == 1
    replaced, _ = import_bundle(cfg, Bundle(people=incoming.people), 'replace')
    assert len(replaced.people) == 1 and not replaced.lessons and not replaced.subjects
    assert replaced.settings == cfg.settings
    with pytest.raises(ValidationError): Bundle(lessons=cfg.lessons)
    # Different source profiles with identical names remain separate people.
    duplicate_names = Bundle.model_validate(dict(people=[dict(id='p', name='Другой'), dict(id='p2', name='Другой')]))
    local = Config.model_validate(dict(people=[dict(id='p', name='Иван'), dict(id='p2', name='Пётр')]))
    distinct, _ = import_bundle(local, duplicate_names, 'merge')
    assert len(distinct.people) == 4
    repeated, _ = import_bundle(distinct, duplicate_names, 'merge')
    assert repeated == distinct


def test_week_and_import_preview_are_validated_and_atomic(tmp_path):
    app = create_app(tmp_path, run_engine=False)
    app.state.storage.save_config(config())
    with TestClient(app) as client:
        headers = {'X-App-Token': app.state.token}
        week = client.get('/api/week?start=2026-12-30').json()
        assert week['start'] == '2026-12-28' and len(week['days']) == 7
        assert len(week['lessons']) == 2
        assert not client.get('/api/week?start=2027-01-04').json()['lessons']
        body = dict(bundle=client.get('/api/export/data?sections=lessons').json(), mode='replace')
        before = client.get('/api/config').json()
        preview = client.post('/api/import/preview', json=body, headers=headers).json()
        assert client.get('/api/config').json() == before
        assert client.post('/api/import/data', json=body, headers=headers).status_code == 409
        body['revision'] = preview['revision']
        changed = dict(before, settings={**before['settings'], 'dry_run': True})
        client.put('/api/config', json=changed, headers=headers).raise_for_status()
        assert client.post('/api/import/data', json=body, headers=headers).status_code == 409
        body['revision'] = client.post('/api/import/preview', json=body, headers=headers).json()['revision']
        response = client.post('/api/import/data', json=body, headers=headers)
        assert response.status_code == 200 and response.json()['settings']['dry_run']
        assert not response.json()['settings']['automation_enabled']
        assert client.get('/api/export/data?sections=unknown').status_code == 400
        assert client.post('/api/import/data', json=body).status_code == 403


@pytest.mark.skipif(os.name != 'nt', reason='Windows window visibility integration')
def test_independent_native_windows_toggle_without_rejoining():
    async def scenario(url):
        pool = BrowserPool()
        first = BrowserAdapter(pool, url, 'Иван', True, 'math')
        second = BrowserAdapter(pool, url, 'Пётр', True, 'physics')
        try:
            await first.connect()
            await second.connect()
            assert first.context.browser is not second.context.browser
            user32 = ctypes.WinDLL('user32')
            from ctypes import wintypes
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            def visible(adapter):
                return any(user32.IsWindowVisible(h) for h in adapter.window.handles())
            assert not visible(first) and not visible(second)
            page = first.page
            await first.page.evaluate("window.sessionMarker='same-page'")
            await first.page.evaluate("document.body.append(document.createElement('audio'))")
            await asyncio.sleep(.05)
            assert await page.locator('audio').evaluate('(audio) => audio.muted')
            await first.set_visible(True)
            assert visible(first) and not visible(second)
            assert not await page.locator('audio').evaluate('(audio) => audio.muted')
            await first.set_visible(False)
            assert not visible(first) and not visible(second)
            assert await page.locator('audio').evaluate('(audio) => audio.muted')
            assert first.page is page and await page.evaluate('window.sessionMarker') == 'same-page'
            assert (await first.observe()).listen_only
            await first.close()
            assert (await second.observe()).healthy
        finally:
            await first.close()
            await second.close()
            await pool.close()
    with LocalServer(LEGACY_FIXTURE) as server:
        asyncio.run(scenario(server.url))
