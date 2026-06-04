import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from monitoring.telegram_alerts import TelegramNotifier as T


def _bot():
    return types.SimpleNamespace(
        config={"dashboard": {"public_url": "http://example.test:8999"}})


def test_dashboard_command_builds_login_link(tmp_path, monkeypatch):
    pw_file = tmp_path / ".dashboard_password"
    pw_file.write_text("DASHBOARD_PASSWORD=secretpw123\n")
    monkeypatch.setattr(T, "_dashboard_password_file",
                        staticmethod(lambda: pw_file), raising=False)
    out = T._cmd_dashboard("", _bot())
    assert "http://example.test:8999/login?token=" in out
    assert "secretpw123" not in out          # raw password never in the reply


def test_dashboard_command_missing_password_file(tmp_path, monkeypatch):
    missing = tmp_path / "nope.txt"
    monkeypatch.setattr(T, "_dashboard_password_file",
                        staticmethod(lambda: missing), raising=False)
    out = T._cmd_dashboard("", _bot())
    assert "not running" in out.lower()
