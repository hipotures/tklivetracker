import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from web_monitor.app import create_app


def _create_analytics_database(path: Path) -> None:
    recent = datetime.now() - timedelta(days=1)
    older = datetime.now() - timedelta(days=100)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            added_at TEXT
        );
        CREATE TABLE lives (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            started_at TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO users (id, username, added_at) VALUES (?, ?, ?)",
        [
            (1, "alice", recent.strftime("%Y-%m-%d %H:%M:%S")),
            (2, "bob", older.strftime("%Y-%m-%d %H:%M:%S")),
        ],
    )
    connection.executemany(
        "INSERT INTO lives (id, user_id, started_at) VALUES (?, ?, ?)",
        [
            (1, 1, recent.strftime("%Y-%m-%d %H:%M:%S")),
            (2, 2, older.strftime("%Y-%m-%d %H:%M:%S")),
        ],
    )
    connection.commit()
    connection.close()


def test_last_year_analytics_returns_daily_live_and_user_series(tmp_path) -> None:
    database = tmp_path / "analytics.db"
    _create_analytics_database(database)
    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(database))
    client = app.test_client()

    live_response = client.get("/api/analytics/live-activity?view=last365d")
    users_response = client.get("/api/analytics/new-users?view=last365d")
    live = live_response.get_json()
    users = users_response.get_json()

    assert live_response.status_code == 200
    assert users_response.status_code == 200
    assert live["view_type"] == "last365d"
    assert users["view_type"] == "last365d"
    assert len(live["data"]) == 365
    assert len(users["data"]) == 365
    assert live["totals"]["total_live_sessions"] == 2
    assert users["totals"]["total_new_users"] == 2
    assert live["navigation"]["has_prev"] is False
    assert live["navigation"]["has_next"] is False
    assert users["navigation"]["has_prev"] is False
    assert users["navigation"]["has_next"] is False


def test_last_year_view_is_available_and_persistable() -> None:
    project_root = Path(__file__).resolve().parent.parent
    template = (
        project_root / "web_monitor" / "templates" / "pages" / "analytics.html"
    ).read_text(encoding="utf-8")
    preferences = (
        project_root / "web_monitor" / "static" / "js" / "preferences.js"
    ).read_text(encoding="utf-8")
    analytics = (
        project_root / "web_monitor" / "static" / "js" / "analytics.js"
    ).read_text(encoding="utf-8")

    assert '<option value="last365d">Last 1y</option>' in template
    assert "'last365d'" in preferences
    assert "case 'last365d':" in analytics
    assert "displayText = 'Last 1 Year';" in analytics


def test_weekly_analytics_handles_month_end_navigation(tmp_path) -> None:
    database = tmp_path / "weekly.db"
    _create_analytics_database(database)
    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(database))
    client = app.test_client()

    for endpoint in ["live-activity", "new-users"]:
        response = client.get(
            f"/api/analytics/{endpoint}?view=weekly&date=2026-08-31"
        )
        payload = response.get_json()

        assert response.status_code == 200
        assert payload["view_type"] == "weekly"
        assert payload["navigation"]["prev"] == "2026-05-01"
        assert payload["navigation"]["next"] == "2026-11-01"


def test_weekly_frontend_navigation_normalizes_month_day() -> None:
    project_root = Path(__file__).resolve().parent.parent
    analytics = (
        project_root / "web_monitor" / "static" / "js" / "analytics.js"
    ).read_text(encoding="utf-8")

    assert analytics.count("currentDate.setDate(1);") == 2
