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
    parser.add_argument('--exe',type=Path,default=root/'dist'/'releases'/'0.2.1'/'BBBCompanion'/'BBBCompanion.exe')
    parser.add_argument('--legacy',action='store_true')
    parser.add_argument('--profiles',type=int,default=15)
    args=parser.parse_args()
    if not 1 <= args.profiles <= 15: parser.error('--profiles must be 1..15')
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
    split=min(12,args.profiles-1) if args.profiles>1 else 1
    assignments=[('local',range(split))]
    if args.profiles>1: assignments.append(('other',range(split,args.profiles)))
    config={'settings':{'automation_enabled':True},
            'people':[{'id':f'frozen{i}','name':f'Frozen profile {i}'} for i in range(args.profiles)],
            'subjects':[{'id':subject,'title':f'Local fixture {subject}','url':f'http://127.0.0.1:{fixture_server.server_port}/?subject={subject}'} for subject,_ in assignments],
            'lessons':[{'id':subject,'subject_id':subject,'person_ids':[f'frozen{i}' for i in indices],
                        'date':start.date().isoformat(),'start':start.strftime('%H:%M'),'end':end.strftime('%H:%M')}
                        for subject,indices in assignments]}
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
            deadline=time.monotonic()+90
            connected=False
            sent=False
            token=None
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise RuntimeError(f'EXE exited with {process.returncode}')
                try:
                    status=json.loads(get('/api/status'))
                    connected=len([s for s in status['sessions'] if s['state']=='active' and s['listen_only']])==args.profiles
                    if connected: wave_ready.set()
                    events=json.loads(get('/api/events'))
                    sent=len([e for e in events if e['kind']=='sent' and e['level']=='info'])==args.profiles
                    token=re.search(r'name="app-token" content="([^"]+)"',get('/').decode()).group(1)
                    if connected and sent: break
                except (OSError,ValueError):
                    pass
                time.sleep(.3)
            if not connected or not sent:
                raise AssertionError(f'Frozen browser check failed: connected={connected}, sent={sent}')
            assert not any(e['kind']=='tray_error' for e in events), 'Native tray failed to start'
            assert len(status['subject_browsers'])==len(assignments)
            session=next(s for s in status['sessions'] if s['state']=='active')
            assert session['window_supported'] and not session['browser_visible']
            for visible in [True,False]:
                request=urllib.request.Request(address+'/api/subject-browser',data=json.dumps({'subject_id':session['subject_id'],'visible':visible}).encode(),
                    headers={'Content-Type':'application/json','X-App-Token':token})
                urllib.request.urlopen(request,timeout=10).close()
                current=next(s for s in json.loads(get('/api/status'))['sessions'] if s['id']==session['id'])
                assert current['browser_visible']==visible and current['state']=='active' and current['listen_only']
                others=[s for s in json.loads(get('/api/status'))['sessions'] if s['subject_id']!=session['subject_id']]
                assert all(not s['browser_visible'] for s in others)
            # Exercise the actual panel buttons against the frozen app too.
            from playwright.sync_api import sync_playwright, expect
            with sync_playwright() as p:
                browser=p.chromium.launch()
                try:
                    panel=browser.new_page(viewport={'width':1440,'height':1000})
                    panel.goto(address)
                    expect(panel.locator('#subject-browsers .list-row')).to_have_count(len(assignments))
                    expect(panel.locator('.session')).to_have_count(args.profiles)
                    button=panel.locator(f'[data-subject-browser="{session["subject_id"]}"]')
                    button.click()
                    expect(button).to_have_attribute('data-visible','false')
                    button.click()
                    expect(button).to_have_attribute('data-visible','true')
                    panel.screenshot(path=str(directory/'subject-browsers.png'),full_page=True)
                finally:
                    browser.close()
            request=urllib.request.Request(address+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json','X-App-Token':token})
            urllib.request.urlopen(request,timeout=5).close()
            process.wait(timeout=15)
            assert process.returncode==0
        print(f'Frozen EXE: {args.profiles} profiles, {len(assignments)} subjects, tray, listen-only, chat replies, subject window API/panel and shutdown OK')
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
