from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="BBB Companion — локальная панель расписания")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--open-browser", action="store_true", help="Открыть панель сразу после запуска")
    parser.add_argument("--no-tray", action="store_true", help="Запустить без значка в трее")
    args = parser.parse_args()
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    bundled = base / "browsers"
    if bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
    directory = (args.data_dir or base / "data").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        # PyInstaller --windowed has no standard streams. Keep startup errors
        # readable even if the HTTP server or tray cannot start.
        stream = (directory / "startup.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    # OS-backed lock is released on crashes. It also prevents duplicate schedulers
    # when the second launch uses a different HTTP port for the same data directory.
    lock_file = (directory / "application.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        print("Приложение с этой папкой данных уже запущено.")
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        return 1
    from .app import create_app
    from .desktop import Desktop, Tray
    import uvicorn
    app = create_app(directory, desktop=Desktop(directory, args.port))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", workers=1))
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    def open_panel():
        def wait_and_open():
            import time
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=.2):
                        webbrowser.open(f"http://127.0.0.1:{args.port}")
                        return
                except OSError:
                    time.sleep(.2)
        threading.Thread(target=wait_and_open, daemon=True).start()

    tray = None
    try:
        if os.name == "nt" and not args.no_tray:
            try:
                tray = Tray(open_panel, app.state.shutdown)
                tray.start()
            except Exception as exc:
                app.state.storage.event("error", "tray_error", f"Не удалось запустить трей: {exc}")
                tray = None
        if not args.no_browser and (args.open_browser or (tray is None)):
            open_panel()
        server.run()
    finally:
        if tray:
            tray.stop()
        lock_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
