from datetime import datetime, timedelta

from modules.db_setup import create_connection, initialize_db
from modules.supervisor_status import SupervisorStatusManager


def test_stopped_supervisor_is_disconnected_even_with_fresh_heartbeat(tmp_path) -> None:
    database = tmp_path / "supervisor-status.db"
    conn = create_connection(database)
    initialize_db(conn)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO supervisor_status
        (pid, started_at, last_heartbeat, status, version, stats_json, config_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (1234, now, now, "stopped", "test", "{}", "test-hash"),
    )
    conn.commit()
    conn.close()

    status = SupervisorStatusManager.get_supervisor_status(
        {"database": {"path": str(database)}}
    )

    assert status["status"] == "disconnected"
    assert status["supervisor_status"] == "stopped"
    assert status["message"] == "Supervisor reported stopped"


def test_active_supervisor_with_fresh_heartbeat_is_connected(tmp_path) -> None:
    database = tmp_path / "supervisor-status.db"
    conn = create_connection(database)
    initialize_db(conn)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO supervisor_status
        (pid, started_at, last_heartbeat, status, version, stats_json, config_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (1234, now, now, "active", "test", "{}", "test-hash"),
    )
    conn.commit()
    conn.close()

    status = SupervisorStatusManager.get_supervisor_status(
        {"database": {"path": str(database)}}
    )

    assert status["status"] == "connected"
    assert status["supervisor_status"] == "active"


def test_active_supervisor_with_three_missed_heartbeats_is_disconnected(tmp_path) -> None:
    database = tmp_path / "supervisor-status.db"
    conn = create_connection(database)
    initialize_db(conn)
    now = datetime.now()
    stale_heartbeat = (now - timedelta(seconds=91)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO supervisor_status
        (pid, started_at, last_heartbeat, status, version, stats_json, config_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1234,
            now.strftime("%Y-%m-%d %H:%M:%S"),
            stale_heartbeat,
            "active",
            "test",
            "{}",
            "test-hash",
        ),
    )
    conn.commit()
    conn.close()

    status = SupervisorStatusManager.get_supervisor_status(
        {
            "database": {"path": str(database)},
            "intervals": {"supervisor_heartbeat_interval": 30},
        }
    )

    assert status["status"] == "disconnected"
