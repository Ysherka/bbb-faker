"""Exercise the frozen app and its bundled browser using localhost only."""
from __future__ import annotations

import ast
import argparse
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo


def main():
    root=Path(__file__).resolve().parent.parent
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',type=Path,default=root/'dist'/'releases'/'0.2.0'/'BBBCompanion'/'BBBCompanion.exe')
    parser.add_argument('--legacy',action='store_true')
    args=parser.parse_args()
    exe=args.exe.resolve()
    if not exe.exists():
        raise RuntimeError('Build the executable first')
    tree=ast.parse((root/'tests'/('test_legacy_browser.py' if args.legacy else 'test_browser.py')).read_text(encoding='utf-8'))
    name='LEGACY_FIXTURE' if args.legacy else 'FIXTURE'
    fixture=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id==name for target in node.targets))
    wave_ready=threading.Event()
    # Start the wave only once the engine has seeded the existing chat and
    # confirmed entry. A fixed timer can fire during a cold EXE startup.
    fixture=fixture.replace('</script>',"""async function waitForWave(){
      const ready=await (await fetch('/wave-state')).text();
      if(ready==='1'){for(let i=0;i<6;i++)post('External '+i,'+');}
      else setTimeout(waitForWave,200);
    }waitForWave();</script>""")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path=='/wave-state':
                self.end_headers()
                self.wfile.write(b'1' if wave_ready.is_set() else b'0')
                return
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(fixture.encode('utf-8'))
        def log_message(self,*args): pass
    fixture_server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=fixture_server.serve_forever,daemon=True).start()
    directory=root/'artifacts'/('frozen-smoke-'+uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    now=datetime.now(ZoneInfo('Europe/Samara'))
    start,end=now-timedelta(minutes=1),now+timedelta(minutes=30)
    config={'settings':{'automation_enabled':True},'people':[{'id':'frozen','name':'Frozen profile'}],
            'subjects':[{'id':'local','title':'Local browser fixture','url':f'http://127.0.0.1:{fixture_server.server_port}/'}],
            'lessons':[{'id':'local','subject_id':'local','person_ids':['frozen'],'date':start.date().isoformat(),
                        'start':start.strftime('%H:%M'),'end':end.strftime('%H:%M')}]}
    (directory/'config.json').write_text(json.dumps(config),encoding='utf-8')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    address=f'http://127.0.0.1:{port}'
    env=os.environ.copy()
    env.pop('PYTHONPATH',None)
    # If the EXE fails to configure its bundled browsers, global cache fallback fails.
    env['PLAYWRIGHT_BROWSERS_PATH']=str(directory/'nonexistent-browser-cache')
    process=None
    def get(path):
        return urllib.request.urlopen(address+path,timeout=3).read()
    try:
        with (directory/'process.log').open('wb') as log:
            process=subprocess.Popen([str(exe),'--no-browser','--port',str(port),'--data-dir',str(directory)],
                                     stdout=log,stderr=log,env=env,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            deadline=time.monotonic()+35
            connected=False
            sent=False
            token=None
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise RuntimeError(f'EXE exited with {process.returncode}')
                try:
                    status=json.loads(get('/api/status'))
                    connected=connected or any(s['state']=='active' and s['listen_only'] for s in status['sessions'])
                    if connected: wave_ready.set()
                    events=json.loads(get('/api/events'))
                    sent=any(e['kind']=='sent' and e['level']=='info' for e in events)
                    token=re.search(r'name="app-token" content="([^"]+)"',get('/').decode()).group(1)
                    if connected and sent: break
                except (OSError,ValueError):
                    pass
                time.sleep(.3)
            if not connected or not sent:
                raise AssertionError(f'Frozen browser check failed: connected={connected}, sent={sent}')
            assert not any(e['kind']=='tray_error' for e in events), 'Native tray failed to start'
            session=next(s for s in status['sessions'] if s['state']=='active')
            assert session['window_supported'] and not session['browser_visible']
            for action,visible in [('show_browser',True),('hide_browser',False)]:
                request=urllib.request.Request(address+'/api/action',data=json.dumps({'id':session['id'],'action':action}).encode(),
                    headers={'Content-Type':'application/json','X-App-Token':token})
                urllib.request.urlopen(request,timeout=10).close()
                current=next(s for s in json.loads(get('/api/status'))['sessions'] if s['id']==session['id'])
                assert current['browser_visible']==visible and current['state']=='active' and current['listen_only']
            request=urllib.request.Request(address+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json','X-App-Token':token})
            urllib.request.urlopen(request,timeout=5).close()
            process.wait(timeout=15)
            assert process.returncode==0
        print('Frozen EXE: tray, bundled Chromium, listen-only, chat reply, show/hide without rejoin and shutdown OK')
        print(directory)
    except Exception:
        if (directory/'process.log').exists():
            print((directory/'process.log').read_text(encoding='utf-8',errors='replace'))
        if (directory/'startup.log').exists():
            print((directory/'startup.log').read_text(encoding='utf-8',errors='replace'))
        raise
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        fixture_server.shutdown()
        fixture_server.server_close()


if __name__=='__main__':
    main()
