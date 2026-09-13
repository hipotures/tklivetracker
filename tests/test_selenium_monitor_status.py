import sqlite3
from datetime import datetime, timedelta

from modules.selenium_monitor_status import SeleniumMonitorStatusManager
from scripts.selenium_live_monitor import SeleniumLiveDB
from web_monitor.app import create_app


def write_monitor_status(database, status="active", stage="waiting_next_cycle") -> None:
    monitor_db = SeleniumLiveDB(str(database))
    now = datetime.now().replace(microsecond=0)
    monitor_db.update_monitor_status(
        pid=1234,
        started_at=now,
        last_progress=now,
        status=status,
        stage=stage,
    )


def test_fresh_active_monitor_heartbeat_is_connected(tmp_path) -> None:
    database = tmp_path / "selenium-live.db"
    write_monitor_status(database)

    result = SeleniumMonitorStatusManager.get_monitor_status(
        {"selenium": {"database_path": str(database)}}
    )

    assert result["status"] == "connected"
    assert result["monitor_status"] == "active"
    assert result["stage"] == "waiting_next_cycle"


def test_stopped_monitor_is_disconnected_with_fresh_heartbeat(tmp_path) -> None:
    database = tmp_path / "selenium-live.db"
    write_monitor_status(database, status="stopped", stage="stopped")

    result = SeleniumMonitorStatusManager.get_monitor_status(
        {"selenium": {"database_path": str(database)}}
    )

    assert result["status"] == "disconnected"
    assert result["message"] == "Live monitor reported stopped"


def test_stale_monitor_heartbeat_is_disconnected(tmp_path) -> None:
    database = tmp_path / "selenium-live.db"
    write_monitor_status(database)
    stale_time = (datetime.now() - timedelta(seconds=31)).isoformat()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE selenium_monitor_status SET last_heartbeat = ? WHERE id = 1",
            (stale_time,),
        )

    result = SeleniumMonitorStatusManager.get_monitor_status(
        {
            "selenium": {
                "database_path": str(database),
                "status_heartbeat_interval": 5,
            }
        }
    )

    assert result["status"] == "disconnected"
    assert result["last_seen_seconds"] >= 30


def test_missing_monitor_heartbeat_is_disconnected(tmp_path) -> None:
    result = SeleniumMonitorStatusManager.get_monitor_status(
        {"selenium": {"database_path": str(tmp_path / "missing.db")}}
    )

    assert result == {
        "status": "disconnected",
        "message": "No live monitor heartbeat found",
        "last_seen_seconds": None,
    }


def test_monitor_status_endpoint_reports_monitor_health(monkeypatch) -> None:
    import web_monitor.blueprints.api as api_module

    monkeypatch.setattr(
        api_module.SeleniumMonitorStatusManager,
        "get_monitor_status",
        lambda config: {
            "status": "warning",
            "message": "heartbeat delayed",
            "last_seen_seconds": 18,
        },
    )
    app = create_app(read_only=False)

    response = app.test_client().get("/api/monitor-status")

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "status": "warning",
        "message": "heartbeat delayed",
        "last_seen_seconds": 18,
    }
