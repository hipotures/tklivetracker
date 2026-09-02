import sqlite3

import pytest

from modules import db_user


def test_activating_user_does_not_create_empty_recording_directory(tmp_path) -> None:
    recordings_path = tmp_path / "recordings"
    inactive_users_path = tmp_path / "inactive_users"
    inactive_user_dir = inactive_users_path / "alice"
    inactive_user_dir.mkdir(parents=True)

    path_config = {
        "paths": {
            "recordings_path": str(recordings_path),
            "inactive_users_path": str(inactive_users_path),
        }
    }

    conn = sqlite3.connect(tmp_path / "users.db")
    conn.execute(
        """
        CREATE TABLE users (
            username TEXT PRIMARY KEY,
            is_active INTEGER NOT NULL,
            last_deactivated_at TEXT
        )
        """
    )
    conn.execute("INSERT INTO users (username, is_active) VALUES ('alice', 0)")
    conn.commit()

    db_user.set_user_active_status(conn, "alice", 1, path_config=path_config)

    is_active = conn.execute(
        "SELECT is_active FROM users WHERE username = 'alice'"
    ).fetchone()[0]
    conn.close()
    assert is_active == 1
    assert not (recordings_path / "alice").exists()
    assert inactive_user_dir.is_dir()


def test_user_activation_rejects_invalid_username_before_file_operations(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "users.db")
    with pytest.raises(ValueError, match="Invalid TikTok username"):
        db_user.set_user_active_status(conn, "../alice", 0)
    conn.close()


def test_deactivation_uses_paths_from_the_owning_config(tmp_path) -> None:
    recordings_path = tmp_path / "deployment" / "recordings"
    inactive_users_path = tmp_path / "deployment" / "inactive"
    user_dir = recordings_path / "alice"
    user_dir.mkdir(parents=True)
    (user_dir / "recording.mp4").write_bytes(b"recording")
    path_config = {
        "paths": {
            "recordings_path": str(recordings_path),
            "inactive_users_path": str(inactive_users_path),
        }
    }

    conn = sqlite3.connect(tmp_path / "users.db")
    conn.execute(
        """
        CREATE TABLE users (
            username TEXT PRIMARY KEY,
            is_active INTEGER NOT NULL,
            last_deactivated_at TEXT
        )
        """
    )
    conn.execute("INSERT INTO users (username, is_active) VALUES ('alice', 1)")
    conn.commit()

    db_user.set_user_active_status(conn, "alice", 0, path_config=path_config)

    assert not user_dir.exists()
    assert (inactive_users_path / "alice" / "recording.mp4").read_bytes() == b"recording"
    assert conn.execute(
        "SELECT is_active FROM users WHERE username = 'alice'"
    ).fetchone()[0] == 0
    conn.close()
