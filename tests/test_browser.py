import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bbb_companion.browser import BrowserAdapter, BrowserPool

FIXTURE = '''<!doctype html><meta charset="utf-8"><form id="join"><input name="join_name"><button>Присоединиться</button></form>
<main hidden id="meeting"><button data-test="listenOnlyBtn">Только слушать</button><button hidden data-test="leaveListenOnly">Отключить аудио</button><header data-test="chatTitle">Общий чат</header><span data-test="participantCount">42</span><div data-test="chatMessages"></div><textarea id="message-input"></textarea></main>
<script>let myName='',seq=0;const box=document.querySelector('#message-input');
function post(name,text){const el=document.createElement('div');el.className='chat-message-container';el.innerHTML='<div role="listitem"><div><div></div><time>10:00</time></div></div><div data-test="chatMessageItem"><div data-test="messageContent"><p></p></div></div>';el.querySelector('[role=listitem] div div').textContent=name;el.querySelector('[data-test=chatMessageItem]').setAttribute('data-chat-message-id',String(++seq));el.querySelector('p').textContent=text;document.querySelector('[data-test=chatMessages]').append(el);}
document.querySelector('#join').onsubmit=e=>{e.preventDefault();myName=document.querySelector('input').value;document.querySelector('#join').hidden=true;document.querySelector('#meeting').hidden=false;post('Старый автор','Здравствуйте');};
document.querySelector('[data-test=listenOnlyBtn]').onclick=e=>{e.target.hidden=true;document.querySelector('[data-test=leaveListenOnly]').hidden=false;};
box.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();post(myName,box.value);box.value='';}};
</script>'''


def test_real_browser_adapter_on_local_dom_fixture():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(FIXTURE.encode())
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    async def scenario():
        pool=BrowserPool()
        adapter=BrowserAdapter(pool,f'http://127.0.0.1:{server.server_port}/','Иван',True)
        try:
            await adapter.connect()
            initial=await adapter.observe()
            assert initial.listen_only and initial.healthy and initial.total==42
            assert initial.messages==[]  # Existing chat is seeded, not replayed.
            await adapter.page.evaluate("post('Автор', '+')")
            incoming=await adapter.observe()
            assert len(incoming.messages)==1 and incoming.messages[0].name=='Автор'
            assert incoming.messages[0].text=='+'
            assert await adapter.send('Здравствуйте')
            assert (await adapter.observe()).messages[0].name=='Иван'
            other=BrowserAdapter(pool,f'http://127.0.0.1:{server.server_port}/','Пётр',True)
            try:
                await other.connect()
                assert other.context != adapter.context
                assert await other.page.locator('input').input_value()=='Пётр'
                assert await adapter.page.locator('input').input_value()=='Иван'
            finally:
                await other.close()
        finally:
            await adapter.close()
            await pool.close()
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
