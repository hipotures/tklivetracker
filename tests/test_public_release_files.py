import os
import sqlite3
import stat
from pathlib import Path

import pytest
import yaml

from scripts.backup_database import (
    backup_database,
    database_path_from_config,
    main as backup_main,
)
from utils.config_paths import resolve_config_placeholders
from web_monitor.app import resolve_server_address


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_config_example_is_safe_and_loadable():
    with (PROJECT_ROOT / "config.example.yaml").open(encoding="utf-8") as stream:
        config = resolve_config_placeholders(yaml.safe_load(stream))

    assert config["paths"]["recordings_path"] == "./recordings"
    assert config["paths"]["log_path"] == "./logs"
    assert config["database"]["path"] == "./db.sqlite"
    assert config["cookies"]["cookie_json_file"] == "./private/cookies_full.json"
    assert resolve_server_address(config) == ("0.0.0.0", 5001)
    assert config["telegram"]["notifications"]["enabled"] is False
    assert config["telegram"]["notifications"]["bot_token"] == ""
    assert config["telegram"]["upload"]["api_hash"] == ""


def test_backup_database_captures_committed_wal_data(tmp_path):
    database = tmp_path / "source.sqlite"
    output_dir = tmp_path / "backup"

    source = sqlite3.connect(database)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("CREATE TABLE items (value TEXT NOT NULL)")
    source.execute("INSERT INTO items VALUES ('committed-in-wal')")
    source.commit()

    backup_path = backup_database(database, output_dir, keep=2)

    with sqlite3.connect(backup_path) as snapshot:
        assert snapshot.execute("SELECT value FROM items").fetchone() == (
            "committed-in-wal",
        )
        assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    source.close()

    if os.name == "posix":
        assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    assert not list(output_dir.glob(".tklivetracker-backup-*"))


def test_backup_database_rejects_missing_source(tmp_path, capsys):
    result = backup_main(
        [
            "--database",
            str(tmp_path / "missing.sqlite"),
            "--output-dir",
            str(tmp_path / "backup"),
        ]
    )

    assert result == 1
    assert "Backup failed:" in capsys.readouterr().out


def test_backup_database_rejects_symlink_source(tmp_path):
    database = tmp_path / "source.sqlite"
    sqlite3.connect(database).close()
    link = tmp_path / "linked.sqlite"
    try:
        link.symlink_to(database)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="regular file"):
        backup_database(link, tmp_path / "backup")


def test_backup_database_uses_configured_database_path(tmp_path):
    database = tmp_path / "data" / "main.sqlite"
    database.parent.mkdir()
    sqlite3.connect(database).close()
    config = tmp_path / "config.yaml"
    config.write_text("database:\n  path: data/main.sqlite\n", encoding="utf-8")

    assert database_path_from_config(config) == database
    assert backup_main([
        "--config", str(config), "--output-dir", str(tmp_path / "backup")
    ]) == 0


def test_backup_database_cli_path_overrides_missing_config(tmp_path):
    database = tmp_path / "explicit.sqlite"
    sqlite3.connect(database).close()

    assert backup_main([
        "--database", str(database),
        "--config", str(tmp_path / "missing.yaml"),
        "--output-dir", str(tmp_path / "backup"),
    ]) == 0


def test_backup_database_fails_when_config_is_missing(tmp_path, capsys):
    assert backup_main([
        "--config", str(tmp_path / "missing.yaml"),
        "--output-dir", str(tmp_path / "backup"),
    ]) == 1
    assert "Backup failed:" in capsys.readouterr().out
