from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from .demo import Observation
from .rules import Message


class MeetingEnded(RuntimeError):
    """The server explicitly ended the meeting; reconnection is inappropriate."""


class BrowserPool:
    def __init__(self):
        self.playwright = None
        self.browsers = {}
        self.lock = asyncio.Lock()

    async def context(self, headless=True):
        async with self.lock:
            if self.playwright is None:
                from playwright.async_api import async_playwright
                self.playwright = await async_playwright().start()
            browser = self.browsers.get(headless)
            if browser is None or not browser.is_connected():
                browser = await self.playwright.chromium.launch(headless=headless)
                self.browsers[headless] = browser
            context = await browser.new_context(locale="ru-RU", viewport={"width": 1440, "height": 1000})
            # No microphone/camera permissions are granted.
            context.set_default_timeout(10000)
            return context

    async def release(self, context):
        async with self.lock:
            browser = context.browser
            await context.close()
            if browser and browser.is_connected() and not browser.contexts:
                await browser.close()
                self.browsers = {key: value for key, value in self.browsers.items() if value is not browser}

    async def close(self):
        for browser in self.browsers.values():
            try:
                await browser.close()
            except Exception:
                pass
        self.browsers.clear()
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None


class BrowserAdapter:
    """DOM adapter. Current upstream selectors; must be verified on the target deployment."""

    def __init__(self, pool: BrowserPool, url: str, name: str, headless: bool):
        self.pool, self.url, self.name, self.headless = pool, url, name, headless
        self.context = self.page = None
        self.seen = set()
        self.seen_order = []
        self.script = Path(__file__).with_name("observe.js").read_text(encoding="utf-8")
        self.ready = False
        self.waiting = False
        self.progress_started = 0
        self.last_submit = 0
        self.last_audio_action = 0

    async def first_visible(self, selectors):
        for selector in selectors:
            elements = self.page.locator(selector)
            for index in range(min(await elements.count(), 20)):
                element = elements.nth(index)
                if await element.is_visible():
                    return element
        return None

    async def connect(self):
        self.context = await self.pool.context(self.headless)
        self.page = await self.context.new_page()
        await self.page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
        self.progress_started = time.monotonic()
        while True:
            if await self.advance_join():
                return True
            if self.waiting:
                # Let the engine expose the waiting state and keep polling this
                # exact page while Greenlight refreshes/redirects it itself.
                return False
            await asyncio.sleep(.5)

    async def snapshot(self):
        try:
            return await self.page.evaluate(self.script)
        except Exception as exc:
            # Greenlight can replace its document while we take a snapshot.
            if "Execution context was destroyed" in str(exc) or "Cannot find context" in str(exc):
                return None
            raise

    async def advance_join(self):
        raw = await self.snapshot()
        if raw is None:
            return False
        if raw["ended"]:
            raise MeetingEnded("Встреча завершена преподавателем")
        self.waiting = raw["waiting"]
        if self.waiting:
            self.progress_started = time.monotonic()
            return False
        if time.monotonic() - self.progress_started > 45:
            raise RuntimeError("Не подтверждён режим «Только слушать»; сохранена диагностика страницы")
        name = await self.first_visible(['input[name="join_name"]', 'input[name="guest_name"]', 'input[name="name"]', '#join_name', '#guest_name', 'input[placeholder*="имя" i]', 'input[placeholder*="name" i]'])
        if name and time.monotonic() - self.last_submit > 5:
            self.last_submit = time.monotonic()
            await name.fill(self.name)
            join = self.page.get_by_role("button", name="Присоединиться", exact=False)
            if not await join.count():
                join = self.page.get_by_role("button", name="Join", exact=False)
            if await join.count() and await join.first.is_visible():
                await join.first.click()
            else:
                await name.press("Enter")
            return False
        modal = await self.first_visible(['[data-test="audioModal"]', '[data-test="audioSettingsModal"]'])
        listen = await self.first_visible(['[data-test="listenOnlyBtn"]'])
        if listen:
            if await listen.is_enabled() and time.monotonic() - self.last_audio_action > 2:
                self.last_audio_action = time.monotonic()
                await listen.click(timeout=2000)
            return False
        if modal:
            # Do not click the footer through a modal overlay while it renders
            # its audio choices. That was causing repeated pointer timeouts.
            return False
        if not raw["listen_only"]:
            audio = await self.first_visible(['[data-test="joinAudio"]'])
            if audio and await audio.is_enabled() and time.monotonic() - self.last_audio_action > 2:
                self.last_audio_action = time.monotonic()
                await audio.click(timeout=2000)
            return False
        await self.ensure_public_chat()
        raw = await self.snapshot()
        if raw is None:
            return False
        if raw["ended"]:
            raise MeetingEnded("Встреча завершена преподавателем")
        for message in raw["messages"]:
            self.remember(message["id"])
        self.ready = True
        return True

    async def ensure_public_chat(self):
        if await self.first_visible(['[data-test="publicChat"] #message-input', '#message-input']):
            return
        public = await self.first_visible(['[data-test="publicChatButton"]', '[data-test="chatButton"][role="button"]', 'button[data-test="chatButton"]'])
        if public is None:
            candidates = self.page.get_by_role("button", name=re.compile(r"^(Общий чат|Public chat)$", re.I))
            if await candidates.count() and await candidates.first.is_visible():
                public = candidates.first
        if public:
            await public.click(timeout=2000)
            try:
                await self.page.locator('#message-input').wait_for(state="visible", timeout=3000)
            except Exception:
                pass  # The observation warning explains unavailable/locked chat.

    def remember(self, message_id):
        self.seen.add(message_id)
        self.seen_order.append(message_id)
        if len(self.seen_order) > 10000:
            self.seen.discard(self.seen_order.pop(0))

    async def observe(self):
        if self.page is None or self.page.is_closed():
            return Observation(False, False, None, [])
        raw = await self.snapshot()
        if raw is None:
            return Observation(False, False, None, [], waiting=self.waiting)
        now = time.time()
        messages = []
        for item in raw.pop("messages"):
            if not self.ready:
                continue
            if item["id"] not in self.seen:
                self.remember(item["id"])
                messages.append(Message(**item, at=now))
        return Observation(messages=messages, **raw)

    async def send(self, text):
        title = self.page.locator('[data-test="chatTitle"]')
        title_text = (await title.inner_text()).casefold() if await title.count() else ""
        if not any(word in title_text for word in ("общий чат", "public chat")):
            raise RuntimeError("Не подтверждено, что открыт общий чат")
        box = self.page.locator('#message-input')
        if not await box.count() or not await box.is_visible() or not await box.is_enabled():
            raise RuntimeError("Поле общего чата недоступно")
        await box.fill(text)
        button = await self.first_visible(['[data-test="sendMessageButton"]'])
        if button and await button.is_enabled():
            await button.click()
        else:
            await box.press("Enter")
        # UI confirmation is required; clearing the textarea alone is insufficient.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            raw = await self.page.evaluate(self.script)
            if any(m["id"] not in self.seen and m["name"] == self.name and m["text"] == text for m in raw["messages"]):
                return True
            await asyncio.sleep(.3)
        return False

    async def diagnostics(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        if self.page and not self.page.is_closed():
            await self.page.screenshot(path=str(directory / "page.png"), full_page=True, timeout=5000)
            # Store only DOM metadata rather than page HTML/session tokens.
            metadata = await self.page.locator('button,input,textarea,[data-test]').evaluate_all("els => els.slice(0,500).map(e => ({tag:e.tagName, test:e.getAttribute('data-test'), role:e.getAttribute('role'), label:e.getAttribute('aria-label'), name:e.getAttribute('name'), id:e.id}))")
            (directory / "elements.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            snapshot = await self.snapshot()
            if snapshot:
                snapshot["messages"] = snapshot["messages"][-20:]
                (directory / "page-state.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            fragments = await self.page.locator('[data-test="chatMessageItem"], p[data-test="chatUserMessageText"]').evaluate_all("els => els.slice(-8).map(e => {let p=e; for(let i=0;i<4 && p.parentElement;i++,p=p.parentElement){if(p.querySelector('time')) break;} return p.outerHTML.slice(0,16000);})")
            (directory / "chat-sample.html").write_text("\n".join(fragments), encoding="utf-8")

    async def close(self):
        if self.context:
            context = self.context
            self.context = self.page = None
            self.ready = False
            await self.pool.release(context)
