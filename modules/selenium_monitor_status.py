"""Read Selenium live-monitor health from its SQLite heartbeat."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from utils.config_paths import resolve_path


class SeleniumMonitorStatusManager:
    """Translate the monitor's persisted heartbeat into a UI status."""

    DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5
    DEFAULT_HANG_TIMEOUT_SECONDS = 180
    DEFAULT_HANG_GRACE_SECONDS = 15

    @classmethod
    def get_monitor_status(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        selenium_config = config.get("selenium", {})
        database_path = resolve_path(
            selenium_config.get("database_path")
            or selenium_config.get("db_path")
            or "./selenium_live.db"
        )

        if not database_path or not Path(database_path).is_file():
            return {
                "status": "disconnected",
                "message": "No live monitor heartbeat found",
                "last_seen_seconds": None,
            }

        try:
            with sqlite3.connect(database_path, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT pid, started_at, last_heartbeat, last_progress,
                           status, stage
                    FROM selenium_monitor_status
                    WHERE id = 1
                    """
                ).fetchone()
        except sqlite3.Error as error:
            if "no such table" in str(error).lower():
                return {
                    "status": "disconnected",
                    "message": "No live monitor heartbeat found",
                    "last_seen_seconds": None,
                }
            return {
                "status": "error",
                "message": f"Monitor database error: {error}",
                "last_seen_seconds": None,
            }

        if row is None:
            return {
                "status": "disconnected",
                "message": "No live monitor heartbeat found",
                "last_seen_seconds": None,
            }

        try:
            now = datetime.now()
            last_heartbeat = datetime.fromisoformat(str(row["last_heartbeat"]))
            last_progress = datetime.fromisoformat(str(row["last_progress"]))
            heartbeat_age = max(0.0, (now - last_heartbeat).total_seconds())
            progress_age = max(0.0, (now - last_progress).total_seconds())
        except (TypeError, ValueError) as error:
            return {
                "status": "error",
                "message": f"Invalid live monitor heartbeat: {error}",
                "last_seen_seconds": None,
            }

        heartbeat_interval = cls._positive_number(
            selenium_config.get("status_heartbeat_interval"),
            cls.DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        )
        hang_timeout = cls._positive_number(
            selenium_config.get("hang_timeout_seconds"),
            cls.DEFAULT_HANG_TIMEOUT_SECONDS,
        )
        hang_grace = cls._positive_number(
            selenium_config.get("hang_grace_seconds"),
            cls.DEFAULT_HANG_GRACE_SECONDS,
        )
        warning_after = max(heartbeat_interval * 3, 15)
        disconnected_after = max(heartbeat_interval * 6, 30)
        stored_status = row["status"]

        if stored_status != "active":
            status = "disconnected"
            message = f"Live monitor reported {stored_status}"
        elif heartbeat_age >= disconnected_after:
            status = "disconnected"
            message = f"No live monitor heartbeat for {int(heartbeat_age)} seconds"
        elif heartbeat_age >= warning_after:
            status = "warning"
            message = f"Live monitor heartbeat is {int(heartbeat_age)} seconds old"
        elif progress_age >= hang_timeout + hang_grace:
            status = "disconnected"
            message = f"Live monitor stalled at {row['stage']}"
        elif progress_age >= hang_timeout:
            status = "warning"
            message = f"Live monitor may be stalled at {row['stage']}"
        else:
            status = "connected"
            message = f"Active: {row['stage']}"

        return {
            "status": status,
            "message": message,
            "last_seen_seconds": int(heartbeat_age),
            "pid": row["pid"],
            "started_at": row["started_at"],
            "last_heartbeat": row["last_heartbeat"],
            "last_progress": row["last_progress"],
            "monitor_status": stored_status,
            "stage": row["stage"],
        }

    @staticmethod
    def _positive_number(value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default
