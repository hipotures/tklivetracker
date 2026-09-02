import re
import sqlite3
import os
from pathlib import Path

from scripts.ttdel import TtDelConfig, run_ttdel


def _create_users_db(path: Path, is_active: int = 1) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_deactivated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO users (username, is_active) VALUES (?, ?)",
        ("alice", is_active),
    )
    conn.commit()
    conn.close()


def _config(tmp_path: Path) -> TtDelConfig:
    return TtDelConfig(
        db_path=tmp_path / "users.db",
        recordings_path=tmp_path / "recordings",
        recordings_fav_path=tmp_path / "recordings_fav",
        inactive_users_path=tmp_path / "inactive_users",
        favorite_source_path=tmp_path / "compressed",
    )


def _active_status(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT is_active FROM users WHERE username = 'alice'").fetchone()[0]
    conn.close()
    return int(status)


def test_ttdel_deactivates_user_and_moves_recording_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _create_users_db(config.db_path)
    user_dir = config.recordings_path / "alice"
    user_dir.mkdir(parents=True)
    (user_dir / "recording.mp4").write_bytes(b"recording")

    exit_code = run_ttdel(config, user_dir)

    assert exit_code == 0
    assert _active_status(config.db_path) == 0
    assert not user_dir.exists()
    assert (config.inactive_users_path / "alice" / "recording.mp4").read_bytes() == b"recording"


def test_ttdel_recognizes_user_from_favorites_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _create_users_db(config.db_path)
    user_dir = config.recordings_path / "alice"
    favorite_dir = config.recordings_fav_path / "alice"
    user_dir.mkdir(parents=True)
    favorite_dir.mkdir(parents=True)

    exit_code = run_ttdel(config, favorite_dir)

    assert exit_code == 0
    assert _active_status(config.db_path) == 0
    assert (config.inactive_users_path / "alice").is_dir()


def test_ttdel_recognizes_user_from_compressed_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _create_users_db(config.db_path)
    user_dir = config.recordings_path / "alice"
    compressed_dir = config.favorite_source_path / "alice"
    user_dir.mkdir(parents=True)
    compressed_dir.mkdir(parents=True)

    exit_code = run_ttdel(config, compressed_dir)

    assert exit_code == 0
    assert _active_status(config.db_path) == 0
    assert (config.inactive_users_path / "alice").is_dir()


def test_ttdel_uses_timestamped_destination_when_inactive_directory_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _create_users_db(config.db_path)
    user_dir = config.recordings_path / "alice"
    user_dir.mkdir(parents=True)
    (user_dir / "new.mp4").write_bytes(b"new")
    existing_inactive_dir = config.inactive_users_path / "alice"
    existing_inactive_dir.mkdir(parents=True)
    (existing_inactive_dir / "old.mp4").write_bytes(b"old")

    exit_code = run_ttdel(config, user_dir)

    assert exit_code == 0
    assert _active_status(config.db_path) == 0
    assert (existing_inactive_dir / "old.mp4").read_bytes() == b"old"
    timestamped_dirs = [
        path
        for path in config.inactive_users_path.iterdir()
        if re.fullmatch(r"alice_\d{8}_\d{6}", path.name)
    ]
    assert len(timestamped_dirs) == 1
    assert (timestamped_dirs[0] / "new.mp4").read_bytes() == b"new"


def test_ttdel_dry_run_does_not_change_status_or_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _create_users_db(config.db_path)
    user_dir = config.recordings_path / "alice"
    user_dir.mkdir(parents=True)

    exit_code = run_ttdel(config, user_dir, dry_run=True)

    assert exit_code == 0
    assert _active_status(config.db_path) == 1
    assert user_dir.is_dir()
    assert not config.inactive_users_path.exists()


def test_ttdel_refuses_active_recorder_without_moving_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _create_users_db(config.db_path)
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            "CREATE TABLE live_processes (username TEXT, is_active INTEGER)"
        )
        connection.execute(
            "INSERT INTO live_processes VALUES ('alice', 1)"
        )
    user_dir = config.recordings_path / "alice"
    user_dir.mkdir(parents=True)
    recording = user_dir / "recording.mp4"
    recording.write_bytes(b"active")

    assert run_ttdel(config, user_dir) == 1
    assert _active_status(config.db_path) == 1
    assert recording.read_bytes() == b"active"
    assert not config.inactive_users_path.exists()


def test_ttdel_rejects_symlinked_recording_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _create_users_db(config.db_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.mp4"
    sentinel.write_bytes(b"keep")
    config.recordings_path.mkdir()
    os.symlink(outside, config.recordings_path / "alice")

    assert run_ttdel(config, config.recordings_path / "alice") == 1
    assert _active_status(config.db_path) == 1
    assert sentinel.read_bytes() == b"keep"


def test_ttdel_rejects_invalid_username_component(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _create_users_db(config.db_path)
    invalid_dir = config.recordings_path / "bad!"
    invalid_dir.mkdir(parents=True)

    assert run_ttdel(config, invalid_dir) == 1
    assert _active_status(config.db_path) == 1
