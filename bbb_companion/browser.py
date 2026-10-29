from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .demo import Observation
from .rules import Message
from .desktop import BrowserWindow

OUTPUT_AUDIO_SCRIPT = """(() => {
  let muted = __MUTED__;
  const previous = new WeakMap();
  function silence(element) {
    if (!muted || !(element instanceof HTMLMediaElement)) return;
    if (!previous.has(element)) previous.set(element, element.muted);
    if (!element.muted) element.muted = true;
  }
  function sync() { document.querySelectorAll('audio,video').forEach(silence); }
  window.__bbbSetOutputMuted = value => {
    muted = value;
    document.querySelectorAll('audio,video').forEach(element => {
      if (muted) silence(element);
      else if (previous.has(element)) { element.muted = previous.get(element); previous.delete(element); }
    });
  };
  document.addEventListener('volumechange', event => silence(event.target), true);
  new MutationObserver(sync).observe(document, {childList: true, subtree: true});
  sync();
  window.__bbbOutputMuted().then(window.__bbbSetOutputMuted).catch(() => {});
})()"""


class MeetingEnded(RuntimeError):
    """The server explicitly ended the meeting; reconnection is inappropriate."""


@dataclass
class SubjectBrowser:
    key: str
    browser: object
    visible: bool
    pid: int | None = None
    members: list = field(default_factory=list)
    selected: object | None = None
    displayed: object | None = None


class BrowserPool:
    def __init__(self):
        self.playwright = None
        self.groups = {}
        self.lock = asyncio.Lock()
        self.stopping = False

    @property
    def browsers(self):
        return {key: group.browser for key, group in self.groups.items()}

    async def open(self, adapter):
        async with self.lock:
            if self.playwright is None:
                from playwright.async_api import async_playwright
                self.playwright = await async_playwright().start()
            key = adapter.subject_id
            group = self.groups.get(key)
            if group is None or not group.browser.is_connected():
                browser = await self.playwright.chromium.launch(
                    headless=False if os.name == "nt" else adapter.headless,
                    args=["--window-position=-32000,-32000"] if os.name == "nt" else [],
                )
                group = SubjectBrowser(key, browser, not adapter.headless)
                self.groups[key] = group
                if BrowserWindow.supported:
                    cdp = await browser.new_browser_cdp_session()
                    try:
                        processes = await cdp.send("SystemInfo.getProcessInfo")
                        group.pid = next(p["id"] for p in processes["processInfo"] if p["type"] == "browser")
                    finally:
                        await cdp.detach()
            adapter.group = group
            context = await group.browser.new_context(locale="ru-RU", viewport={"width": 1440, "height": 1000})
            adapter.context = context
            group.members.append(adapter)
            # No microphone/camera permissions are granted.
            context.set_default_timeout(10000)
            await context.expose_binding("__bbbOutputMuted", lambda source: not adapter.output_enabled)
            await context.add_init_script(OUTPUT_AUDIO_SCRIPT.replace("__MUTED__", "true"))
            before = set(BrowserWindow(group.pid).handles()) if group.pid else set()
            adapter.page = await context.new_page()
            if group.pid:
                # Each isolated context gets a native incognito window. Capture
                # its handle while creation is serialized, then show only one.
                for _ in range(40):
                    created = set(BrowserWindow(group.pid).handles()) - before
                    if created:
                        adapter.window = BrowserWindow(group.pid, next(iter(created)))
                        adapter.window.set_visible(False)
                        break
                    await asyncio.sleep(.05)
                else:
                    raise RuntimeError("Не найдено окно нового контекста Chromium")
            await self._apply_visibility(group)

    async def _apply_visibility(self, group):
        live = [a for a in group.members if a.page and not a.page.is_closed()]
        if group.selected not in live:
            group.selected = live[0] if live else None
        for member in live:
            selected = group.visible and not self.stopping and member is group.selected
            if member.window:
                if selected and group.displayed is not member:
                    cdp = await member.context.new_cdp_session(member.page)
                    try:
                        info = await cdp.send("Browser.getWindowForTarget")
                        await cdp.send("Browser.setWindowBounds", {"windowId": info["windowId"], "bounds": {"windowState": "normal"}})
                        await cdp.send("Browser.setWindowBounds", {"windowId": info["windowId"], "bounds": {"left": 80, "top": 60, "width": 1280, "height": 900}})
                    finally:
                        await cdp.detach()
                if not selected or group.displayed is not member:
                    member.window.set_visible(selected)
            try:
                await member.page.evaluate("muted => window.__bbbSetOutputMuted?.(muted)", not selected)
            except Exception as exc:
                # The exposed binding reapplies the current choice on reload.
                if not any(word in str(exc) for word in ("Execution context was destroyed", "Cannot find context", "Target page, context or browser has been closed")):
                    raise
        group.displayed = group.selected if group.visible and not self.stopping else None

    async def set_visible(self, adapter, visible):
        async with self.lock:
            group = adapter.group
            if not group or not group.browser.is_connected() or not adapter.window:
                raise ValueError("Окно предмета пока недоступно")
            group.visible = visible
            group.displayed = None
            await self._apply_visibility(group)

    async def release(self, adapter, context):
        async with self.lock:
            group = adapter.group
            if group and adapter in group.members:
                group.members.remove(adapter)
            try:
                await context.close()
            finally:
                if group:
                    if not group.members:
                        await group.browser.close()
                        if self.groups.get(group.key) is group:
                            del self.groups[group.key]
                    elif group.browser.is_connected():
                        await self._apply_visibility(group)

    async def close(self):
        self.stopping = True
        for group in list(self.groups.values()):
            try:
                await group.browser.close()
            except Exception:
                pass
        self.groups.clear()
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None


class BrowserAdapter:
    """DOM adapter. Current upstream selectors; must be verified on the target deployment."""

    def __init__(self, pool: BrowserPool, url: str, name: str, headless: bool, subject_id: str | None = None):
        self.pool, self.url, self.name, self.headless = pool, url, name, headless
        self.context = self.page = None
        self.subject_id = subject_id or url
        self.group = None
        self.window = None
        self.seen = set()
        self.seen_order = []
        self.script = Path(__file__).with_name("observe.js").read_text(encoding="utf-8")
        self.ready = False
        self.waiting = False
        self.progress_started = 0
        self.last_submit = 0
        self.last_audio_action = 0

    @property
    def browser_visible(self):
        return self.group.visible if self.group else False

    @property
    def output_enabled(self):
        return bool(self.group and not self.pool.stopping and self.group.visible and self.group.selected is self)

    async def first_visible(self, selectors):
        for selector in selectors:
            elements = self.page.locator(selector)
            for index in range(min(await elements.count(), 20)):
                element = elements.nth(index)
                if await element.is_visible():
                    return element
        return None

    async def connect(self):
        await self.pool.open(self)
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

    async def set_visible(self, visible: bool):
        await self.pool.set_visible(self, visible)

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
            try:
                await self.pool.release(self, context)
            finally:
                self.window = self.group = None
