"""Windows integration; importing this module has no desktop/registry side effects."""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class Desktop:
    def __init__(self, directory: Path, port: int = 8765):
        self.directory = directory.resolve()
        self.port = port
        self.value_name = "BBBCompanion_" + hashlib.sha256(str(self.directory).casefold().encode()).hexdigest()[:12]

    def startup_command(self):
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            command = [str(pythonw), str(Path(__file__).resolve().parent.parent / "launcher.py")]
        return subprocess.list2cmdline([*command, "--data-dir", str(self.directory), "--port", str(self.port)])

    def status(self):
        enabled = False
        current = None
        if os.name == "nt":
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                    current, _ = winreg.QueryValueEx(key, self.value_name)
                    enabled = True
            except FileNotFoundError:
                pass
        return {"supported": os.name == "nt", "autostart": enabled,
                "autostart_current_path": current == self.startup_command() if enabled else True}

    def set_autostart(self, enabled: bool):
        if os.name != "nt":
            raise ValueError("Автозапуск доступен в Windows")
        import winreg
        command = self.startup_command()
        if enabled and len(command) > 260:
            raise ValueError("Путь слишком длинный для автозапуска Windows. Переместите приложение ближе к корню диска.")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, self.value_name)
                except FileNotFoundError:
                    pass
        return self.status()


class BrowserWindow:
    """Only touches top-level Chromium windows owned by this connection's PID."""
    supported = os.name == "nt"

    def __init__(self, process_id: int):
        self.process_id = process_id

    def handles(self):
        if not self.supported:
            return []
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        windows = []

        @callback_type
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, len(name))
            if pid.value == self.process_id and name.value.startswith("Chrome_WidgetWin_"):
                windows.append(hwnd)
            return True

        user32.EnumWindows(visit, 0)
        return windows

    def set_visible(self, visible: bool):
        from ctypes import wintypes
        windows = self.handles()
        if not windows:
            raise ValueError("Окно браузера ещё не готово или уже закрыто")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        for hwnd in windows:
            user32.ShowWindow(hwnd, 9 if visible else 0)  # SW_RESTORE / SW_HIDE
        if visible:
            user32.SetForegroundWindow(windows[0])


class Tray:
    def __init__(self, open_panel, shutdown):
        import pystray
        from PIL import Image, ImageDraw
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((2, 2, 62, 62), radius=15, fill="#176958")
        draw.rounded_rectangle((16, 17, 48, 43), radius=4, outline="white", width=4)
        draw.line((25, 51, 39, 51), fill="white", width=4)
        draw.line((32, 43, 32, 51), fill="white", width=4)
        self.icon = pystray.Icon("BBBCompanion", image, "BBB Companion 0.2.0", pystray.Menu(
            pystray.MenuItem("Открыть настройки", lambda: open_panel(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Завершить приложение", lambda: shutdown()),
        ))

    def start(self):
        import threading
        ready = threading.Event()
        errors = []

        def setup(icon):
            try:
                icon.visible = True
            except Exception as exc:
                errors.append(exc)
            finally:
                ready.set()

        self.icon.run_detached(setup)
        if not ready.wait(5) or errors:
            self.icon.stop()
            raise RuntimeError(str(errors[0]) if errors else "Трей Windows не ответил")

    def stop(self):
        self.icon.stop()
