import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bbb_companion.browser import BrowserAdapter, BrowserPool, MeetingEnded
from bbb_companion.models import ChatSettings
from bbb_companion.rules import ChatDetector


# Structure from the official BBB 2 time-window-chat-item component. No BBB 3
# chatMessageItem, messageContent, message-id or author-id attributes exist.
LEGACY_FIXTURE = '''<!doctype html><meta charset="utf-8">
<form id="join"><input name="join_name"><button>Присоединиться</button></form>
<main hidden id="meeting">
<div data-test="userListContent"><h3>ПОЛЬЗОВАТЕЛИ (73)</h3></div>
<div data-test="chatButton" role="button" aria-label="Общий чат" id="chat-toggle-button">Общий чат</div>
<div data-test="publicChat" hidden><header data-test="chatTitle">Общий чат</header><div data-test="chatMessages"></div>
<form id="message-form"><textarea id="message-input"></textarea><button data-test="sendMessageButton" type="submit">Отправить сообщение</button></form></div>
<div data-test="audioModal" role="dialog" hidden style="min-height:100px"></div>
<button data-test="joinAudio">Подключиться к аудио</button><button data-test="leaveListenOnly" hidden>Отключить аудио</button>
</main>
<script>
let myName='',seq=0,footerClicks=0;
function post(name,text,append=false){
 const list=document.querySelector('[data-test=chatMessages]');let group=list.lastElementChild;
 if(!append||!group){group=document.createElement('div');group.innerHTML='<div class="meta"><div class="name"><span></span></div><time></time></div><div class="messages"></div>';group.querySelector('span').textContent=name;group.querySelector('time').dateTime=new Date(1700000000000+(++seq)*1000).toISOString();group.querySelector('time').textContent='11:36';list.append(group);}
 const p=document.createElement('p');p.dataset.test='chatUserMessageText';p.textContent=text;group.querySelector('.messages').append(p);
}
function endMeeting(){document.body.innerHTML='<h1 data-test="meetingEndedModalTitle">Этот вебинар был завершен преподавателем</h1><button>ОК</button>';}
document.querySelector('#join').onsubmit=e=>{e.preventDefault();myName=document.querySelector('input').value;document.querySelector('#join').hidden=true;document.querySelector('#meeting').hidden=false;post('Старый автор','Здравствуйте');const modal=document.querySelector('[data-test=audioModal]');modal.hidden=false;setTimeout(()=>{modal.innerHTML='<button data-test="listenOnlyBtn">Только слушать</button>';modal.querySelector('button').onclick=()=>{modal.hidden=true;document.querySelector('[data-test=leaveListenOnly]').hidden=false;};},400);};
document.querySelector('[data-test=joinAudio]').onclick=()=>{footerClicks++;};
document.querySelector('#chat-toggle-button').onclick=()=>document.querySelector('[data-test=publicChat]').hidden=false;
document.querySelector('#message-form').onsubmit=e=>{e.preventDefault();const box=document.querySelector('#message-input');post(myName,box.value);box.value='';};
</script>'''

WAITING_FIXTURE = '''<!doctype html><meta charset="utf-8"><h4>Встреча еще не началась</h4><p>Вы автоматически присоединитесь, когда начнётся встреча.</p><script>setTimeout(()=>location.reload(),200);</script>'''
ENDED_FIXTURE = '''<!doctype html><meta charset="utf-8"><h1 data-test="meetingEndedModalTitle">Этот вебинар был завершен преподавателем</h1><button aria-label="ОК">ОК</button>'''


class LocalServer:
    def __init__(self, html=LEGACY_FIXTURE):
        self.html=html
        self.requests=0
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests+=1
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(owner.html.encode('utf-8'))
            def log_message(self,*args): pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url=f'http://127.0.0.1:{self.server.server_port}/'
    def __enter__(self):
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        return self
    def __exit__(self,*args):
        self.server.shutdown()
        self.server.server_close()


def test_bbb2_chat_grouped_authors_wave_and_send_button():
    async def scenario(url):
        pool=BrowserPool()
        adapter=BrowserAdapter(pool,url,'Иван',True)
        try:
            assert await adapter.connect()
            initial=await adapter.observe()
            assert initial.chat_layout=='bbb2'
            assert initial.chat_parsed==initial.chat_count==1
            assert initial.total==73 and not initial.messages
            assert await adapter.page.evaluate('footerClicks')==0
            await adapter.page.evaluate("post('Автор 0','+');post('Автор 0','++',true);for(let i=1;i<5;i++)post('Автор '+i,i%2?'+':'++');")
            received=await adapter.observe()
            assert len(received.messages)==6
            assert received.messages[0].author==received.messages[1].author
            assert received.messages[0].id!=received.messages[1].id
            detector=ChatDetector(ChatSettings())
            reactions=[]
            for message in received.messages:
                reactions.extend(detector.add(message,set(),set()))
            assert len(reactions)==1 and reactions[0].authors==5
            assert not (await adapter.observe()).messages
            assert await adapter.send(reactions[0].text)
            assert (await adapter.observe()).messages[0].name=='Иван'
            await adapter.page.evaluate('endMeeting()')
            ended=await adapter.observe()
            assert ended.ended and not ended.healthy
            browser=adapter.context.browser
            await adapter.close()
            assert not browser.is_connected()
        finally:
            await adapter.close()
            await pool.close()
    with LocalServer() as server:
        asyncio.run(scenario(server.url))


def test_waiting_page_reloads_keep_browser_context_and_then_join():
    async def scenario(server):
        pool=BrowserPool()
        adapter=BrowserAdapter(pool,server.url,'Иван',True)
        try:
            assert await adapter.connect() is False
            context,page,browser=adapter.context,adapter.page,adapter.context.browser
            for _ in range(5):
                adapter.progress_started=time.monotonic()-1000
                assert await adapter.advance_join() is False
                await asyncio.sleep(.25)
            assert server.requests>=3
            assert len(browser.contexts)==1
            assert adapter.context is context and adapter.page is page
            server.html=LEGACY_FIXTURE
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if await adapter.advance_join(): break
                await asyncio.sleep(.15)
            else: raise AssertionError('Did not join after the same page reloaded into the open meeting')
            assert adapter.context is context and adapter.page is page
            assert (await adapter.observe()).listen_only
        finally:
            await adapter.close()
            await pool.close()
    with LocalServer(WAITING_FIXTURE) as server:
        asyncio.run(scenario(server))


def test_ended_modal_during_entry_is_terminal_before_audio_actions():
    async def scenario(url):
        pool=BrowserPool()
        adapter=BrowserAdapter(pool,url,'Иван',True)
        try:
            with pytest.raises(MeetingEnded):
                await adapter.connect()
            assert (await adapter.observe()).ended
        finally:
            await adapter.close()
            await pool.close()
    with LocalServer(ENDED_FIXTURE) as server:
        asyncio.run(scenario(server.url))


def test_waiting_notice_without_heading_is_recognized():
    async def scenario(url):
        pool=BrowserPool()
        adapter=BrowserAdapter(pool,url,'Иван',True)
        try:
            assert await adapter.connect() is False
            assert (await adapter.observe()).waiting
        finally:
            await adapter.close()
            await pool.close()
    with LocalServer(WAITING_FIXTURE.replace('<h4>','<div>').replace('</h4>','</div>')) as server:
        asyncio.run(scenario(server.url))
