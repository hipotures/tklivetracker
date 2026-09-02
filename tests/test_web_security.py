import os
import runpy
import sqlite3
import sys
from pathlib import Path

import pytest

from web_monitor.app import create_app, parse_arguments, resolve_server_address
from web_monitor.blueprints.api import resolve_user_recordings_directory
from web_monitor.utils.config import load_config


MUTATING_ROUTES = [
    ("post", "/api/users", {"username": "alice"}),
    ("put", "/api/users/alice", {"check_interval": 300}),
    ("delete", "/api/users/alice", None),
    ("put", "/api/users/alice/favorite", {"is_favorite": True}),
    ("put", "/api/users/alice/notifications", {"notifications_enabled": True}),
    ("post", "/api/users/alice/check", {}),
    ("post", "/api/test-notification", {}),
    ("post", "/api/events/room-id-unavailable", {"username": "alice"}),
    ("post", "/api/update_priority", {"username": "alice", "interval": 300}),
]


def test_read_only_matrix_enumerates_every_unsafe_route() -> None:
    app = create_app(read_only=True)
    unsafe = {
        (method.lower(), rule.rule)
        for rule in app.url_map.iter_rules()
        for method in rule.methods
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    enumerated = {
        ("post", "/api/users"),
        ("put", "/api/users/<username>"),
        ("delete", "/api/users/<username>"),
        ("put", "/api/users/<username>/favorite"),
        ("put", "/api/users/<username>/notifications"),
        ("post", "/api/users/<username>/check"),
        ("post", "/api/test-notification"),
        ("post", "/api/events/room-id-unavailable"),
        ("post", "/api/update_priority"),
    }
    assert unsafe == enumerated


def test_server_address_uses_config_and_backwards_compatible_defaults() -> None:
    assert resolve_server_address({}) == ("0.0.0.0", 5001)
    assert resolve_server_address(
        {"web_monitor": {"host": "192.168.1.10", "port": 8000}}
    ) == ("192.168.1.10", 8000)


def test_server_address_loads_from_configuration_file(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "web_monitor:\n  host: 0.0.0.0\n  port: 7001\n",
        encoding="utf-8",
    )
    config, *_ = load_config(config_path)
    assert resolve_server_address(config) == ("0.0.0.0", 7001)


def test_custom_config_resolves_web_monitor_paths_from_config_directory(tmp_path) -> None:
    config_dir = tmp_path / "deployment"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "database:\n"
        "  path: ./data/db.sqlite\n"
        "paths:\n"
        "  recordings_path: ./recordings\n"
        "  recordings_fav_path: ./favorites\n"
        "persistent_live_system:\n"
        "  compressed_output_path: ./compressed\n"
        "web_monitor:\n"
        "  host: 0.0.0.0\n"
        "  port: 8000\n",
        encoding="utf-8",
    )

    app = create_app(config_path=str(config_path))

    assert app.config["DATABASE"] == str(config_dir / "data" / "db.sqlite")
    assert app.config["RECORDINGS_PATH"] == str(config_dir / "recordings")
    assert app.config["RECORDINGS_FAV_PATH"] == str(config_dir / "favorites")
    assert app.config["FAVORITE_SOURCE_PATH"] == str(config_dir / "compressed")
    assert resolve_server_address(app.config["CONFIG"]) == ("0.0.0.0", 8000)


def test_cli_host_and_port_override_configuration(monkeypatch) -> None:
    monkeypatch.setattr(
        sys, "argv", ["web-monitor", "--host", "127.0.0.1", "--port", "6001"]
    )
    args = parse_arguments()
    assert resolve_server_address(
        {"web_monitor": {"host": "0.0.0.0", "port": 5001}},
        args.host,
        args.port,
    ) == ("127.0.0.1", 6001)


def test_cli_accepts_custom_config_path(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        sys, "argv", ["web-monitor", "--config", str(config_path)]
    )

    assert parse_arguments().config == str(config_path)


def test_web_monitor_cli_uses_custom_config_for_bind(monkeypatch, tmp_path) -> None:
    import web_monitor.app as web_app

    config_dir = tmp_path / "deployment"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "database:\n"
        "  path: ./data/db.sqlite\n"
        "paths:\n"
        "  recordings_path: ./recordings\n"
        "web_monitor:\n"
        "  host: 0.0.0.0\n"
        "  port: 8000\n",
        encoding="utf-8",
    )
    run_calls = []
    monkeypatch.setattr(
        web_app.Flask,
        "run",
        lambda app, **kwargs: run_calls.append((app, kwargs)),
    )
    monkeypatch.setattr(
        sys, "argv", ["web_monitor/app.py", "--config", str(config_path)]
    )

    runpy.run_path(str(Path(web_app.__file__)), run_name="__main__")

    app, run_kwargs = run_calls[0]
    assert run_kwargs == {"debug": False, "host": "0.0.0.0", "port": 8000}
    assert app.config["DATABASE"] == str(config_dir / "data" / "db.sqlite")
    assert app.config["RECORDINGS_PATH"] == str(config_dir / "recordings")


@pytest.mark.parametrize(("method", "path", "payload"), MUTATING_ROUTES)
def test_every_mutating_route_is_blocked_in_read_only_mode(method, path, payload) -> None:
    app = create_app(read_only=True)
    app.config["TESTING"] = True
    response = getattr(app.test_client(), method)(path, json=payload)

    assert response.status_code == 403
    assert response.get_json()["read_only"] is True


def test_cross_origin_write_is_rejected_but_same_origin_is_allowed() -> None:
    app = create_app(read_only=False)
    app.config["TESTING"] = True
    client = app.test_client()

    rejected = client.post(
        "/api/test-notification",
        json={},
        headers={"Origin": "https://attacker.example"},
    )
    allowed = client.post(
        "/api/test-notification",
        json={},
        headers={"Origin": "http://localhost"},
    )

    assert rejected.status_code == 403
    assert rejected.get_json()["error"] == "Cross-origin write request rejected"
    assert allowed.status_code == 200


def test_configured_external_origin_is_accepted(monkeypatch, tmp_path) -> None:
    import web_monitor.app as web_app

    monkeypatch.setattr(
        web_app,
        "load_config",
        lambda _config_path=None: (
            {"web_monitor": {"allowed_origins": ["https://tracker.example.com"]}},
            str(tmp_path / "db.sqlite"),
            str(tmp_path / "recordings"),
            str(tmp_path / "favorites"),
            str(tmp_path / "recordings"),
        ),
    )
    app = create_app(read_only=False)

    response = app.test_client().post(
        "/api/test-notification",
        json={},
        headers={"Origin": "https://tracker.example.com"},
    )

    assert response.status_code == 200


def test_forwarded_headers_do_not_bypass_origin_check() -> None:
    app = create_app(read_only=False)
    response = app.test_client().post(
        "/api/test-notification",
        json={},
        headers={
            "Origin": "https://tracker.example.com",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "tracker.example.com",
        },
    )

    assert response.status_code == 403


def test_every_http_method_and_path_registration_is_unique() -> None:
    app = create_app(read_only=False)
    registrations = []
    for rule in app.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            registrations.append((method, rule.rule))

    assert len(registrations) == len(set(registrations))
    assert registrations.count(("POST", "/api/test-notification")) == 1


def test_security_headers_are_added() -> None:
    app = create_app(read_only=True)
    response = app.test_client().get("/api/config")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_read_only_supervisor_details_hide_host_paths_and_pid(monkeypatch, tmp_path) -> None:
    import web_monitor.blueprints.api as api_module

    monkeypatch.setattr(
        api_module.SupervisorStatusManager,
        "get_supervisor_status",
        lambda config: {
            "status": "running",
            "message": "ok",
            "pid": 1234,
            "config_hash": "private-hash",
            "stats": {},
        },
    )
    app = create_app(read_only=True)
    app.config.update(
        DATABASE=str(tmp_path / "secret.db"),
        RECORDINGS_PATH=str(tmp_path / "recordings"),
        CONFIG={"paths": {"log_path": str(tmp_path / "private.log")}},
    )

    payload = app.test_client().get("/api/supervisor-details").get_json()
    serialized = str(payload)
    assert payload["supervisor"]["pid"] == "[hidden in read-only mode]"
    assert "secret.db" not in serialized
    assert "private.log" not in serialized
    assert "private-hash" not in serialized


def test_supervisor_details_reads_persistent_live_system(monkeypatch) -> None:
    import web_monitor.blueprints.api as api_module

    monkeypatch.setattr(
        api_module.SupervisorStatusManager,
        "get_supervisor_status",
        lambda config: {
            "status": "running",
            "message": "ok",
            "stats": {},
        },
    )
    app = create_app(read_only=False)
    app.config["CONFIG"] = {
        "persistent_live_system": {
            "health_check_interval": 61,
            "max_live_processes": 19,
            "detached_process_mode": True,
        }
    }

    payload = app.test_client().get("/api/supervisor-details").get_json()

    assert payload["system"]["config"]["health_check_interval"] == 61
    assert payload["system"]["config"]["max_live_processes"] == 19
    assert payload["system"]["config"]["detached_process_mode"] is True


def test_database_statistics_return_on_demand_aggregates(tmp_path) -> None:
    db_path = tmp_path / "statistics.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE lives (id INTEGER PRIMARY KEY, user_id INTEGER);
        CREATE TABLE live_processes (
            id INTEGER PRIMARY KEY,
            username TEXT,
            is_active INTEGER
        );
        INSERT INTO users (username) VALUES ('alice'), ('bob');
        INSERT INTO lives (user_id) VALUES (1), (1), (2);
        INSERT INTO live_processes (username, is_active)
        VALUES ('alice', 1), ('bob', 0);
        """
    )
    connection.commit()
    connection.close()
    app = create_app(read_only=True)
    app.config.update(TESTING=True, DATABASE=str(db_path))

    response = app.test_client().get("/api/database-statistics")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["database"]["size_bytes"] > 0
    assert payload["database"]["table_count"] == 3
    assert payload["database"]["users"] == 2
    assert payload["database"]["lives"] == 3
    assert payload["database"]["process_records"] == 2
    assert payload["database"]["active_recorders"] == 1
    assert str(db_path) not in str(payload)


def test_delete_rejects_symlinked_user_directory(web_client, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    recordings = tmp_path / "recordings"
    recordings.mkdir(exist_ok=True)
    os.symlink(outside, recordings / "alice")

    response = web_client.delete("/api/users/alice")

    assert response.status_code == 400
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("username", ["", ".", "..", "../outside", "alice/part"])
def test_delete_path_resolution_rejects_invalid_usernames(tmp_path, username) -> None:
    root = tmp_path / "recordings"
    root.mkdir()
    with pytest.raises(ValueError):
        resolve_user_recordings_directory(root, username)
    assert root.exists()


def test_delete_refuses_active_recorder(web_client, tmp_path) -> None:
    recordings = tmp_path / "recordings"
    user_dir = recordings / "alice"
    user_dir.mkdir(parents=True)
    sentinel = user_dir / "recording.mp4"
    sentinel.write_text("active", encoding="utf-8")

    with sqlite3.connect(tmp_path / "delete.db") as connection:
        connection.execute(
            "INSERT INTO live_processes (username, is_active) VALUES ('alice', 1)"
        )

    response = web_client.delete("/api/users/alice")

    assert response.status_code == 409
    assert sentinel.exists()
    assert recordings.exists()


def test_create_rejects_existing_symlinked_user_directory(web_client, tmp_path) -> None:
    outside = tmp_path / "outside-create"
    outside.mkdir()
    recordings = tmp_path / "recordings"
    recordings.mkdir(exist_ok=True)
    os.symlink(outside, recordings / "newuser")

    response = web_client.post("/api/users", json={"username": "newuser"})

    assert response.status_code == 400
    assert (recordings / "newuser").is_symlink()
    assert list(outside.iterdir()) == []


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    db_path = tmp_path / "delete.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
            is_live INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE live_processes (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE lives (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL
        );
        INSERT INTO users (username) VALUES ('alice');
        """
    )
    connection.commit()
    connection.close()
    app = create_app(read_only=False)
    app.config.update(
        TESTING=True,
        DATABASE=str(db_path),
        RECORDINGS_PATH=str(tmp_path / "recordings"),
        RECORDINGS_FAV_PATH=str(tmp_path / "favorites"),
    )
    return app.test_client()
