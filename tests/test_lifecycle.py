import asyncio
import time
from datetime import datetime, timedelta

from bbb_companion.browser import MeetingEnded
from bbb_companion.demo import Observation
from bbb_companion.engine import Engine
from bbb_companion.models import Person, Subject
from bbb_companion.schedule import SAMARA
from bbb_companion.storage import Storage


async def until(predicate):
    end=time.monotonic()+7
    while time.monotonic()<end:
        if predicate(): return
        await asyncio.sleep(.03)
    raise AssertionError('State transition timeout')


def test_waiting_reuses_adapter_and_explicit_end_is_not_retried(tmp_path,monkeypatch):
    class Adapter:
        instances=[]
        waiting=True
        ended=False
        closed=0
        def __init__(self,*args): self.instances.append(self)
        async def connect(self): return False
        async def advance_join(self): return not self.waiting
        async def observe(self):
            return Observation(not self.waiting,not self.waiting,40,[],ended=self.ended,
                               waiting=self.waiting,waiting_reason='Встреча еще не началась' if self.waiting else None)
        async def close(self): self.closed+=1
    monkeypatch.setattr('bbb_companion.engine.BrowserAdapter',Adapter)
    async def scenario():
        store=Storage(tmp_path)
        engine=Engine(store)
        await engine.start()
        now=datetime.now(SAMARA)
        snapshot=engine.make_snapshot(Person(id='ivan',name='Иван'),Subject(id='math',title='Математика',url='https://example.test/b/local'),store.config.settings,now,now+timedelta(hours=1))
        store.create_run('local/ivan','ivan','local',snapshot)
        engine.spawn(store.run('local/ivan'))
        session=engine.sessions['local/ivan']
        try:
            await until(lambda:len(Adapter.instances)==1 and session.state=='waiting')
            await asyncio.sleep(1.2)
            assert len(Adapter.instances)==1 and Adapter.instances[0].closed==0
            assert not session.snapshot['ever_joined']
            Adapter.waiting=False
            await until(lambda:session.state=='active')
            Adapter.ended=True
            await until(lambda:session.state=='finished')
            assert len(Adapter.instances)==1 and Adapter.instances[0].closed==1
            assert store.run(session.id)['state']=='finished'
            events=store.events(limit=1000)
            assert any(event['kind']=='waiting_room' for event in events)
            assert not any(event['kind']=='connection_error' for event in events)
            assert not store.unfinished()
        finally:
            await engine.stop()
            store.close()
    asyncio.run(scenario())


def test_explicit_end_during_connect_is_saved_as_finished(tmp_path,monkeypatch):
    class Adapter:
        calls=0
        def __init__(self,*args): pass
        async def connect(self):
            Adapter.calls+=1
            raise MeetingEnded('meetingEndedModalTitle')
        async def close(self): pass
    monkeypatch.setattr('bbb_companion.engine.BrowserAdapter',Adapter)
    async def scenario():
        store=Storage(tmp_path)
        engine=Engine(store)
        await engine.start()
        now=datetime.now(SAMARA)
        snapshot=engine.make_snapshot(Person(id='ivan',name='Иван'),Subject(id='math',title='Математика',url='https://example.test/b/local'),store.config.settings,now,now+timedelta(hours=1))
        store.create_run('local/ivan','ivan','local',snapshot)
        engine.spawn(store.run('local/ivan'))
        try:
            await until(lambda:engine.sessions['local/ivan'].state=='finished')
            assert Adapter.calls==1
            assert not store.unfinished()
        finally:
            await engine.stop()
            store.close()
    asyncio.run(scenario())
