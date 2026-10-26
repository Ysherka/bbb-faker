import asyncio
import time

from fastapi.testclient import TestClient

from bbb_companion.app import create_app
from bbb_companion.engine import Engine
from bbb_companion.models import Config
from bbb_companion.storage import Storage


async def until(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(.05)
    raise AssertionError("Ожидаемое состояние не наступило")


def test_five_profiles_waves_pause_and_mass_exit(tmp_path):
    async def scenario():
        store = Storage(tmp_path)
        cfg = Config()
        cfg.settings.leave.confirm_seconds = 1
        store.save_config(cfg)
        engine = Engine(store)
        await engine.start()
        try:
            await engine.start_demo(5)
            await until(lambda: len([s for s in engine.active() if s.healthy]) == 5)
            sessions = engine.active()
            await engine.command(sessions[-1].id, "pause")
            room = engine.demo_rooms["demo://sandbox"]
            for i in range(6):
                room.post(str(i), f"Автор {i}", "Здравствуйте" if i % 2 else "Добрый день!")
            await until(lambda: len([m for m in room.messages if m.author.startswith("demo:")]) == 4)
            await asyncio.sleep(1.2)
            assert len([m for m in room.messages if m.author.startswith("demo:")]) == 4
            await engine.command(sessions[-1].id, "pause")
            # Old greetings do not generate a catch-up reply after unpausing.
            for i in range(6):
                room.post(str(i), f"Автор {i}", "+")
            await until(lambda: len([m for m in room.messages if m.author.startswith("demo:") and m.text == "+"]) == 5)
            # Each session has an immutable configuration snapshot.
            changed = store.config.model_copy(deep=True)
            changed.settings.leave.confirm_seconds = 100
            store.save_config(changed)
            assert sessions[0].settings.leave.confirm_seconds == 1
            room.external = 10
            await until(lambda: all(s.state == "finished" for s in sessions))
            assert len(room.members) == 0
            assert any(e["kind"] == "exit_candidate" for e in store.events(limit=1000))
            assert any(e["kind"] == "chat_observed" for e in store.events(limit=1000))
        finally:
            await engine.stop()
            store.close()
    asyncio.run(scenario())


def test_restart_restores_only_unfinished_and_suppresses_previous_wave(tmp_path):
    async def scenario():
        store = Storage(tmp_path)
        engine = Engine(store)
        await engine.start()
        await engine.start_demo(2)
        await until(lambda: len([s for s in engine.active() if s.healthy]) == 2)
        room = engine.demo_rooms["demo://sandbox"]
        for i in range(6): room.post(str(i), f"Автор {i}", "+")
        await until(lambda: len([m for m in room.messages if m.author.startswith("demo:")]) == 2)
        sessions = engine.active()
        manual_id = sessions[0].id
        await engine.command(manual_id, "leave")
        await until(lambda: sessions[0].state == "manual")
        await engine.stop()
        store.close()
        store = Storage(tmp_path)
        restarted = Engine(store)
        await restarted.start()
        try:
            await until(lambda: len(restarted.active()) == 1 and restarted.active()[0].healthy)
            assert manual_id not in restarted.sessions
            room = restarted.demo_rooms["demo://sandbox"]
            for i in range(6): room.post(str(i), f"Автор {i}", "+")
            await asyncio.sleep(1.3)
            assert not [m for m in room.messages if m.author.startswith("demo:")]
        finally:
            await restarted.stop()
            store.close()
    asyncio.run(scenario())


def test_api_config_validation_local_access_and_history(tmp_path):
    app = create_app(tmp_path, run_engine=False)
    with TestClient(app) as client:
        token = app.state.token
        assert client.get('/').status_code == 200
        assert token in client.get('/').text
        assert client.put('/api/config', json={}).status_code == 403
        headers = {"X-App-Token": token}
        broken = {"people":[{"id":"p","name":" "}]}
        assert client.put('/api/config', json=broken, headers=headers).status_code == 422
        assert client.get('/api/config').json()["people"] == []
        assert client.put('/api/config', json={"people":[{"id":"p","name":"Иван"}]}, headers=headers).status_code == 200
        assert client.get('/api/config').json()["people"][0]["name"] == "Иван"
        assert client.get('/api/events').json()[-1]["kind"] == "config_saved"
        assert client.get('/api/events?person_id=absent').json() == []
        assert client.get('/api/export/log').status_code == 200
        assert client.get('/api/status', headers={"Host":"external.example"}).status_code == 403


def test_initial_room_with_only_five_profiles_does_not_finish(tmp_path):
    async def scenario():
        store=Storage(tmp_path)
        cfg=Config()
        cfg.settings.leave.empty_seconds=1
        cfg.settings.leave.confirm_seconds=1
        store.save_config(cfg)
        engine=Engine(store)
        await engine.start()
        try:
            await engine.start_demo(5)
            engine.demo_rooms['demo://sandbox'].external=0
            await until(lambda:len([s for s in engine.active() if s.healthy])==5)
            await asyncio.sleep(2.2)
            assert len(engine.active())==5
            assert not engine.rooms['demo://sandbox'].leave.had_external
        finally:
            await engine.stop()
            store.close()
    asyncio.run(scenario())
