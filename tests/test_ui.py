"""Browser check of the actual local panel, configuration forms and live demo."""
import asyncio
import socket
import threading
import time

import uvicorn

from bbb_companion.app import create_app


def test_panel_forms_demo_and_live_log(tmp_path):
    app=create_app(tmp_path)
    sock=socket.socket()
    sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    # On Windows the browser needs Proactor. Server and test each own their loop.
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True)
    thread.start()
    end=time.monotonic()+10
    while not server.started and time.monotonic()<end: time.sleep(.05)
    assert server.started
    async def scenario():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser=await p.chromium.launch()
            page=await browser.new_page(viewport={'width':1440,'height':1000})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            try:
                await page.goto(f'http://127.0.0.1:{port}')
                await page.get_by_role('button',name='Включить расписание',exact=True).wait_for()
                await page.locator('a[data-page=directory]').click()
                await page.locator('#add-person').click()
                await page.locator('#editor input[name=name]').fill('Тестовый человек')
                await page.locator('#editor-form button[type=submit]').click()
                await page.locator('#people').get_by_text('Тестовый человек',exact=True).wait_for()
                await page.locator('#add-subject').click()
                await page.locator('#editor input[name=title]').fill('Тестовый предмет')
                await page.locator('#editor input[name=url]').fill('demo://ui-test')
                await page.locator('#editor select[name=adapter]').select_option('demo')
                await page.locator('#editor-form button[type=submit]').click()
                await page.locator('#subjects').get_by_text('Тестовый предмет',exact=True).wait_for()
                await page.locator('a[data-page=schedule]').click()
                await page.locator('#add-lesson').click()
                await page.locator('#editor input[name=person]').check()
                await page.locator('#editor-form button[type=submit]').click()
                await page.locator('#lessons').get_by_text('Тестовый предмет',exact=True).wait_for()
                # Creating configuration does not enable the scheduler implicitly.
                assert not app.state.storage.config.settings.automation_enabled
                await page.locator('a[data-page=now]').click()
                await page.locator('.demo-panel summary').click()
                await page.locator('button[data-demo=start]').click()
                await page.locator('.session .badge.active').first.wait_for(timeout=8000)
                await page.locator('button[data-demo=wave][data-text="Здравствуйте"]').click()
                await page.locator('a[data-page=logs]').click()
                await page.get_by_text('Сообщение «Здравствуйте»: отправлено',exact=True).first.wait_for(timeout=8000)
                await page.locator('#log-level').select_option('error')
                await page.get_by_text('Событий пока нет',exact=True).wait_for()
                await page.locator('#log-level').select_option('')
                await page.get_by_text('Сообщение «Здравствуйте»: отправлено',exact=True).first.wait_for()
                await page.locator('a[data-page=now]').click()
                artifacts=__import__('pathlib').Path('artifacts')
                artifacts.mkdir(exist_ok=True)
                await page.screenshot(path=str(artifacts/'panel-desktop.png'),full_page=True)
                await page.set_viewport_size({'width':390,'height':844})
                await page.screenshot(path=str(artifacts/'panel-mobile.png'),full_page=True)
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                assert not errors
            finally:
                await browser.close()
    try:
        asyncio.run(scenario())
    finally:
        server.should_exit=True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive()
