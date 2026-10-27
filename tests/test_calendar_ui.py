import asyncio
import json
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn

from bbb_companion.app import create_app
from bbb_companion.models import Config
from bbb_companion.schedule import SAMARA


def test_week_editor_transfer_and_autostart_controls(tmp_path):
    class Desktop:
        enabled = False
        def status(self): return dict(supported=True, autostart=self.enabled, autostart_current_path=True)
        def set_autostart(self, value):
            self.enabled = value
            return self.status()
    desktop = Desktop()
    app = create_app(tmp_path, run_engine=False, desktop=desktop)
    today = datetime.now(SAMARA).date()
    monday = (today - timedelta(days=today.weekday())).isoformat()
    app.state.storage.save_config(Config.model_validate(dict(
        settings=dict(dry_run=True), people=[dict(id='p1', name='Иван'), dict(id='p2', name='Пётр')],
        subjects=[dict(id='math', title='Математика', url='demo://math', adapter='demo')],
        lessons=[dict(id='math', subject_id='math', person_ids=['p1'], weekdays=[0,2],
            start='10:00', end='11:30', interval_weeks=2, anchor_date=monday)])))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic()+10
    while not server.started and time.monotonic()<deadline: time.sleep(.05)
    assert server.started

    async def scenario():
        from playwright.async_api import async_playwright, expect
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport=dict(width=1440, height=1000))
            page.set_default_timeout(7000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            try:
                await page.goto(f'http://127.0.0.1:{port}/#schedule')
                await expect(page.locator('.week-lesson')).to_have_count(2)
                await page.locator('#week-next').click()
                await expect(page.locator('.week-lesson')).to_have_count(0)
                await page.locator('#week-prev').click()
                await expect(page.locator('.week-lesson')).to_have_count(2)
                await page.locator('.week-lesson').first.click()
                await expect(page.locator('select[name=repeat]')).to_have_value('biweekly')
                await page.locator('input[name=start]').fill('09:15')
                await page.locator('input[name=end]').fill('10:45')
                await page.locator('#editor-form button[type=submit]').click()
                await expect(page.locator('.week-time').first).to_have_text('09:15–10:45')
                await page.locator('#schedule-person').select_option('p2')
                await expect(page.locator('.week-lesson')).to_have_count(0)
                await page.locator(f'.cell-add[data-add-day="{monday}"][data-time="12:00"]').click()
                await expect(page.locator('input[name=date]')).to_have_value(monday)
                await expect(page.locator('input[name=person][value=p2]')).to_be_checked()
                await page.locator('select[name=repeat]').select_option('biweekly')
                await expect(page.locator('input[name=anchor_date]')).to_have_value(monday)
                await page.locator('#editor-form button[type=submit]').click()
                await expect(page.locator('.week-lesson')).to_have_count(1)
                await page.locator('#schedule-person').select_option('')
                await expect(page.locator('.week-lesson')).to_have_count(3)
                await page.screenshot(path='artifacts/calendar-desktop.png', full_page=True)
                await page.set_viewport_size(dict(width=390, height=844))
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.screenshot(path='artifacts/calendar-mobile.png', full_page=True)
                await page.set_viewport_size(dict(width=1440, height=1000))
                await page.locator('#transfer-open').click()
                async with page.expect_download() as downloading:
                    await page.locator('#data-export').click()
                downloaded = await downloading.value
                bundle_path = tmp_path / 'transfer.json'
                await downloaded.save_as(bundle_path)
                bundle = json.loads(bundle_path.read_text(encoding='utf-8'))
                assert len(bundle['lessons']) == 2 and 'settings' not in bundle
                await page.locator('[data-close=transfer-dialog]').click()
                await page.locator('a[data-page=directory]').click()
                await page.locator('#add-person').click()
                await page.locator('input[name=name]').fill('Лишний человек')
                await page.locator('#editor-form button[type=submit]').click()
                await expect(page.locator('#people .list-row')).to_have_count(3)
                await page.locator('a[data-page=schedule]').click()
                await page.locator('#transfer-open').click()
                await page.locator('#data-import').set_input_files(bundle_path)
                await expect(page.locator('.import-table')).to_be_visible()
                await page.locator('#import-mode').select_option('replace')
                await expect(page.locator('.replace-note')).to_be_visible()
                await expect(page.locator('#data-apply')).to_be_enabled()
                await page.locator('#data-apply').click()
                await expect(page.locator('#transfer-dialog')).not_to_be_visible()
                assert len(app.state.storage.config.people) == 2
                assert app.state.storage.config.settings.dry_run
                assert not app.state.storage.config.settings.automation_enabled
                await page.locator('a[data-page=settings]').click()
                await page.locator('#autostart').check()
                await expect(page.locator('#autostart-status')).to_contain_text('Будет запущена')
                assert desktop.enabled
                await page.locator('#autostart').uncheck()
                await expect(page.locator('#autostart-status')).to_have_text('Автозапуск выключен.')
                assert not desktop.enabled and not errors
            finally:
                await browser.close()
    Path('artifacts').mkdir(exist_ok=True)
    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive()
