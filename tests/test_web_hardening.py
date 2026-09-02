import sqlite3
from pathlib import Path

import pytest

from web_monitor.app import create_app
from web_monitor.services.notification_service import notification_service
from web_monitor.utils.validation import sanitize_username


def _create_users_db(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            check_interval INTEGER NOT NULL DEFAULT 300,
            is_live INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            total_lives INTEGER NOT NULL DEFAULT 0,
            next_check TEXT,
            added_at TEXT,
            is_favorite INTEGER NOT NULL DEFAULT 0,
            notifications_enabled INTEGER NOT NULL DEFAULT 0
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
            is_active INTEGER NOT NULL DEFAULT 1
        );

        INSERT INTO users (id, username) VALUES (1, 'alice');
        """
    )
    connection.commit()
    connection.close()


@pytest.fixture
def web_client(tmp_path: Path):
    db_path = tmp_path / "web.db"
    _create_users_db(db_path)

    app = create_app(read_only=False)
    app.config.update(
        TESTING=True,
        DATABASE=str(db_path),
        RECORDINGS_PATH=str(tmp_path / "recordings"),
        RECORDINGS_FAV_PATH=str(tmp_path / "favorites"),
    )
    return app.test_client()


@pytest.mark.parametrize(
    "query",
    [
        "page=not-a-number",
        "page=0",
        "per_page=101",
        "live_filter=unknown",
        "active_filter=unknown",
    ],
)
def test_users_api_rejects_invalid_query_values(web_client, query: str) -> None:
    response = web_client.get(f"/api/users?{query}")

    assert response.status_code == 400
    assert response.get_json()["error"]


@pytest.mark.parametrize("check_interval", ["fast", 1.5, True, 0, 3601])
def test_update_user_rejects_invalid_intervals(web_client, check_interval) -> None:
    response = web_client.put(
        "/api/users/alice",
        json={"check_interval": check_interval},
    )

    assert response.status_code == 400
    assert "Check interval" in response.get_json()["error"]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/users/..", None),
        ("put", "/api/users/..", {"check_interval": 300}),
        ("put", "/api/users/../favorite", {"is_favorite": True}),
        ("put", "/api/users/../notifications", {"notifications_enabled": True}),
        ("post", "/api/users/../check", {}),
    ],
)
def test_username_routes_share_canonical_validation(web_client, method, path, payload) -> None:
    response = getattr(web_client, method)(path, json=payload)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid username format"


@pytest.mark.parametrize(
    ("endpoint", "field"),
    [
        ("favorite", "is_favorite"),
        ("notifications", "notifications_enabled"),
    ],
)
def test_boolean_endpoints_reject_truthy_strings(
    web_client, endpoint: str, field: str
) -> None:
    response = web_client.put(
        f"/api/users/alice/{endpoint}",
        json={field: "false"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == f"{field} must be a boolean"


def test_username_validation_handles_non_text_input() -> None:
    assert sanitize_username(None) is None
    assert sanitize_username(123) is None


def test_create_user_rejects_non_text_username(web_client) -> None:
    response = web_client.post("/api/users", json={"username": 123})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid username format"


@pytest.mark.parametrize("username", ["a", "abc."])
def test_create_user_rejects_public_username_rule_violations(
    web_client, username
) -> None:
    response = web_client.post("/api/users", json={"username": username})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid username format"


def test_room_id_unavailable_endpoint_sends_system_event(web_client, monkeypatch) -> None:
    sent_events = []
    monkeypatch.setattr(
        notification_service,
        "send_notification_event",
        lambda *args, **kwargs: sent_events.append((args, kwargs)),
    )

    response = web_client.post(
        "/api/events/room-id-unavailable",
        json={"username": "madie_sky_hunter"},
    )

    assert response.status_code == 200
    assert sent_events == [
        (
            ("recorder_room_id_unavailable", "madie_sky_hunter"),
            {
                "status": "warning",
                "message": (
                    "madie_sky_hunter is LIVE, but recording could not start "
                    "because RoomID is unavailable."
                ),
            },
        )
    ]


def test_test_notification_route_sends_sse_event(web_client, monkeypatch) -> None:
    sent_events = []
    monkeypatch.setattr(
        notification_service,
        "send_notification_event",
        lambda *args, **kwargs: sent_events.append((args, kwargs)),
    )

    response = web_client.post(
        "/api/test-notification",
        json={"username": "leja..1", "type": "live_start", "message": "test"},
    )

    assert response.status_code == 200
    assert sent_events == [(("live_start", "leja..1"), {"message": "test"})]


def test_room_id_unavailable_endpoint_rejects_invalid_username(web_client) -> None:
    response = web_client.post(
        "/api/events/room-id-unavailable",
        json={"username": "../invalid"},
    )

    assert response.status_code == 400


def test_dashboard_markup_exposes_recovery_and_accessibility_states(web_client) -> None:
    response = web_client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'class="skip-link" href="#main-content"' in html
    assert 'id="connection-banner"' in html
    assert 'id="quick-add-form"' not in html
    assert 'role="dialog"' in html
    assert 'aria-live="polite"' in html
    assert '/static/vendor/chartjs/chart.umd.min.js' in html
    assert 'cdn.jsdelivr.net' not in html
    assert 'fonts.googleapis.com' not in html


def test_frontend_does_not_embed_user_actions_in_inline_javascript() -> None:
    project_root = Path(__file__).resolve().parent.parent
    loaded_scripts = [
        "core.js",
        "preferences.js",
        "dashboard.js",
        "users.js",
        "stats.js",
        "analytics.js",
    ]

    source = "\n".join(
        (project_root / "web_monitor" / "static" / "js" / name).read_text(
            encoding="utf-8"
        )
        for name in loaded_scripts
    )

    assert 'onclick="app.' not in source
    assert "Waiting for Chart.js" not in source
    assert "messageElement.textContent" in source
