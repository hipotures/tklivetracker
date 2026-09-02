import os
import json
import sqlite3
import subprocess
from pathlib import Path

from scripts.ttfav import TtFavConfig, run_ttfav


def _create_users_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            is_favorite INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def _favorite_status(db_path: Path, username: str) -> int:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT is_favorite FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return int(row[0])


def test_run_ttfav_from_recordings_path_sets_user_favorite(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "users.db"
    conn = _create_users_db(db_path)
    conn.execute("INSERT INTO users (username, is_favorite) VALUES (?, ?)", ("alice", 0))
    conn.commit()
    conn.close()

    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)

    exit_code = run_ttfav(
        TtFavConfig(db_path=db_path, recordings_path=recordings_path, recordings_fav_path=fav_path),
        cwd=recordings_path / "alice",
        dry_run=False,
    )

    assert exit_code == 0
    assert _favorite_status(db_path, "alice") == 1
    assert (fav_path / "alice").is_symlink()
    assert "favorite enabled: alice" in capsys.readouterr().out


def test_run_ttfav_from_favorites_path_unsets_user_favorite(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "users.db"
    conn = _create_users_db(db_path)
    conn.execute("INSERT INTO users (username, is_favorite) VALUES (?, ?)", ("alice", 1))
    conn.commit()
    conn.close()

    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)
    fav_path.mkdir()
    (fav_path / "alice").symlink_to(recordings_path / "alice", target_is_directory=True)

    exit_code = run_ttfav(
        TtFavConfig(db_path=db_path, recordings_path=recordings_path, recordings_fav_path=fav_path),
        cwd=fav_path / "alice",
        dry_run=False,
    )

    assert exit_code == 0
    assert _favorite_status(db_path, "alice") == 0
    assert not (fav_path / "alice").exists()
    assert "favorite disabled: alice" in capsys.readouterr().out


def test_run_ttfav_dry_run_does_not_change_database_or_links(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "users.db"
    conn = _create_users_db(db_path)
    conn.execute("INSERT INTO users (username, is_favorite) VALUES (?, ?)", ("alice", 0))
    conn.commit()
    conn.close()

    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "alice").mkdir(parents=True)

    exit_code = run_ttfav(
        TtFavConfig(db_path=db_path, recordings_path=recordings_path, recordings_fav_path=fav_path),
        cwd=recordings_path / "alice",
        dry_run=True,
    )

    assert exit_code == 0
    assert _favorite_status(db_path, "alice") == 0
    assert not (fav_path / "alice").exists()
    assert "would enable favorite: alice" in capsys.readouterr().out


def test_run_ttfav_outside_configured_paths_syncs_links(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "users.db"
    conn = _create_users_db(db_path)
    conn.execute("INSERT INTO users (username, is_favorite) VALUES (?, ?)", ("alice", 1))
    conn.commit()
    conn.close()

    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    outside = tmp_path / "outside"
    (recordings_path / "alice").mkdir(parents=True)
    outside.mkdir()

    exit_code = run_ttfav(
        TtFavConfig(db_path=db_path, recordings_path=recordings_path, recordings_fav_path=fav_path),
        cwd=outside,
        dry_run=False,
    )

    assert exit_code == 0
    assert (fav_path / "alice").is_symlink()
    output = capsys.readouterr().out
    assert "syncing favorite links from database" in output
    assert "links added: alice" in output
    assert str(recordings_path) in output
    assert str(fav_path) in output


def test_run_ttfav_syncs_links_from_separate_favorite_source(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    conn = _create_users_db(db_path)
    conn.execute("INSERT INTO users (username, is_favorite) VALUES (?, ?)", ("alice", 1))
    conn.commit()
    conn.close()

    recordings_path = tmp_path / "recordings"
    compressed_path = tmp_path / "compressed"
    fav_path = tmp_path / "recordings_fav"
    outside = tmp_path / "outside"
    (recordings_path / "alice").mkdir(parents=True)
    (compressed_path / "alice").mkdir(parents=True)
    outside.mkdir()

    exit_code = run_ttfav(
        TtFavConfig(
            db_path=db_path,
            recordings_path=recordings_path,
            recordings_fav_path=fav_path,
            favorite_source_path=compressed_path,
        ),
        cwd=outside,
    )

    assert exit_code == 0
    assert (fav_path / "alice").resolve() == (compressed_path / "alice").resolve()


def test_run_ttfav_from_separate_favorite_source_sets_user_favorite(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "users.db"
    conn = _create_users_db(db_path)
    conn.execute("INSERT INTO users (username, is_favorite) VALUES (?, ?)", ("alice", 0))
    conn.commit()
    conn.close()

    recordings_path = tmp_path / "recordings"
    compressed_path = tmp_path / "compressed"
    fav_path = tmp_path / "recordings_fav"
    (compressed_path / "alice").mkdir(parents=True)

    exit_code = run_ttfav(
        TtFavConfig(
            db_path=db_path,
            recordings_path=recordings_path,
            recordings_fav_path=fav_path,
            favorite_source_path=compressed_path,
        ),
        cwd=compressed_path / "alice",
    )

    assert exit_code == 0
    assert _favorite_status(db_path, "alice") == 1
    assert (fav_path / "alice").resolve() == (compressed_path / "alice").resolve()
    assert "favorite enabled: alice" in capsys.readouterr().out


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
    assert installed_config["favorite_source_path"] == str(favorite_source)
    assert installed_config["db_path"] == str(
        tmp_path / "deployment" / "db.sqlite"
    )
