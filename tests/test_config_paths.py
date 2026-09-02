import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

from utils.config_paths import absolute_config_path, normalize_config_paths


def test_config_symlink_keeps_its_lexical_parent_as_path_base(tmp_path: Path) -> None:
    public_dir = tmp_path / "public"
    private_dir = tmp_path / "config-private"
    public_dir.mkdir()
    private_dir.mkdir()
    target = private_dir / "config.yaml"
    target.write_text("cookies: {}\n", encoding="utf-8")
    config_link = public_dir / "config.yaml"
    config_link.symlink_to(target)

    normalized = normalize_config_paths(
        {
            "database": {"path": "./data/db.sqlite"},
            "cookies": {"cookie_json_file": "./private/cookies_full.json"},
        },
        str(config_link),
    )

    assert absolute_config_path(config_link) == str(config_link)
    assert normalized["database"]["path"] == str(public_dir / "data" / "db.sqlite")
    assert normalized["cookies"]["cookie_json_file"] == str(
        public_dir / "private" / "cookies_full.json"
    )


def test_normalize_config_paths_anchors_all_runtime_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "deployment" / "config.yaml"
    config_path.parent.mkdir()
    absolute_cookie = tmp_path / "shared" / "cookies.json"
    config = {
        "paths": {
            "recordings_path": "./recordings",
            "recordings_fav_path": "./favorites",
            "inactive_users_path": "./inactive",
            "log_path": "./logs",
        },
        "database": {"path": "./data/db.sqlite"},
        "persistent_live_system": {
            "metadata_path": "./metadata",
            "compressed_output_path": "./compressed",
            "lock_file_path": "./run/supervisor.lock",
            "recorder_log_path": "./recorder-logs",
        },
        "selenium": {
            "database_path": "./data/selenium.sqlite",
            "chrome_profile_path": "./chrome-profile",
            "cache_dir": "./cache",
            "screenshots_dir": "./screenshots",
        },
        "telegram": {
            "notifications": {"state_file": "./state/telegram.json"},
            "upload": {"session_path": "./private"},
        },
        "cookies": {"cookie_json_file": str(absolute_cookie)},
        "logging": {"supervisor_log_file": "supervisor.log"},
    }

    normalized = normalize_config_paths(config, str(config_path))
    base = config_path.parent

    assert normalized["paths"] == {
        "recordings_path": str(base / "recordings"),
        "recordings_fav_path": str(base / "favorites"),
        "inactive_users_path": str(base / "inactive"),
        "log_path": str(base / "logs"),
    }
    assert normalized["database"]["path"] == str(base / "data" / "db.sqlite")
    assert normalized["persistent_live_system"] == {
        "metadata_path": str(base / "metadata"),
        "compressed_output_path": str(base / "compressed"),
        "lock_file_path": str(base / "run" / "supervisor.lock"),
        "recorder_log_path": str(base / "recorder-logs"),
    }
    assert normalized["selenium"]["database_path"] == str(
        base / "data" / "selenium.sqlite"
    )
    assert normalized["selenium"]["chrome_profile_path"] == str(
        base / "chrome-profile"
    )
    assert normalized["selenium"]["cache_dir"] == str(base / "cache")
    assert normalized["selenium"]["screenshots_dir"] == str(base / "screenshots")
    assert normalized["telegram"]["notifications"]["state_file"] == str(
        base / "state" / "telegram.json"
    )
    assert normalized["telegram"]["upload"]["session_path"] == str(base / "private")
    assert normalized["cookies"]["cookie_json_file"] == str(absolute_cookie)
    assert normalized["logging"]["supervisor_log_file"] == "supervisor.log"


def test_db_helper_uses_database_from_explicit_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "deployment"
    database_path = config_dir / "data" / "db.sqlite"
    log_dir = config_dir / "logs"
    database_path.parent.mkdir(parents=True)
    log_dir.mkdir()
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE users (username TEXT PRIMARY KEY, is_live INTEGER NOT NULL)"
    )
    connection.execute("INSERT INTO users VALUES ('alice', 1)")
    connection.commit()
    connection.close()

    config_path = config_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "database": {"path": "./data/db.sqlite"},
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/db_helper.py",
            "--config",
            str(config_path),
            "get_is_live",
            "alice",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "1"
    assert (log_dir / "aplikacja.log").is_file()
