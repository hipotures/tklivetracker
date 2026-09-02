import sqlite3
from pathlib import Path

from web_monitor.app import create_app


def _app_with_legacy_scheduler_stats(enabled: bool, tmp_path: Path):
    app = create_app(read_only=False)
    app.config.update(
        TESTING=True,
        DATABASE=str(tmp_path / "db.sqlite"),
        CONFIG={
            "web_monitor": {
                "legacy_scheduler_stats_enabled": enabled,
            },
            "intervals": {
                "min_user_interval": 2,
            },
            "persistent_live_system": {
                "max_live_processes": 100,
            },
        },
    )
    return app


def _create_stats_db(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            check_interval INTEGER NOT NULL DEFAULT 300,
            next_check TEXT,
            is_live INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_favorite INTEGER NOT NULL DEFAULT 0,
            total_lives INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE live_processes (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            pid INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        INSERT INTO users (
            username, check_interval, next_check, is_live,
            is_active, is_favorite, total_lives
        ) VALUES (
            'alice', 300, NULL, 0, 1, 0, 0
        );
        """
    )
    connection.commit()
    connection.close()


def test_dashboard_hides_legacy_scheduler_stats_when_disabled(tmp_path: Path) -> None:
    app = _app_with_legacy_scheduler_stats(False, tmp_path)

    response = app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "System Overview" in html
    assert "Newest Users" in html
    assert "Recent Live Users" in html
    assert "Live Activity Analytics" in html
    assert "Live Sessions" in html
    assert "New Users" in html

    assert "Est. Cycle Time" not in html
    assert "Delay Analysis" not in html
    assert "Users with Max Delays" not in html
    assert "Check Interval Distribution" not in html
    assert "Users with Interval" not in html
    assert '<option value="check_interval">Interval</option>' not in html
    assert '<option value="interval">Check Interval</option>' not in html
    assert '<option value="next_check">Next Check</option>' not in html
    assert '<th scope="col" title="Check Interval (seconds)">Interval</th>' not in html
    assert 'id="edit-check-interval"' not in html
    assert '<th scope="col" title="Next Check (countdown)">Next</th>' not in html
    assert "users-table-no-scheduler" in html
    assert 'id="quick-add-form"' not in html
    assert '"legacySchedulerStatsEnabled": false' in html


def test_dashboard_can_restore_legacy_scheduler_stats(tmp_path: Path) -> None:
    app = _app_with_legacy_scheduler_stats(True, tmp_path)

    response = app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Est. Cycle Time" in html
    assert "Delay Analysis" in html
    assert "Users with Max Delays" in html
    assert "Check Interval Distribution" in html
    assert "Users with Interval" in html
    assert '<option value="check_interval">Interval</option>' in html
    assert '<option value="interval">Check Interval</option>' in html
    assert '<option value="next_check">Next Check</option>' in html
    assert '<th scope="col" title="Check Interval (seconds)">Interval</th>' in html
    assert 'id="edit-check-interval"' in html
    assert '<th scope="col" title="Next Check (countdown)">Next</th>' in html
    assert "users-table-no-scheduler" not in html
    assert 'id="quick-add-form"' in html
    assert 'maxlength="25"' in html
    assert '"legacySchedulerStatsEnabled": true' in html


def test_standalone_quick_add_route_is_removed(tmp_path: Path) -> None:
    app = _app_with_legacy_scheduler_stats(True, tmp_path)

    response = app.test_client().get("/u")

    assert response.status_code == 404


def test_stats_api_reports_legacy_scheduler_stats_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    _create_stats_db(db_path)
    app = _app_with_legacy_scheduler_stats(False, tmp_path)

    response = app.test_client().get("/api/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["config"]["legacy_scheduler_stats_enabled"] is False
