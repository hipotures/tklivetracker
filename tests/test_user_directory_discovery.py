import logging
import sqlite3

import pytest

from selenium_supervisor import SeleniumSupervisor


@pytest.mark.asyncio
async def test_directory_discovery_does_not_deactivate_user_with_missing_directory(tmp_path) -> None:
    db_path = tmp_path / "users.db"
    recordings_path = tmp_path / "recordings"
    recordings_path.mkdir()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute("INSERT INTO users (username, is_active) VALUES ('alice', 1)")
    conn.commit()
    conn.close()

    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.database_path = str(db_path)
    supervisor.config = {
        "paths": {"recordings_path": str(recordings_path)},
        "intervals": {"default_check_interval": 300},
    }
    supervisor.logger = logging.getLogger("test-directory-discovery")
    supervisor.stats = {"new_users_added": 0}

    await supervisor._discover_new_users()

    conn = sqlite3.connect(db_path)
    is_active = conn.execute(
        "SELECT is_active FROM users WHERE username = 'alice'"
    ).fetchone()[0]
    conn.close()
    assert is_active == 1
