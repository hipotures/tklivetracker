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


def test_period_registry_defines_calendar_views() -> None:
    assert list(analytics_module.PERIODS) == [
        "day",
        "week",
        "month",
        "quarter",
        "year",
        "all",
    ]
    assert analytics_module.PERIODS["day"]["aggregation"] == "hour"
    assert analytics_module.PERIODS["week"]["aggregation"] == "day"
    assert analytics_module.PERIODS["month"]["aggregation"] == "day"
    assert analytics_module.PERIODS["quarter"]["aggregation"] == "week"
    assert analytics_module.PERIODS["year"]["aggregation"] == "month"
    assert analytics_module.PERIODS["all"]["aggregation"] == "month"


def test_day_counts_distinct_users_across_the_whole_period(
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
    assert payload["period"]["start"] == "2026-09-08"
    assert payload["period"]["end"] == "2026-09-08"
    assert payload["totals"]["total_live_sessions"] == 5
    assert payload["totals"]["total_unique_users"] == 3
    assert payload["data"][0]["unique_users"] == 2
    assert payload["data"][1]["unique_users"] == 2


def test_current_day_keeps_full_24_hour_axis_and_marks_future_hours_unknown(
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
    assert payload["data"][10]["time_period"] == "2026-09-08 10:00:00"
    assert payload["data"][10]["live_count"] == 0
    assert payload["data"][10]["is_future"] is False
    assert payload["data"][10]["is_partial"] is True
    assert payload["data"][11]["live_count"] is None
    assert payload["data"][11]["unique_users"] is None
    assert payload["data"][11]["is_future"] is True
    assert payload["data"][-1]["display_label"] == "23:00"
    assert payload["summary"]["observed_periods"] == 11
    assert payload["summary"]["average_periods"] == 10
    assert payload["summary"]["average_per_period"] == 0.1


def test_week_is_monday_through_sunday_and_moves_by_whole_weeks(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "week.db"
    _create_analytics_database(
        database,
        users=[
            (1, "alice", "2026-09-07 08:00:00"),
            (2, "bob", "2026-09-08 09:00:00"),
        ],
        lives=[
            (1, 1, "2026-09-07 08:30:00"),
            (2, 2, "2026-09-08 09:30:00"),
            (3, 1, "2026-08-31 08:30:00"),
        ],
    )
    client = _client_for(database, monkeypatch)

    payload = client.get(
        "/api/analytics/live-activity?view=week&date=2026-09-08"
    ).get_json()

    assert payload["date"] == "2026-09-07"
    assert payload["period"]["start"] == "2026-09-07"
    assert payload["period"]["end"] == "2026-09-13"
    assert payload["period"]["range_label"] == "Sep 7 - Sep 13, 2026"
    assert len(payload["data"]) == 7
    assert payload["data"][0]["time_period"] == "2026-09-07"
    assert payload["data"][-1]["time_period"] == "2026-09-13"
    assert payload["data"][2]["live_count"] is None
    assert payload["navigation"]["prev"] == "2026-08-31"
    assert payload["navigation"]["next"] == "2026-09-14"
    assert payload["navigation"]["has_prev"] is True
    assert payload["navigation"]["has_next"] is False


def test_month_is_complete_calendar_month_with_future_days_unknown(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "month.db"
    _create_analytics_database(
        database,
        users=[(1, "alice", "2026-09-01 08:00:00")],
        lives=[
            (1, 1, "2026-09-01 08:30:00"),
            (2, 1, "2026-08-31 08:30:00"),
        ],
    )
    client = _client_for(database, monkeypatch)

    payload = client.get(
        "/api/analytics/live-activity?view=month&date=2026-09-08"
    ).get_json()

    assert payload["date"] == "2026-09-01"
    assert payload["period"]["range_label"] == "September 2026"
    assert payload["period"]["start"] == "2026-09-01"
    assert payload["period"]["end"] == "2026-09-30"
    assert len(payload["data"]) == 30
    assert payload["data"][7]["time_period"] == "2026-09-08"
    assert payload["data"][8]["time_period"] == "2026-09-09"
    assert payload["data"][8]["live_count"] is None
    assert payload["navigation"]["prev"] == "2026-08-01"
    assert payload["navigation"]["next"] == "2026-10-01"
    assert payload["navigation"]["has_prev"] is True
    assert payload["navigation"]["has_next"] is False


def test_quarter_uses_q_boundaries_and_week_buckets_cover_full_quarter(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "quarter.db"
    _create_analytics_database(
        database,
        users=[(1, "alice", "2026-07-01 08:00:00")],
        lives=[
            (1, 1, "2026-07-01 08:30:00"),
            (2, 1, "2026-07-20 08:30:00"),
            (3, 1, "2026-06-20 08:30:00"),
        ],
    )
    client = _client_for(database, monkeypatch)

    payload = client.get(
        "/api/analytics/live-activity?view=quarter&date=2026-09-08"
    ).get_json()

    assert payload["date"] == "2026-07-01"
    assert payload["period"]["start"] == "2026-07-01"
    assert payload["period"]["end"] == "2026-09-30"
    assert payload["period"]["range_label"] == "Q3 2026 · Jul 1 - Sep 30, 2026"
    assert payload["data"][0]["time_period"] == "2026-06-29"
    assert payload["data"][0]["display_label"] == "Jul 1–Jul 5"
    assert payload["data"][-1]["time_period"] == "2026-09-28"
    assert payload["data"][-1]["display_label"] == "Sep 28–Sep 30"
    assert payload["data"][-1]["live_count"] is None
    assert payload["navigation"]["prev"] == "2026-04-01"
    assert payload["navigation"]["next"] == "2026-10-01"
    assert payload["navigation"]["has_next"] is False


def test_year_is_january_through_december_with_month_buckets(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "year.db"
    _create_analytics_database(
        database,
        users=[
            (1, "alice", "2026-01-10 08:00:00"),
            (2, "bob", "2026-09-08 09:00:00"),
        ],
        lives=[
            (1, 1, "2026-01-10 08:30:00"),
            (2, 2, "2026-09-08 09:30:00"),
            (3, 1, "2025-12-10 08:30:00"),
        ],
    )
    client = _client_for(database, monkeypatch)

    live = client.get(
        "/api/analytics/live-activity?view=year&date=2026-09-08"
    ).get_json()
    users = client.get(
        "/api/analytics/new-users?view=year&date=2026-09-08"
    ).get_json()

    assert live["date"] == "2026-01-01"
    assert live["period"]["start"] == "2026-01-01"
    assert live["period"]["end"] == "2026-12-31"
    assert live["period"]["range_label"] == "2026"
    assert len(live["data"]) == 12
    assert len(users["data"]) == 12
    assert live["data"][8]["time_period"] == "2026-09"
    assert live["data"][9]["time_period"] == "2026-10"
    assert live["data"][9]["live_count"] is None
    assert live["navigation"]["prev"] == "2025-01-01"
    assert live["navigation"]["next"] == "2027-01-01"
    assert live["navigation"]["has_prev"] is True
    assert live["navigation"]["has_next"] is False


def test_all_uses_shared_history_start_and_disables_navigation(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "all.db"
    _create_analytics_database(
        database,
        users=[
            (1, "alice", "2025-06-15 08:00:00"),
            (2, "bob", "2026-09-08 09:00:00"),
        ],
        lives=[
            (1, 1, "2025-07-10 08:30:00"),
            (2, 2, "2026-09-08 09:30:00"),
        ],
    )
    client = _client_for(database, monkeypatch)

    live = client.get("/api/analytics/live-activity?view=all").get_json()
    users = client.get("/api/analytics/new-users?view=all").get_json()

    assert live["date"] is None
    assert users["date"] is None
    assert live["period"]["start"] == "2025-06-15"
    assert users["period"]["start"] == "2025-06-15"
    assert live["period"]["end"] == "2026-09-08"
    assert live["period"]["range_label"] == (
        "All time · Jun 15, 2025 - Sep 8, 2026"
    )
    assert live["period"]["aggregation"] == "month"
    assert len(live["data"]) == 16
    assert len(users["data"]) == 16
    assert live["navigation"] == {
        "current": None,
        "has_next": False,
        "has_prev": False,
        "next": None,
        "prev": None,
    }
    assert users["navigation"] == live["navigation"]


def test_legacy_views_map_to_calendar_periods(tmp_path, monkeypatch) -> None:
    database = tmp_path / "legacy.db"
    _create_analytics_database(database)
    client = _client_for(database, monkeypatch)

    aliases = {
        "last24h": "day",
        "last7d": "week",
        "last30d": "month",
        "3m": "quarter",
        "last365d": "year",
    }
    for legacy, expected in aliases.items():
        payload = client.get(
            f"/api/analytics/live-activity?view={legacy}&date=2026-09-08"
        ).get_json()
        assert payload["view_type"] == expected


def test_analytics_frontend_uses_calendar_period_selector() -> None:
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

    for value, label in [
        ("day", "Day"),
        ("week", "Week"),
        ("month", "Month"),
        ("quarter", "Quarter"),
        ("year", "Year"),
        ("all", "All"),
    ]:
        assert f'<option value="{value}"' in template
        assert f">{label}</option>" in template

    assert "7 days" not in template
    assert "30 days" not in template
    assert "3 months" not in template
    assert "12 months" not in template
    assert "const allowedAnalyticsViews = [" in preferences
    for value in ["day", "week", "month", "quarter", "year", "all"]:
        assert f"'{value}'" in preferences

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
