import json
import os
import subprocess
from pathlib import Path

import scripts.ttfav as ttfav
from scripts.ttfav import TtFavConfig, run_ttfav


def _config(tmp_path: Path) -> TtFavConfig:
    return TtFavConfig(api_url="http://192.168.100.201:5001")


def test_run_ttfav_uses_api_from_exported_recordings_path(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_api_request(config, method, path, data=None):
        calls.append((config.api_url, method, path, data))
        if method == "GET":
            return {"user": {"username": "alice", "is_favorite": False}}
        return {"message": "updated", "is_favorite": True}

    monkeypatch.setattr(ttfav, "_api_request", fake_api_request)
    exported_user_dir = tmp_path / "TT" / "recordings" / "alice"
    exported_user_dir.mkdir(parents=True)

    exit_code = run_ttfav(_config(tmp_path), exported_user_dir)

    assert exit_code == 0
    assert calls == [
        (
            "http://192.168.100.201:5001",
            "GET",
            "/api/users/alice",
            None,
        ),
        (
            "http://192.168.100.201:5001",
            "PUT",
            "/api/users/alice/favorite",
            {"is_favorite": True},
        ),
    ]
    assert "favorite enabled: alice" in capsys.readouterr().out


def test_run_ttfav_del_from_arbitrary_export_path_disables_favorite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_api_request(config, method, path, data=None):
        calls.append((method, path, data))
        if method == "GET":
            return {"user": {"username": "alice", "is_favorite": True}}
        return {"message": "updated", "is_favorite": False}

    monkeypatch.setattr(ttfav, "_api_request", fake_api_request)
    exported_user_dir = tmp_path / "different" / "export" / "alice"
    exported_user_dir.mkdir(parents=True)

    assert run_ttfav(
        _config(tmp_path),
        exported_user_dir,
        requested_action="del",
    ) == 0
    assert calls[-1] == (
        "PUT",
        "/api/users/alice/favorite",
        {"is_favorite": False},
    )
    assert "favorite disabled: alice" in capsys.readouterr().out


def test_run_ttfav_del_explicitly_disables_from_recordings_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = []

    def fake_api_request(config, method, path, data=None):
        calls.append((method, path, data))
        if method == "GET":
            return {"user": {"username": "alice", "is_favorite": True}}
        return {"message": "updated", "is_favorite": False}

    monkeypatch.setattr(ttfav, "_api_request", fake_api_request)

    assert run_ttfav(
        _config(tmp_path),
        tmp_path / "TT" / "recordings" / "alice",
        requested_action="del",
    ) == 0
    assert calls[-1] == (
        "PUT",
        "/api/users/alice/favorite",
        {"is_favorite": False},
    )


def test_run_ttfav_checks_user_through_api_in_dry_run(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_api_request(config, method, path, data=None):
        calls.append((method, path, data))
        return {"user": {"username": "alice", "is_favorite": False}}

    monkeypatch.setattr(ttfav, "_api_request", fake_api_request)

    exit_code = run_ttfav(
        _config(tmp_path),
        tmp_path / "mount" / "compressed" / "alice",
        dry_run=True,
    )

    assert exit_code == 0
    assert calls == [("GET", "/api/users/alice", None)]
    assert "would enable favorite: alice" in capsys.readouterr().out


def test_run_ttfav_invalid_directory_name_does_not_call_api(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def unexpected_api_request(*args, **kwargs):
        raise AssertionError("API must not be called outside a recognized user folder")

    monkeypatch.setattr(ttfav, "_api_request", unexpected_api_request)

    assert run_ttfav(_config(tmp_path), tmp_path / "not-a-user!") == 1
    assert "not a valid TikTok username" in capsys.readouterr().out


def test_run_ttfav_existing_favorite_requires_confirmation_to_remove(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_api_request(config, method, path, data=None):
        calls.append((method, path, data))
        return {"user": {"username": "alice", "is_favorite": True}}

    monkeypatch.setattr(ttfav, "_api_request", fake_api_request)
    monkeypatch.setattr(ttfav, "_confirm_removal", lambda username: False)

    assert run_ttfav(_config(tmp_path), tmp_path / "anywhere" / "alice") == 0
    assert calls == [("GET", "/api/users/alice", None)]
    output = capsys.readouterr().out
    assert "favorite already enabled: alice" in output
    assert "favorite unchanged: alice" in output


def test_run_ttfav_existing_favorite_removes_after_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = []

    def fake_api_request(config, method, path, data=None):
        calls.append((method, path, data))
        if method == "GET":
            return {"user": {"username": "alice", "is_favorite": True}}
        return {"message": "updated", "is_favorite": False}

    monkeypatch.setattr(ttfav, "_api_request", fake_api_request)
    monkeypatch.setattr(ttfav, "_confirm_removal", lambda username: True)

    assert run_ttfav(_config(tmp_path), tmp_path / "anywhere" / "alice") == 0
    assert calls[-1] == (
        "PUT",
        "/api/users/alice/favorite",
        {"is_favorite": False},
    )


def test_run_ttfav_rejects_invalid_username_before_api_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def unexpected_api_request(*args, **kwargs):
        raise AssertionError("API must not be called for an invalid username")

    monkeypatch.setattr(ttfav, "_api_request", unexpected_api_request)

    assert run_ttfav(
        _config(tmp_path),
        tmp_path / "mount" / "recordings" / "invalid!",
    ) == 1


def test_run_ttfav_reports_user_missing_from_server_database(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def missing_user(*args, **kwargs):
        raise RuntimeError("User not found")

    monkeypatch.setattr(ttfav, "_api_request", missing_user)

    assert run_ttfav(
        _config(tmp_path),
        tmp_path / "mount" / "recordings" / "alice",
    ) == 1
    assert "ttfav failed for alice: User not found" in capsys.readouterr().out


def test_install_tt_tools_runs_as_direct_script(tmp_path: Path) -> None:
    legacy_fav = tmp_path / "home" / ".local" / "bin" / "fav"
    legacy_fav.parent.mkdir(parents=True)
    legacy_fav.write_text("legacy", encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
    })
    config_path = tmp_path / "deployment" / "config.yaml"
    config_path.parent.mkdir()
    favorite_source = tmp_path / "deployment" / "compressed"
    config_path.write_text(
        """
paths:
  recordings_path: ./recordings
  recordings_fav_path: ./recordings_fav
  inactive_users_path: ./inactive
database:
  path: ./db.sqlite
persistent_live_system:
  compressed_output_path: %s
""" % favorite_source,
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "uv", "run", "python", "scripts/install_tt_tools.py",
            "--config", str(config_path),
            "--api-url", "http://192.168.100.201:5001/",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home" / ".local" / "bin" / "ttfav").exists()
    assert (tmp_path / "home" / ".local" / "bin" / "ttdel").exists()
    assert not legacy_fav.exists()
    bin_fav_mtime = tmp_path / "home" / ".local" / "bin" / "fav-mtime"
    local_fav_mtime = tmp_path / "deployment" / "recordings_fav" / "fav-mtime"
    assert bin_fav_mtime.is_file()
    assert not bin_fav_mtime.is_symlink()
    assert os.access(bin_fav_mtime, os.X_OK)
    assert local_fav_mtime.is_file()
    assert not local_fav_mtime.is_symlink()
    assert os.access(local_fav_mtime, os.X_OK)
    installed_config_path = tmp_path / "home" / ".config" / "ttracker" / "fav.json"
    assert installed_config_path.exists()
    with installed_config_path.open(encoding="utf-8") as config_file:
        installed_config = json.load(config_file)
    assert installed_config["api_url"] == "http://192.168.100.201:5001"
    assert installed_config["favorite_source_path"] == str(favorite_source)
    assert installed_config["db_path"] == str(
        tmp_path / "deployment" / "db.sqlite"
    )


def test_install_tt_tools_remote_mode_does_not_touch_recording_paths(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
paths:
  recordings_path: /proc/ttracker-remote/recordings
  recordings_fav_path: /proc/ttracker-remote/recordings_fav
  inactive_users_path: /proc/ttracker-remote/inactive
database:
  path: /proc/ttracker-remote/db.sqlite
persistent_live_system:
  compressed_output_path: /proc/ttracker-remote/compressed
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "uv", "run", "python", "scripts/install_tt_tools.py",
            "--config", str(config_path),
            "--api-tools-only",
            "--api-url", "http://192.168.100.201:5001",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    bin_dir = tmp_path / "home" / ".local" / "bin"
    assert (bin_dir / "ttfav").is_file()
    assert (bin_dir / "ttdel").is_file()
    assert not (bin_dir / "fav-mtime").exists()
    installed_config = json.loads(
        (tmp_path / "home" / ".config" / "ttracker" / "fav.json").read_text()
    )
    assert installed_config["api_url"] == "http://192.168.100.201:5001"
