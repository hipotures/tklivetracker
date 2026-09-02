import json
import logging
import os
import stat
from pathlib import Path

import pytest

from persistent_live_manager.live_process_manager import LiveProcessManager
from scripts import selenium_live_monitor
from scripts.selenium_live_monitor import ConfigurationManager, SessionManager
from selenium_supervisor import SeleniumSupervisor


class FakeCookieDriver:
    def get_cookies(self):
        return [
            {"name": "sessionid_ss", "value": "representative-secret"},
            {"name": "tt_csrf_token", "value": "representative-csrf"},
        ]


def test_configured_cookie_path_is_honored(tmp_path, caplog) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "cookies:\n  cookie_json_file: exported/browser.json\n", encoding="utf-8"
    )
    output = tmp_path / "exported" / "browser.json"
    manager = SessionManager(
        FakeCookieDriver(), ConfigurationManager(str(config_path)).get_cookie_json_path()
    )

    with caplog.at_level(logging.INFO):
        assert manager.export_cookies_for_apps() is True

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "sessionid_ss": "representative-secret",
        "tt_csrf_token": "representative-csrf",
    }
    assert not (tmp_path / "private" / "cookies_full.json").exists()
    assert "representative-secret" not in caplog.text
    assert "representative-csrf" not in caplog.text


def _load_supervisor_config(config_path: Path) -> SeleniumSupervisor:
    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.config_path = str(config_path)
    supervisor._load_configuration()
    return supervisor


def test_monitor_and_supervisor_resolve_relative_cookie_path_from_config(tmp_path) -> None:
    config_dir = tmp_path / "example" / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "cookies:\n"
        "  enabled: true\n"
        "  cookies_source: json\n"
        "  cookie_json_file: ./private/cookies_full.json\n",
        encoding="utf-8",
    )
    expected = str(config_dir / "private" / "cookies_full.json")

    monitor_config = ConfigurationManager(str(config_path))
    supervisor = _load_supervisor_config(config_path)

    assert monitor_config.get_cookie_json_path() == expected
    assert supervisor.config["cookies"]["cookie_json_file"] == expected

    process_manager = LiveProcessManager(
        object(),
        {
            "recordings_path": str(tmp_path / "recordings"),
            "cookie_file_path": supervisor.config["cookies"]["cookie_json_file"],
        },
    )
    command = process_manager._build_recording_command(
        "alice", None, tmp_path, tmp_path / "alice.mp4"
    )
    cookie_option = command.index("--cookie-file")
    assert command[cookie_option + 1] == expected


def test_default_config_name_resolves_cookie_path_from_its_directory(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "cookies:\n  cookie_json_file: ./private/cookies_full.json\n",
        encoding="utf-8",
    )
    expected = str(tmp_path / "private" / "cookies_full.json")

    assert ConfigurationManager(str(config_path)).get_cookie_json_path() == expected
    assert (
        _load_supervisor_config(config_path).config["cookies"]["cookie_json_file"]
        == expected
    )


def test_cookie_paths_preserve_absolute_values(tmp_path) -> None:
    absolute_cookie_path = tmp_path / "shared" / "cookies.json"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        f"cookies:\n  cookie_json_file: {absolute_cookie_path}\n",
        encoding="utf-8",
    )

    monitor_config = ConfigurationManager(str(config_path))
    supervisor = _load_supervisor_config(config_path)

    assert monitor_config.get_cookie_json_path() == str(absolute_cookie_path)
    assert supervisor.config["cookies"]["cookie_json_file"] == str(absolute_cookie_path)


def test_supervisor_resolves_firefox_cookie_paths_from_config_directory(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "cookies:\n"
        "  firefox_cookie_db_path: ./firefox/cookies.sqlite\n"
        "  target_cookie_json_path: ./private/cookies.json\n",
        encoding="utf-8",
    )

    cookies = _load_supervisor_config(config_path).config["cookies"]

    assert cookies["firefox_cookie_db_path"] == str(
        config_dir / "firefox" / "cookies.sqlite"
    )
    assert cookies["target_cookie_json_path"] == str(
        config_dir / "private" / "cookies.json"
    )


def test_monitor_cli_passes_custom_config_to_monitor(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "custom.yaml"
    calls = []

    class FakeMonitor:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def run_monitor(self):
            calls.append("run")

    monkeypatch.setattr(selenium_live_monitor, "EnhancedLiveMonitor", FakeMonitor)
    monkeypatch.setattr(
        selenium_live_monitor.sys,
        "argv",
        ["selenium_live_monitor.py", "--config", str(config_path)],
    )

    selenium_live_monitor.main()

    assert calls == [{"config_path": str(config_path)}, "run"]


def test_monitor_cli_preserves_default_config_path(monkeypatch) -> None:
    calls = []

    class FakeMonitor:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def run_monitor(self):
            calls.append("run")

    monkeypatch.setattr(selenium_live_monitor, "EnhancedLiveMonitor", FakeMonitor)
    monkeypatch.setattr(
        selenium_live_monitor.sys, "argv", ["selenium_live_monitor.py"]
    )

    selenium_live_monitor.main()

    assert calls == [{"config_path": "config.yaml"}, "run"]


def test_cookie_export_supports_basename_and_private_permissions(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert SessionManager(FakeCookieDriver(), "cookies.json").export_cookies_for_apps()

    output = tmp_path / "cookies.json"
    assert json.loads(output.read_text(encoding="utf-8"))["sessionid_ss"]
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_cookie_export_uses_atomic_replace_and_cleans_temp_file(tmp_path, monkeypatch) -> None:
    output = tmp_path / "private" / "cookies.json"
    replaced = []
    real_replace = os.replace

    def recording_replace(source, destination):
        replaced.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(selenium_live_monitor.os, "replace", recording_replace)
    assert SessionManager(FakeCookieDriver(), output).export_cookies_for_apps()

    assert len(replaced) == 1
    assert replaced[0][0].parent == output.parent
    assert replaced[0][0].name != "cookies_temp.sqlite"
    assert replaced[0][1] == output
    assert list(output.parent.glob(".cookies.json.*.tmp")) == []


def test_cookie_export_cleans_temp_file_when_replace_fails(tmp_path, monkeypatch) -> None:
    output = tmp_path / "cookies.json"

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(selenium_live_monitor.os, "replace", fail_replace)

    assert SessionManager(FakeCookieDriver(), output).export_cookies_for_apps() is False
    assert not output.exists()
    assert list(tmp_path.glob(".cookies.json.*.tmp")) == []


def test_cookie_export_refuses_symlink_destination(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    destination = tmp_path / "cookies.json"
    destination.symlink_to(outside)

    assert SessionManager(FakeCookieDriver(), destination).export_cookies_for_apps() is False
    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert destination.is_symlink()
