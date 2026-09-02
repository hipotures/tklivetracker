import ast
import json
import logging
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest
import requests

from recorder.main import (
    cleanup_pid_file,
    cleanup_startup_metadata,
    write_pid_file,
    write_startup_metadata,
)
from recorder.utils.args_handler import validate_and_parse_args
from recorder.utils.cookie_extractor import extract_and_save_cookies
from recorder.utils.custom_exceptions import ArgsParseError
from recorder.utils.security import redact_secrets, redact_url_credentials
from selenium_supervisor import TelegramNotifier
from utils.username import (
    MAX_TIKTOK_USERNAME_LENGTH,
    MIN_TIKTOK_USERNAME_LENGTH,
    normalize_tiktok_username,
)


@pytest.mark.parametrize(
    "username",
    [
        "",
        "@",
        "../alice",
        "alice/part",
        r"alice\\part",
        "alice\npart",
        " alice",
        "alice ",
        "a" * 25,
        "alice💥",
    ],
)
def test_recorder_rejects_unsafe_usernames(monkeypatch, username) -> None:
    monkeypatch.setattr(sys, "argv", ["recorder", "-user", username])
    with pytest.raises(ArgsParseError):
        validate_and_parse_args()


def test_recorder_preserves_at_prefix_compatibility(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["recorder", "-user", "@alice_1"])
    args, _ = validate_and_parse_args()
    assert args.user == "alice_1"


@pytest.mark.parametrize(
    "username",
    ["abc", "abc_123", "abc.def", "leja..1", "a" * MAX_TIKTOK_USERNAME_LENGTH],
)
def test_canonical_username_validator_accepts_supported_forms(username) -> None:
    assert normalize_tiktok_username(username, strip_at=False) == username


@pytest.mark.parametrize(
    "username",
    [
        "",
        "a" * (MIN_TIKTOK_USERNAME_LENGTH - 1),
        ".",
        "..",
        "abc.",
        "/",
        "../abc",
        "abc/def",
        r"abc\def",
        "abc\x00def",
        "abc\ndef",
        "abc\x1fdef",
        "a" * (MAX_TIKTOK_USERNAME_LENGTH + 1),
    ],
)
def test_canonical_username_validator_rejects_unsafe_forms(username) -> None:
    assert normalize_tiktok_username(username, strip_at=False) is None


def test_canonical_username_validator_allows_at_plus_24_characters() -> None:
    username = "a" * MAX_TIKTOK_USERNAME_LENGTH

    assert normalize_tiktok_username(f"@{username}") == username


def test_pid_file_is_private_exclusive_and_owned(tmp_path) -> None:
    pid_path = Path(write_pid_file("alice", 1234, str(tmp_path)))
    assert stat.S_IMODE(pid_path.stat().st_mode) == 0o600

    with pytest.raises(FileExistsError):
        write_pid_file("alice", 9999, str(tmp_path))
    assert pid_path.read_text(encoding="ascii") == "1234"
    assert cleanup_pid_file(str(pid_path), 9999) is False
    assert pid_path.exists()
    assert cleanup_pid_file(str(pid_path), 1234) is True
    assert not pid_path.exists()


def test_pid_file_creation_does_not_follow_symlink(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("unchanged", encoding="utf-8")
    os.symlink(outside, tmp_path / "tiktok_live_alice.pid")

    with pytest.raises(FileExistsError):
        write_pid_file("alice", 1234, str(tmp_path))
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_startup_metadata_is_atomic_and_private(tmp_path) -> None:
    target = tmp_path / "startup.json"
    write_startup_metadata(str(target), {"pid": 123, "live_id": 7})

    assert json.loads(target.read_text(encoding="utf-8"))["live_id"] == 7
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".startup-*.tmp")) == []
    assert cleanup_startup_metadata(str(target), 999) is False
    assert target.exists()
    assert cleanup_startup_metadata(str(target), 123) is True
    assert not target.exists()


def _create_firefox_cookie_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE moz_cookies "
        "(name TEXT, value TEXT, host TEXT, path TEXT, lastAccessed INTEGER)"
    )
    connection.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, '.tiktok.com', '/', 1)",
        ("sessionid_ss", "cookie-value"),
    )
    connection.commit()
    connection.close()


def test_cookie_extraction_uses_private_random_temporary_files_and_basename_target(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "firefox.sqlite"
    _create_firefox_cookie_db(source)
    monkeypatch.chdir(tmp_path)
    config = {
        "firefox_cookie_db_path": str(source),
        "target_cookie_json_path": "cookies.json",
        "cookies_to_extract": ["sessionid_ss"],
    }

    assert extract_and_save_cookies(config) is True
    assert extract_and_save_cookies(config) is True

    output = tmp_path / "cookies.json"
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "sessionid_ss": "cookie-value"
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not (tmp_path / "cookies_temp.sqlite").exists()
    assert list(tmp_path.glob("tklivetracker-cookies-*")) == []


def test_proxy_and_known_secrets_are_redacted() -> None:
    proxy = "http://proxy-user:proxy-password@proxy.example:8080/path"
    redacted = redact_url_credentials(proxy)
    assert "proxy-user" not in redacted
    assert "proxy-password" not in redacted
    assert "proxy.example:8080" in redacted
    assert "bot-secret" not in redact_secrets(
        "request failed at /botbot-secret/sendMessage", "bot-secret"
    )


def test_telegram_request_errors_do_not_log_bot_token(tmp_path, monkeypatch, caplog) -> None:
    token = "123456:representative-secret-token"
    notifier = TelegramNotifier(
        {
            "enabled": True,
            "bot_token": token,
            "chat_id": "123",
            "state_file": str(tmp_path / "state.json"),
        },
        logging.getLogger("telegram-redaction-test"),
    )

    def fail_request(*args, **kwargs):
        raise requests.RequestException(f"failed URL {notifier.api_base}/sendMessage")

    monkeypatch.setattr(requests, "post", fail_request)
    with caplog.at_level(logging.ERROR):
        notifier._send_message("test")

    assert token not in caplog.text
    assert "[redacted]" in caplog.text


def test_telegram_state_is_written_atomically_with_private_permissions(tmp_path) -> None:
    state_path = tmp_path / "notifier-state.json"
    notifier = TelegramNotifier(
        {
            "enabled": True,
            "bot_token": "representative-token",
            "chat_id": "123",
            "state_file": str(state_path),
        },
        logging.getLogger("telegram-state-test"),
    )
    notifier.set_status("healthy")

    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "healthy"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".notifier-state.json-*.tmp")) == []


def test_all_control_plane_tiktok_requests_have_explicit_timeouts() -> None:
    source_path = Path(__file__).resolve().parents[1] / "recorder" / "core" / "tiktok_api.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "get":
            continue
        owner = node.func.value
        if not (
            isinstance(owner, ast.Attribute)
            and owner.attr == "http_client"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            continue
        calls.append(node)

    assert calls
    for call in calls:
        keywords = {keyword.arg for keyword in call.keywords}
        is_media_stream = "stream" in keywords
        if is_media_stream:
            assert "timeout" not in keywords
        else:
            assert "timeout" in keywords, f"missing timeout at line {call.lineno}"
