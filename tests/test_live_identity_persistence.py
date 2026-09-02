import sqlite3
from datetime import datetime

from modules.db_setup import initialize_db
from modules.db_user import add_live_session
from persistent_live_manager.process_metadata_store import ProcessMetadataStore


def test_add_live_session_persists_tiktok_identity():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_db(connection)
    cursor = connection.cursor()
    cursor.execute("INSERT INTO users (username) VALUES (?)", ("alice",))
    user_id = cursor.lastrowid

    live_id = add_live_session(
        connection,
        user_id,
        started_at=datetime(2026, 7, 18, 2, 30, 0),
        tiktok_stream_id="4443200303975629693",
        tiktok_started_at=datetime(2026, 7, 18, 2, 28, 28),
        tiktok_owner_user_id="7525149453251937313",
    )

    row = connection.execute(
        """
        SELECT tiktok_stream_id, tiktok_started_at, tiktok_owner_user_id
        FROM lives
        WHERE id = ?
        """,
        (live_id,),
    ).fetchone()

    assert row["tiktok_stream_id"] == "4443200303975629693"
    assert row["tiktok_started_at"] == "2026-07-18 02:28:28"
    assert row["tiktok_owner_user_id"] == "7525149453251937313"
    assert connection.execute(
        "SELECT tt_user_id FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()[0] == "7525149453251937313"


def test_initialize_db_adds_tiktok_identity_columns_to_existing_lives_table():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE lives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            started_at TIMESTAMP,
            ended_at TIMESTAMP
        )
        """
    )

    initialize_db(connection)

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(lives)").fetchall()
    }
    assert "tiktok_stream_id" in columns
    assert "tiktok_started_at" in columns
    assert "tiktok_owner_user_id" in columns
    indexes = {
        row[1] for row in connection.execute("PRAGMA index_list(lives)").fetchall()
    }
    assert "idx_lives_user_id" in indexes
    assert "idx_lives_tiktok_owner_user_id" in indexes
    user_indexes = {
        row[1] for row in connection.execute("PRAGMA index_list(users)").fetchall()
    }
    assert "idx_users_is_favorite" in user_indexes


def test_live_owner_mismatch_keeps_user_identity_and_snapshots_live(caplog):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_db(connection)
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO users (username, tt_user_id) VALUES (?, ?)",
        ("alice", "existing-owner"),
    )
    user_id = cursor.lastrowid

    live_id = add_live_session(
        connection,
        user_id,
        tiktok_owner_user_id="different-owner",
    )

    assert connection.execute(
        "SELECT tt_user_id FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()[0] == "existing-owner"
    assert connection.execute(
        "SELECT tiktok_owner_user_id FROM lives WHERE id = ?",
        (live_id,),
    ).fetchone()[0] == "different-owner"
    assert "TikTok owner ID mismatch" in caplog.text


def test_new_live_processes_table_links_exact_live_row():
    connection = sqlite3.connect(":memory:")
    initialize_db(connection)

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(live_processes)").fetchall()
    }
    assert "live_id" in columns


def test_initialize_db_does_not_alter_existing_live_processes_table():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE live_processes (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            pid INTEGER NOT NULL
        );
        """
    )

    initialize_db(connection)

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(live_processes)").fetchall()
    }
    assert "live_id" not in columns


def test_process_metadata_links_and_closes_exact_live_row(tmp_path):
    db_path = tmp_path / "process-live.db"
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    initialize_db(connection)
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO users (username, is_live) VALUES (?, 1)",
        ("alice",),
    )
    user_id = cursor.lastrowid
    live_id = add_live_session(
        connection,
        user_id,
        tiktok_stream_id="stream-1",
        tiktok_started_at=datetime(2026, 7, 18, 2, 28, 28),
    )
    connection.close()

    store = ProcessMetadataStore(str(db_path))
    process_id = store.register_process(
        "alice",
        123,
        recording_file_path="/tmp/alice.mp4",
        live_id=live_id,
    )

    identity = store.get_process_live_identity(process_id)
    assert identity["live_id"] == live_id
    assert identity["tiktok_stream_id"] == "stream-1"
    assert store.close_process_live_session(process_id) is True

    connection = sqlite3.connect(db_path)
    ended_at = connection.execute(
        "SELECT ended_at FROM lives WHERE id = ?",
        (live_id,),
    ).fetchone()[0]
    connection.close()
    assert ended_at is not None
