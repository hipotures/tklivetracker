import logging
import sqlite3
from pathlib import Path

from web_monitor.app import create_app


def _create_web_favorite_db(db_path: Path, favorite: int = 0, active: int = 1) -> None:
    connection = sqlite3.connect(db_path)
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
        """
        INSERT INTO users (
            username, check_interval, is_live, is_active,
            total_lives, next_check, added_at, is_favorite, notifications_enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("alice", 300, 0, active, 0, None, "2026-01-01 00:00:00", favorite, 0),
    )
    connection.commit()
    connection.close()


def _app_with_paths(
    db_path: Path,
    recordings_path: Path,
    fav_path: Path,
    favorite_source_path: Path | None = None,
):
    app = create_app(read_only=False)
    app.config.update(
        TESTING=True,
        DATABASE=str(db_path),
        RECORDINGS_PATH=str(recordings_path),
        RECORDINGS_FAV_PATH=str(fav_path),
        FAVORITE_SOURCE_PATH=str(favorite_source_path or recordings_path),
    )
    return app


def test_favorite_endpoint_syncs_favorite_symlink(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)
    _create_web_favorite_db(db_path, favorite=0)

    response = _app_with_paths(db_path, recordings_path, fav_path).test_client().put(
        "/api/users/alice/favorite",
        json={"is_favorite": True},
    )

    assert response.status_code == 200
    assert (fav_path / "alice").is_symlink()
    assert (fav_path / "alice").resolve() == (recordings_path / "alice").resolve()


def test_favorite_endpoint_uses_separate_favorite_source(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    recordings_path = tmp_path / "recordings"
    compressed_path = tmp_path / "compressed"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)
    (compressed_path / "alice").mkdir(parents=True)
    _create_web_favorite_db(db_path, favorite=0)

    response = _app_with_paths(
        db_path,
        recordings_path,
        fav_path,
        favorite_source_path=compressed_path,
    ).test_client().put(
        "/api/users/alice/favorite",
        json={"is_favorite": True},
    )

    assert response.status_code == 200
    assert (fav_path / "alice").resolve() == (compressed_path / "alice").resolve()


def test_favorite_endpoint_does_not_log_noop_sync_at_info(tmp_path: Path, caplog) -> None:
    db_path = tmp_path / "users.db"
    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)
    fav_path.mkdir()
    (fav_path / "alice").symlink_to("../recordings/alice", target_is_directory=True)
    _create_web_favorite_db(db_path, favorite=1)
    app = _app_with_paths(db_path, recordings_path, fav_path)

    with caplog.at_level(logging.INFO, logger=app.logger.name):
        response = app.test_client().put(
            "/api/users/alice/favorite",
            json={"is_favorite": True},
        )

    assert response.status_code == 200
    assert "Favorite link sync" not in caplog.text


def test_favorite_endpoint_logs_database_favorite_change(tmp_path: Path, caplog) -> None:
    db_path = tmp_path / "users.db"
    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)
    _create_web_favorite_db(db_path, favorite=1)
    app = _app_with_paths(db_path, recordings_path, fav_path)

    with caplog.at_level(logging.INFO, logger=app.logger.name):
        response = app.test_client().put(
            "/api/users/alice/favorite",
            json={"is_favorite": False},
        )

    assert response.status_code == 200
    assert "Favorite disabled: alice" in caplog.text


def test_user_update_endpoint_syncs_favorite_symlink(tmp_path: Path, caplog) -> None:
    db_path = tmp_path / "users.db"
    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)
    _create_web_favorite_db(db_path, favorite=1)
    fav_path.mkdir()
    (fav_path / "alice").symlink_to(recordings_path / "alice", target_is_directory=True)
    app = _app_with_paths(db_path, recordings_path, fav_path)

    with caplog.at_level(logging.INFO, logger=app.logger.name):
        response = app.test_client().put(
            "/api/users/alice",
            json={"is_favorite": False},
        )

    assert response.status_code == 200
    assert not (fav_path / "alice").exists()
    assert "Favorite disabled: alice" in caplog.text


def test_user_update_endpoint_activation_syncs_existing_favorite(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)
    _create_web_favorite_db(db_path, favorite=1, active=0)

    response = _app_with_paths(db_path, recordings_path, fav_path).test_client().put(
        "/api/users/alice",
        json={"is_active": True},
    )

    assert response.status_code == 200
    assert (fav_path / "alice").is_symlink()
    assert (fav_path / "alice").resolve() == (recordings_path / "alice").resolve()


def test_delete_user_endpoint_removes_favorite_symlink(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)
    _create_web_favorite_db(db_path, favorite=1)
    fav_path.mkdir()
    (fav_path / "alice").symlink_to(recordings_path / "alice", target_is_directory=True)

    response = _app_with_paths(db_path, recordings_path, fav_path).test_client().delete(
        "/api/users/alice",
    )

    assert response.status_code == 200
    assert not (fav_path / "alice").is_symlink()
    assert not (recordings_path / "alice").exists()
