from pathlib import Path

import scripts.ttdel as ttdel
from scripts.ttdel import ApiError, TtDelConfig, run_ttdel


def _config() -> TtDelConfig:
    return TtDelConfig(api_url="http://tracker.example:5001")


def test_ttdel_deactivates_confirmed_user_through_api(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_api_request(config, method, path, data=None):
        calls.append((config.api_url, method, path, data))
        if method == "GET":
            return {
                "user": {
                    "username": "alice",
                    "is_active": True,
                    "is_deleted": False,
                }
            }
        return {"already_inactive": False, "moved": True}

    monkeypatch.setattr(ttdel, "_api_request", fake_api_request)
    monkeypatch.setattr(ttdel, "_confirm_deactivation", lambda username: True)

    exit_code = run_ttdel(
        _config(),
        tmp_path / "any" / "export" / "alice",
    )

    assert exit_code == 0
    assert calls == [
        (
            "http://tracker.example:5001",
            "GET",
            "/api/users/alice",
            None,
        ),
        (
            "http://tracker.example:5001",
            "POST",
            "/api/users/alice/deactivate",
            {},
        ),
    ]
    assert "deactivated user: alice" in capsys.readouterr().out


def test_ttdel_keeps_active_user_without_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_api_request(config, method, path, data=None):
        calls.append((method, path, data))
        return {
            "user": {
                "username": "alice",
                "is_active": True,
                "is_deleted": False,
            }
        }

    monkeypatch.setattr(ttdel, "_api_request", fake_api_request)
    monkeypatch.setattr(ttdel, "_confirm_deactivation", lambda username: False)

    assert run_ttdel(_config(), tmp_path / "alice") == 0
    assert calls == [("GET", "/api/users/alice", None)]
    assert "user remains active: alice" in capsys.readouterr().out


def test_ttdel_reports_already_inactive_without_prompt(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def fake_api_request(config, method, path, data=None):
        return {
            "user": {
                "username": "alice",
                "is_active": False,
                "is_deleted": False,
            }
        }

    monkeypatch.setattr(ttdel, "_api_request", fake_api_request)
    monkeypatch.setattr(
        ttdel,
        "_confirm_deactivation",
        lambda username: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    assert run_ttdel(_config(), tmp_path / "alice") == 0
    assert "user already inactive: alice" in capsys.readouterr().out


def test_ttdel_reports_missing_server_user(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def missing_user(*args, **kwargs):
        raise ApiError("User not found", 404)

    monkeypatch.setattr(ttdel, "_api_request", missing_user)

    assert run_ttdel(_config(), tmp_path / "alice") == 1
    assert "user not found in server database: alice" in capsys.readouterr().out


def test_ttdel_reports_deleted_user_without_prompt(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def fake_api_request(config, method, path, data=None):
        return {
            "user": {
                "username": "alice",
                "is_active": True,
                "is_deleted": True,
            }
        }

    monkeypatch.setattr(ttdel, "_api_request", fake_api_request)

    assert run_ttdel(_config(), tmp_path / "alice") == 0
    assert "user is marked as deleted: alice" in capsys.readouterr().out


def test_ttdel_dry_run_checks_state_without_prompt_or_write(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_api_request(config, method, path, data=None):
        calls.append((method, path, data))
        return {
            "user": {
                "username": "alice",
                "is_active": True,
                "is_deleted": False,
            }
        }

    monkeypatch.setattr(ttdel, "_api_request", fake_api_request)

    assert run_ttdel(_config(), tmp_path / "alice", dry_run=True) == 0
    assert calls == [("GET", "/api/users/alice", None)]
    assert "would ask before deactivating user: alice" in capsys.readouterr().out


def test_ttdel_rejects_invalid_current_directory_before_api(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def unexpected_api_request(*args, **kwargs):
        raise AssertionError("API must not be called for an invalid username")

    monkeypatch.setattr(ttdel, "_api_request", unexpected_api_request)

    assert run_ttdel(_config(), tmp_path / "bad!") == 1


def test_ttdel_reports_server_active_recorder_refusal(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def fake_api_request(config, method, path, data=None):
        if method == "GET":
            return {
                "user": {
                    "username": "alice",
                    "is_active": True,
                    "is_deleted": False,
                }
            }
        raise ApiError("Active recorder is registered for alice", 409)

    monkeypatch.setattr(ttdel, "_api_request", fake_api_request)
    monkeypatch.setattr(ttdel, "_confirm_deactivation", lambda username: True)

    assert run_ttdel(_config(), tmp_path / "alice") == 1
    assert "Active recorder is registered for alice" in capsys.readouterr().out
