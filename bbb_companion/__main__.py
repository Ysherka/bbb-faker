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
    args = parser.parse_args()
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    bundled = base / "browsers"
    if bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
    directory = (args.data_dir or base / "data").resolve()
    directory.mkdir(parents=True, exist_ok=True)
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
        print("Приложение с этой папкой данных уже запущено.")
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        return 1
    from .app import create_app
    import uvicorn
    app = create_app(directory)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", workers=1))
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    if not args.no_browser:
        def open_panel():
            import time
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=.2):
                        webbrowser.open(f"http://127.0.0.1:{args.port}")
                        return
                except OSError:
                    time.sleep(.2)
        threading.Thread(target=open_panel, daemon=True).start()
    try:
        server.run()
    finally:
        lock_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
