import sqlite3
from datetime import datetime
from pathlib import Path

import web_monitor.blueprints.analytics as analytics_module
from modules.db_setup import initialize_db
from web_monitor.app import create_app


FROZEN_NOW = datetime(2026, 9, 8, 10, 30, 0)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(
            FROZEN_NOW.year,
            FROZEN_NOW.month,
            FROZEN_NOW.day,
            FROZEN_NOW.hour,
            FROZEN_NOW.minute,
            FROZEN_NOW.second,
        )
        if tz is not None:
            return tz.fromutc(value.replace(tzinfo=tz))
        return value


def _create_analytics_database(
    path: Path,
    *,
    users: list[tuple[int, str, str]] | None = None,
    lives: list[tuple[int, int, str]] | None = None,
) -> None:
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
    if users:
        connection.executemany(
            "INSERT INTO users (id, username, added_at) VALUES (?, ?, ?)",
            users,
        )
    if lives:
        connection.executemany(
            "INSERT INTO lives (id, user_id, started_at) VALUES (?, ?, ?)",
            lives,
        )
    connection.commit()
    connection.close()


def _client_for(database: Path, monkeypatch):
    monkeypatch.setattr(analytics_module, "datetime", FrozenDateTime)
    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(database))
    return app.test_client()


def test_daily_live_analytics_counts_distinct_users_across_the_whole_period(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "analytics.db"
    _create_analytics_database(
        database,
        users=[
            (1, "alice", "2026-09-01 12:00:00"),
            (2, "bob", "2026-09-01 12:00:00"),
            (3, "carol", "2026-09-01 12:00:00"),
        ],
        lives=[
            (1, 1, "2026-09-08 00:15:00"),
            (2, 2, "2026-09-08 00:30:00"),
            (3, 1, "2026-09-08 01:15:00"),
            (4, 3, "2026-09-08 01:45:00"),
            (5, 2, "2026-09-08 02:15:00"),
        ],
    )
    client = _client_for(database, monkeypatch)

    response = client.get("/api/analytics/live-activity?view=day&date=2026-09-08")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["totals"]["total_live_sessions"] == 5
    assert payload["totals"]["total_unique_users"] == 3
    assert payload["data"][0]["unique_users"] == 2
    assert payload["data"][1]["unique_users"] == 2


def test_current_day_future_hours_are_not_presented_as_zero(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "future-hours.db"
    _create_analytics_database(
        database,
        users=[(1, "alice", "2026-09-08 08:00:00")],
        lives=[(1, 1, "2026-09-08 09:15:00")],
    )
    client = _client_for(database, monkeypatch)

    payload = client.get(
        "/api/analytics/live-activity?view=day&date=2026-09-08"
    ).get_json()

    assert len(payload["data"]) == 24
    assert payload["data"][10]["live_count"] == 0
    assert payload["data"][10]["is_future"] is False
    assert payload["data"][11]["live_count"] is None
    assert payload["data"][11]["unique_users"] is None
    assert payload["data"][11]["is_future"] is True
    assert payload["summary"]["observed_periods"] == 11
    assert payload["summary"]["average_per_period"] == 0.1


def test_recent_day_ranges_include_the_selected_current_day(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "recent.db"
    _create_analytics_database(
        database,
        users=[
            (1, "alice", "2026-09-02 08:00:00"),
            (2, "bob", "2026-09-08 09:00:00"),
        ],
        lives=[
            (1, 1, "2026-09-02 08:30:00"),
            (2, 2, "2026-09-08 09:30:00"),
        ],
    )
    client = _client_for(database, monkeypatch)

    live_7d = client.get(
        "/api/analytics/live-activity?view=7d&date=2026-09-08"
    ).get_json()
    live_30d = client.get(
        "/api/analytics/live-activity?view=30d&date=2026-09-08"
    ).get_json()
    users_7d = client.get(
        "/api/analytics/new-users?view=7d&date=2026-09-08"
    ).get_json()

    assert len(live_7d["data"]) == 7
    assert live_7d["data"][-1]["time_period"] == "2026-09-08"
    assert live_7d["data"][-1]["live_count"] == 1
    assert live_30d["data"][-1]["time_period"] == "2026-09-08"
    assert live_30d["data"][-1]["live_count"] == 1
    assert users_7d["data"][-1]["new_users_count"] == 1


def test_three_month_view_has_continuous_monday_week_buckets(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "weekly.db"
    _create_analytics_database(
        database,
        users=[(1, "alice", "2026-07-01 08:00:00")],
        lives=[
            (1, 1, "2026-07-01 08:30:00"),
            (2, 1, "2026-07-20 08:30:00"),
        ],
    )
    client = _client_for(database, monkeypatch)

    payload = client.get(
        "/api/analytics/live-activity?view=3m&date=2026-09-08"
    ).get_json()

    assert payload["view_type"] == "3m"
    assert len(payload["data"]) >= 13
    assert any(item["live_count"] == 0 for item in payload["data"] if not item["is_future"])
    for item in payload["data"]:
        bucket_date = datetime.strptime(item["time_period"], "%Y-%m-%d")
        assert bucket_date.weekday() == 0
        assert item["display_label"].startswith("Week ")


def test_twelve_month_view_uses_monthly_buckets_instead_of_365_daily_points(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "year.db"
    _create_analytics_database(
        database,
        users=[
            (1, "alice", "2025-10-10 08:00:00"),
            (2, "bob", "2026-09-08 09:00:00"),
        ],
        lives=[
            (1, 1, "2025-10-10 08:30:00"),
            (2, 2, "2026-09-08 09:30:00"),
        ],
    )
    client = _client_for(database, monkeypatch)

    live = client.get(
        "/api/analytics/live-activity?view=12m&date=2026-09-08"
    ).get_json()
    users = client.get(
        "/api/analytics/new-users?view=12m&date=2026-09-08"
    ).get_json()

    assert len(live["data"]) == 12
    assert len(users["data"]) == 12
    assert live["period"]["aggregation"] == "month"
    assert users["period"]["aggregation"] == "month"
    assert live["totals"]["total_live_sessions"] == 2
    assert users["totals"]["total_new_users"] == 2


def test_legacy_last_year_view_maps_to_current_twelve_month_period(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "legacy.db"
    _create_analytics_database(database)
    client = _client_for(database, monkeypatch)

    payload = client.get(
        "/api/analytics/live-activity?view=last365d&date=2026-09-08"
    ).get_json()

    assert payload["view_type"] == "12m"
    assert len(payload["data"]) == 12


def test_three_month_navigation_normalizes_to_month_boundaries(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "navigation.db"
    _create_analytics_database(
        database,
        users=[(1, "alice", "2026-01-01 08:00:00")],
        lives=[(1, 1, "2026-01-01 08:30:00")],
    )
    client = _client_for(database, monkeypatch)

    for endpoint in ["live-activity", "new-users"]:
        response = client.get(
            f"/api/analytics/{endpoint}?view=3m&date=2026-08-31"
        )
        payload = response.get_json()

        assert response.status_code == 200
        assert payload["date"] == "2026-08-01"
        assert payload["navigation"]["prev"] == "2026-05-01"
        assert payload["navigation"]["next"] == "2026-11-01"
        assert payload["navigation"]["has_next"] is True


def test_analytics_frontend_uses_period_model_and_discrete_charts() -> None:
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

    for value in ["day", "7d", "30d", "3m", "12m"]:
        assert f'<option value="{value}"' in template

    assert "Last 24h" not in template
    assert "Last 30d" not in template
    assert "Last 1y" not in template
    assert "Live Starts" in template
    assert "Tracked Users Added" in template
    assert "Distinct Users" in template
    assert "const allowedAnalyticsViews = ['day', '7d', '30d', '3m', '12m'];" in preferences
    assert "type: 'bar'" in analytics
    assert "fill: false" in analytics
    assert "tension: 0.15" in analytics
    assert "tension: 0.4" not in analytics
    assert "this.analyticsNavigation?.prev" in analytics
    assert "this.analyticsNavigation?.next" in analytics


def test_analytics_timestamp_indexes_are_created() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        initialize_db(connection)
        indexes = {
            row[1]
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()

    assert "idx_lives_started_at" in indexes
    assert "idx_users_added_at" in indexes
