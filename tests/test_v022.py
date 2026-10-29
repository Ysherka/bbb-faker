import asyncio
import json
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from bbb_companion.app import create_app
from bbb_companion.engine import Engine
from bbb_companion.models import ChatSettings, Config
from bbb_companion.rules import ChatDetector, Message
from bbb_companion.schedule import SAMARA, due, occurrences
from bbb_companion.storage import Storage


def message(i, text='42', at=100, author=None):
    return Message(str(i), author or f'p{i}', f'Person {i}', text, at)


def test_identical_answers_normalization_original_text_and_restore():
    detector = ChatDetector(ChatSettings())
    reactions = []
    for i in range(5):
        reactions += detector.add(message(i, '42' if i<3 else ' ４２ ', at=100+i), set(), set())
    assert len(reactions)==1 and reactions[0].text=='42' and reactions[0].identical
    assert reactions[0].authors==reactions[0].total==5
    assert not detector.add(message(5),set(),set())
    saved = json.loads(json.dumps(detector.dump()))
    restored = ChatDetector(ChatSettings(), saved)
    for i in range(10,16):
        assert not restored.add(message(i,at=110+i),set(),set())
    reactions2 = []
    for i in range(20,25):
        reactions2 += restored.add(message(i,at=400+i),set(),set())
    assert len(reactions2)==1 and reactions2[0].id!=reactions[0].id
    assert any(item.get('label')=='Одинаковый ответ «42»' for item in restored.progress(425).values())


def test_identical_answers_require_authors_ratio_and_preserve_punctuation():
    detector = ChatDetector(ChatSettings())
    for i in range(6): assert not detector.add(message(i,author='one'),set(),set())
    for i in range(10,14): assert not detector.add(message(i,'другой ответ'),set(),set())
    assert not detector.add(message(20,author='own'),{'own'},set())
    assert not detector.add(Message('self','bot','My bot','42',100),set(),{'my bot'})
    for i in range(30,34): assert not detector.add(message(i),set(),set())
    result=detector.add(message(34),set(),set())
    assert len(result)==1 and result[0].authors==6 and result[0].total==10
    assert not detector.add(message(34),set(),set())
    detector = ChatDetector(ChatSettings())
    for i in range(3): assert not detector.add(message(i,'да'),set(),set())
    for i in range(3,6): assert not detector.add(message(i,'да?'),set(),set())
    assert len(detector.identical_waves)==2


def test_identical_can_be_disabled_and_whitelist_keeps_priority():
    disabled = ChatDetector(ChatSettings(repeat_identical=False))
    for i in range(6): assert not disabled.add(message(i),set(),set())
    for text in ('Здравствуйте','+','До свидания'):
        detector=ChatDetector(ChatSettings())
        result=[]
        for i in range(5): result+=detector.add(message(i,text),set(),set())
        assert len(result)==1 and not result[0].identical and not detector.identical_waves
    for i in range(20,25): result=disabled.add(message(i,'+',at=400+i),set(),set())
    assert len(result)==1 and not result[0].identical


def test_identical_expiry_length_and_inactive_wave_cleanup():
    detector=ChatDetector(ChatSettings())
    for i in range(5): assert not detector.add(message(i,'x'*301),set(),set())
    assert not detector.identical_waves
    detector=ChatDetector(ChatSettings())
    for i in range(10,14): assert not detector.add(message(i,at=i),set(),set())
    for i in range(20,24): assert not detector.add(message(i,at=100),set(),set())
    assert detector.add(message(24,at=101),set(),set())
    detector.add(message(30,'different',at=300),set(),set())
    assert set(detector.identical_waves)=={'different'}


def test_disabled_lessons_are_visible_but_never_due(tmp_path):
    day=datetime.now(SAMARA).date()
    now=datetime.combine(day,datetime.min.time(),SAMARA)+timedelta(hours=10,minutes=30)
    cfg=Config.model_validate(dict(people=[dict(id='p',name='Person')],subjects=[dict(id='s',title='Subject',url='demo://s',adapter='demo')],
        lessons=[dict(id='l',subject_id='s',person_ids=['p'],date=day,start='10:00',end='11:00',enabled=False)]))
    assert not occurrences(cfg,day,1) and not due(cfg,now)
    assert len(occurrences(cfg,day,1,include_disabled=True))==1
    app=create_app(tmp_path,run_engine=False)
    app.state.storage.save_config(cfg)
    with TestClient(app) as client:
        data=client.get('/api/week',params={'start':day.isoformat()}).json()
        assert len(data['lessons'])==1 and not data['lessons'][0]['enabled']
        assert not client.get('/api/status').json()['upcoming']


def test_generic_reply_respects_each_profile_setting_and_dry_run(tmp_path):
    async def wait_for(predicate):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if predicate(): return
            await asyncio.sleep(.05)
        raise AssertionError('Expected demo state did not arrive')
    async def scenario():
        store=Storage(tmp_path)
        cfg=Config();cfg.settings.chat.repeat_identical=False
        store.save_config(cfg)
        engine=Engine(store)
        await engine.start()
        try:
            await engine.start_demo(3)
            await wait_for(lambda:len([s for s in engine.active() if s.healthy])==3)
            sessions=engine.active()
            for target in sessions[1:]: target.snapshot['settings']['chat']['repeat_identical']=True
            sessions[2].snapshot['settings']['dry_run']=True
            room=engine.demo_rooms['demo://sandbox']
            for i in range(6): room.post(str(i),f'Author {i}','42')
            await wait_for(lambda:any(e['kind']=='would_send' for e in store.events(limit=1000)))
            replies=[m for m in room.messages if m.author.startswith('demo:')]
            assert len(replies)==1 and replies[0].author==sessions[1].id and replies[0].text=='42'
            await asyncio.sleep(1.2)
            assert len([m for m in room.messages if m.author.startswith('demo:')])==1
        finally:
            await engine.stop();store.close()
    asyncio.run(scenario())


def test_create_directory_entries_inside_lesson_and_keep_disabled_card(tmp_path):
    app=create_app(tmp_path,run_engine=False)
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline: time.sleep(.05)
    assert server.started
    async def scenario():
        from playwright.async_api import async_playwright, expect
        async with async_playwright() as p:
            browser=await p.chromium.launch()
            page=await browser.new_page(viewport=dict(width=1440,height=1000))
            page.set_default_timeout(7000)
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            try:
                await page.goto(f'http://127.0.0.1:{port}/#schedule')
                await page.locator('#add-lesson').click()  # Empty directory is supported.
                await page.locator('#editor input[name=start]').fill('09:15')
                await page.locator('#editor input[name=end]').fill('10:45')
                await page.locator('#editor select[name=repeat]').select_option('biweekly')
                today=datetime.now(SAMARA).date()
                monday=(today-timedelta(days=today.weekday())).isoformat()
                await page.locator('#editor input[name=anchor_date]').fill(monday)
                await page.locator('#editor input[name=weekday][value="0"]').check()
                await page.locator('#editor [data-quick=subject]').click()
                await page.locator('#quick-editor input[name=title]').fill('Новый предмет')
                await page.locator('#quick-editor input[name=url]').fill('bad-url')
                await page.locator('#quick-form [type=submit]').click()
                await expect(page.locator('#quick-error')).not_to_be_empty()
                await page.locator('#quick-editor input[name=url]').fill('demo://new')
                await page.locator('#quick-editor select[name=adapter]').select_option('demo')
                await page.locator('#quick-form [type=submit]').click()
                await expect(page.locator('#quick-editor')).not_to_be_visible()
                await expect(page.locator('#editor select[name=subject_id] option:checked')).to_have_text('Новый предмет')
                await page.locator('#editor [data-quick=person]').click()
                await page.locator('#quick-editor input[name=name]').fill('  Новый человек  ')
                await page.locator('#quick-form [type=submit]').click()
                await expect(page.locator('#editor input[name=person]')).to_be_checked()
                await page.locator('#editor [data-quick=person]').click()
                await page.locator('#quick-editor input[name=name]').fill('Не сохранять')
                await page.locator('#quick-editor [data-close=quick-editor]').first.click()
                await expect(page.locator('#editor input[name=start]')).to_have_value('09:15')
                await expect(page.locator('#editor select[name=repeat]')).to_have_value('biweekly')
                await expect(page.locator('#editor input[name=anchor_date]')).to_have_value(monday)
                await page.locator('#editor input[name=enabled]').uncheck()
                await page.locator('#editor-form [type=submit]').click()
                await expect(page.locator('#editor')).not_to_be_visible()
                await expect(page.locator('.week-lesson.disabled-lesson')).not_to_have_count(0)
                await expect(page.locator('.lesson-disabled-label').first).to_have_text('Выключено')
                await page.locator('.week-lesson.disabled-lesson').first.click()
                await expect(page.locator('#editor input[name=enabled]')).not_to_be_checked()
                await page.locator('#editor input[name=enabled]').check()
                await page.locator('#editor-form [type=submit]').click()
                await expect(page.locator('.week-lesson.disabled-lesson')).to_have_count(0)
                assert len(app.state.storage.config.people)==len(app.state.storage.config.subjects)==1
                assert app.state.storage.config.people[0].name=='Новый человек'
                await page.locator('.week-lesson').first.click()
                await page.locator('#editor input[name=enabled]').uncheck()
                await page.locator('#editor-form [type=submit]').click()
                Path('artifacts').mkdir(exist_ok=True)
                await page.screenshot(path='artifacts/v022-disabled-week.png',full_page=True)
                await page.set_viewport_size(dict(width=390,height=844))
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await page.screenshot(path='artifacts/v022-disabled-mobile.png',full_page=True)
                await page.locator('a[data-page=settings]').click()
                await expect(page.locator('input[name=repeat_identical]')).to_be_checked()
                await page.locator('input[name=repeat_identical]').uncheck()
                async with page.expect_response(lambda response: response.url.endswith('/api/config') and response.request.method=='PUT') as saved:
                    await page.locator('#settings-form [type=submit]').click()
                assert (await saved.value).ok
                assert not app.state.storage.config.settings.chat.repeat_identical
                assert not errors
            finally:
                await browser.close()
    try: asyncio.run(scenario())
    finally:
        server.should_exit=True;thread.join(timeout=15);sock.close()
        assert not thread.is_alive()
