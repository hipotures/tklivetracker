import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from web_monitor.app import create_app


def _create_dashboard_test_db(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            check_interval INTEGER NOT NULL,
            is_live INTEGER NOT NULL,
            is_active INTEGER NOT NULL,
            total_lives INTEGER NOT NULL,
            next_check TEXT,
            added_at TEXT,
            is_favorite INTEGER NOT NULL,
            notifications_enabled INTEGER NOT NULL
        );

        CREATE TABLE lives (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );

        CREATE TABLE live_processes (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            started_at TEXT,
            is_active INTEGER NOT NULL
        );
        """
    )

    connection.executemany(
        """
        INSERT INTO users (
            id, username, check_interval, is_live, is_active,
            total_lives, next_check, added_at, is_favorite, notifications_enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "nonfav_old", 300, 1, 1, 12, None, "2026-04-01 00:00:00", 0, 0),
            (2, "fav_old", 300, 1, 1, 40, None, "2026-04-01 00:00:00", 1, 0),
            (3, "fav_new", 300, 1, 1, 8, None, "2026-04-01 00:00:00", 1, 1),
            (4, "nonfav_new", 300, 1, 1, 5, None, "2026-04-01 00:00:00", 0, 0),
            (5, "fav_never_live", 300, 0, 1, 0, None, "2026-04-01 00:00:00", 1, 0),
        ],
    )

    connection.executemany(
        "INSERT INTO lives (id, user_id, started_at, ended_at) VALUES (?, ?, ?, ?)",
        [
            (1, 1, "2026-04-19 09:00:00", None),
            (2, 2, "2026-04-19 10:00:00", None),
            (3, 3, "2026-04-19 11:00:00", None),
            (4, 4, "2026-04-19 12:00:00", None),
        ],
    )

    connection.commit()
    connection.close()


def test_dashboard_live_users_sort_favorites_first_then_by_started_at(tmp_path: Path) -> None:
    db_path = tmp_path / "dashboard.db"
    _create_dashboard_test_db(db_path)

    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(db_path))

    client = app.test_client()
    response = client.get("/api/dashboard")

    assert response.status_code == 200
    assert [user["username"] for user in response.get_json()["live_users"]] == [
        "fav_new",
        "fav_old",
        "nonfav_new",
        "nonfav_old",
    ]


def test_get_single_user_returns_notification_settings(tmp_path: Path) -> None:
    db_path = tmp_path / "dashboard.db"
    _create_dashboard_test_db(db_path)

    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(db_path))

    client = app.test_client()
    response = client.get("/api/users/fav_new")

    assert response.status_code == 200
    assert response.get_json()["user"]["username"] == "fav_new"
    assert response.get_json()["user"]["notifications_enabled"] is True


def test_dashboard_live_users_include_notification_settings(tmp_path: Path) -> None:
    db_path = tmp_path / "dashboard.db"
    _create_dashboard_test_db(db_path)

    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(db_path))

    client = app.test_client()
    response = client.get("/api/dashboard")

    assert response.status_code == 200
    live_users = response.get_json()["live_users"]
    fav_new = next(user for user in live_users if user["username"] == "fav_new")
    nonfav_new = next(user for user in live_users if user["username"] == "nonfav_new")

    assert fav_new["notifications_enabled"] is True
    assert nonfav_new["notifications_enabled"] is False


def test_dashboard_distinguishes_live_detection_from_active_recording(tmp_path: Path) -> None:
    db_path = tmp_path / "dashboard.db"
    _create_dashboard_test_db(db_path)

    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO live_processes (id, username, started_at, is_active) VALUES (?, ?, ?, ?)",
        (1, "fav_new", "2026-04-19 11:01:00", 1),
    )
    connection.execute("UPDATE users SET is_active = 0 WHERE username = ?", ("nonfav_new",))
    connection.execute(
        "UPDATE lives SET started_at = ? WHERE user_id = ? AND ended_at IS NULL",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 2),
    )
    connection.commit()
    connection.close()

    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(db_path))

    response = app.test_client().get("/api/dashboard")

    assert response.status_code == 200
    live_users = {user["username"]: user for user in response.get_json()["live_users"]}
    assert live_users["fav_new"]["is_recording"] is True
    assert live_users["fav_new"]["recording_state"] == "recording"
    assert live_users["fav_old"]["is_recording"] is False
    assert live_users["fav_old"]["recording_state"] == "starting"
    assert live_users["nonfav_new"]["is_active"] is False


def test_dashboard_frontend_labels_recording_and_inactive_live_users() -> None:
    source = (
        Path(__file__).resolve().parent.parent
        / "web_monitor"
        / "static"
        / "js"
        / "dashboard.js"
    ).read_text(encoding="utf-8")

    assert "A recorder process is active" in source
    assert "Recorder startup is in progress" in source
    assert "No recorder process is active" in source
    assert "Recording is disabled for this user" in source


def test_favorites_include_days_since_last_live(tmp_path: Path) -> None:
    db_path = tmp_path / "dashboard.db"
    _create_dashboard_test_db(db_path)

    expected_days = 3
    last_live_at = (datetime.now() - timedelta(days=expected_days, hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE lives SET ended_at = ? WHERE user_id = ?",
        (last_live_at, 2),
    )
    connection.commit()
    connection.close()

    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(db_path))

    client = app.test_client()
    response = client.get("/api/favorites")

    assert response.status_code == 200
    favorites = {user["username"]: user for user in response.get_json()["favorites"]}

    assert favorites["fav_old"]["last_live_days_ago"] == expected_days
    assert favorites["fav_never_live"]["last_live_days_ago"] == -1


def test_favorites_return_each_user_once_when_live_rows_are_duplicated(tmp_path: Path) -> None:
    db_path = tmp_path / "dashboard.db"
    _create_dashboard_test_db(db_path)

    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO lives (id, user_id, started_at, ended_at) VALUES (?, ?, ?, ?)",
        (5, 2, "2026-04-19 10:05:00", None),
    )
    connection.commit()
    connection.close()

    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(db_path))

    client = app.test_client()
    response = client.get("/api/favorites")

    assert response.status_code == 200
    favorite_names = [user["username"] for user in response.get_json()["favorites"]]

    assert favorite_names.count("fav_old") == 1
    assert response.get_json()["count"] == len(set(favorite_names))


def test_users_api_supports_notification_options_in_active_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "dashboard.db"
    _create_dashboard_test_db(db_path)

    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(db_path))

    client = app.test_client()
    response = client.get("/api/users?active_filter=notifications_enabled")

    assert response.status_code == 200
    assert [user["username"] for user in response.get_json()["users"]] == ["fav_new"]
