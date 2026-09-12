import os
import sqlite3
from pathlib import Path

from web_monitor.app import create_app


def _create_database(path: Path, is_active: int = 1) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            check_interval INTEGER NOT NULL DEFAULT 300,
            is_live INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            total_lives INTEGER NOT NULL DEFAULT 0,
            next_check TEXT,
            added_at TEXT,
            last_deactivated_at TEXT,
            is_favorite INTEGER NOT NULL DEFAULT 0,
            notifications_enabled INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE lives (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            started_at TEXT,
            ended_at TEXT
        );

        CREATE TABLE live_processes (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    connection.execute(
        "INSERT INTO users (username, is_active) VALUES (?, ?)",
        ("alice", is_active),
    )
    connection.commit()
    connection.close()


def _create_test_app(tmp_path: Path, is_active: int = 1):
    db_path = tmp_path / "users.db"
    recordings_path = tmp_path / "recordings"
    favorites_path = tmp_path / "recordings_fav"
    inactive_path = tmp_path / "inactive"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
paths:
  recordings_path: {recordings_path}
  recordings_fav_path: {favorites_path}
  inactive_users_path: {inactive_path}
database:
  path: {db_path}
persistent_live_system:
  compressed_output_path: {recordings_path}
web_monitor:
  allowed_origins: []
""",
        encoding="utf-8",
    )
    _create_database(db_path, is_active=is_active)
    app = create_app(read_only=False, config_path=str(config_path))
    app.config["TESTING"] = True
    return app, db_path, recordings_path, inactive_path


def _active_status(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT is_active FROM users WHERE username = 'alice'"
        ).fetchone()
    return int(row[0])


def test_deactivate_endpoint_updates_database_and_moves_recordings(
    tmp_path: Path,
) -> None:
    app, db_path, recordings_path, inactive_path = _create_test_app(tmp_path)
    user_path = recordings_path / "alice"
    user_path.mkdir(parents=True)
    (user_path / "recording.mp4").write_bytes(b"recording")

    response = app.test_client().post("/api/users/alice/deactivate", json={})

    assert response.status_code == 200
    assert response.get_json()["already_inactive"] is False
    assert _active_status(db_path) == 0
    assert not user_path.exists()
    assert (inactive_path / "alice" / "recording.mp4").read_bytes() == b"recording"


def test_deactivate_endpoint_reports_already_inactive_without_move(
    tmp_path: Path,
) -> None:
    app, db_path, recordings_path, inactive_path = _create_test_app(
        tmp_path,
        is_active=0,
    )
    user_path = recordings_path / "alice"
    user_path.mkdir(parents=True)

    response = app.test_client().post("/api/users/alice/deactivate", json={})

    assert response.status_code == 200
    assert response.get_json()["already_inactive"] is True
    assert _active_status(db_path) == 0
    assert user_path.is_dir()
    assert not inactive_path.exists()


def test_deactivate_endpoint_allows_missing_recordings_with_existing_archive(
    tmp_path: Path,
) -> None:
    app, db_path, _, inactive_path = _create_test_app(tmp_path)
    (inactive_path / "alice").mkdir(parents=True)

    response = app.test_client().post("/api/users/alice/deactivate", json={})

    assert response.status_code == 200
    assert response.get_json()["moved"] is False
    assert _active_status(db_path) == 0
    assert (inactive_path / "alice").is_dir()


def test_deactivate_endpoint_refuses_active_recorder(tmp_path: Path) -> None:
    app, db_path, recordings_path, inactive_path = _create_test_app(tmp_path)
    user_path = recordings_path / "alice"
    user_path.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO live_processes (username, is_active) VALUES ('alice', 1)"
        )

    response = app.test_client().post("/api/users/alice/deactivate", json={})

    assert response.status_code == 409
    assert "Active recorder" in response.get_json()["error"]
    assert _active_status(db_path) == 1
    assert user_path.is_dir()
    assert not inactive_path.exists()


def test_deactivate_endpoint_refuses_symlinked_recording_directory(
    tmp_path: Path,
) -> None:
    app, db_path, recordings_path, inactive_path = _create_test_app(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.mp4"
    sentinel.write_bytes(b"keep")
    recordings_path.mkdir()
    os.symlink(outside, recordings_path / "alice")

    response = app.test_client().post("/api/users/alice/deactivate", json={})

    assert response.status_code == 400
    assert _active_status(db_path) == 1
    assert sentinel.read_bytes() == b"keep"
    assert not inactive_path.exists()


def test_deactivate_endpoint_reports_missing_user(tmp_path: Path) -> None:
    app, _, _, _ = _create_test_app(tmp_path)

    response = app.test_client().post("/api/users/missing/deactivate", json={})

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_get_user_exposes_deleted_state(tmp_path: Path) -> None:
    app, _, _, _ = _create_test_app(tmp_path, is_active=-1)

    response = app.test_client().get("/api/users/alice")

    assert response.status_code == 200
    assert response.get_json()["user"]["is_deleted"] is True
