import os
import sys
from pathlib import Path

import pytest

from bbb_companion.desktop import Desktop, Tray


@pytest.mark.skipif(os.name != 'nt', reason='Windows registry/tray integration')
def test_autostart_uses_quoted_paths_and_only_its_own_registry_value(tmp_path, monkeypatch):
    import winreg
    # A private non-Run test key exercises native registry I/O without enabling
    # an actual startup task on the developer's machine or the CI runner.
    key_path = 'Software\\BBBCompanionTests\\' + tmp_path.name
    monkeypatch.setattr('bbb_companion.desktop.RUN_KEY', key_path)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    # The command is only stored/read, never executed. Keep its fake EXE path
    # short so the runner's longer temp directory cannot exhaust the Run limit.
    monkeypatch.setattr(sys, 'executable', str(Path(tmp_path.anchor) / 'With spaces' / 'BBBCompanion.exe'))
    desktop = Desktop(tmp_path / 'Data with spaces', port=9123)
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, 'OtherApp', 0, winreg.REG_SZ, 'leave-me')
        assert not desktop.status()['autostart']
        assert desktop.set_autostart(True)['autostart_current_path']
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            command = winreg.QueryValueEx(key, desktop.value_name)[0]
        assert command.startswith('"') and '" --port 9123' in command
        assert '--data-dir "' in command
        assert not desktop.set_autostart(False)['autostart']
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            assert winreg.QueryValueEx(key, 'OtherApp')[0] == 'leave-me'
        monkeypatch.setattr(sys, 'executable', str(tmp_path / ('x' * 260) / 'BBBCompanion.exe'))
        with pytest.raises(ValueError, match='слишком длинный'):
            desktop.set_autostart(True)
        assert not desktop.status()['autostart']
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)


@pytest.mark.skipif(os.name != 'nt', reason='Windows native tray')
def test_native_tray_start_menu_callbacks_and_shutdown():
    calls = []
    tray = Tray(lambda: calls.append('open'), lambda: calls.append('stop'))
    try:
        tray.start()
        assert tray.icon.visible
        tray.icon()  # Default double-click action
        tray.icon.menu.items[-1](tray.icon)
        assert calls == ['open', 'stop']
    finally:
        tray.stop()
