from pathlib import Path

import pytest

import selenium_supervisor
from selenium_supervisor import SeleniumSupervisor, get_web_monitor_local_base_url


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("0.0.0.0", 5001, "http://127.0.0.1:5001"),
        ("0.0.0.0", 8000, "http://127.0.0.1:8000"),
        ("127.0.0.1", 5001, "http://127.0.0.1:5001"),
        ("::", 5001, "http://[::1]:5001"),
        ("::1", 5001, "http://[::1]:5001"),
    ],
)
def test_web_monitor_local_base_url(host, port, expected) -> None:
    config = {"web_monitor": {"host": host, "port": port}}
    assert get_web_monitor_local_base_url(config) == expected


def test_supervisor_callbacks_use_configured_web_monitor_port(monkeypatch) -> None:
    requested_urls = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"active_clients": 1}

    def fake_post(url, **_kwargs):
        requested_urls.append(url)
        return FakeResponse()

    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.config = {"web_monitor": {"host": "0.0.0.0", "port": 8000}}
    supervisor.logger = selenium_supervisor.logging.getLogger("callback-test")
    supervisor._should_send_notification = lambda _username: True
    monkeypatch.setattr(selenium_supervisor.requests, "post", fake_post)

    assert supervisor._send_room_id_unavailable_notification("alice") is True
    supervisor._send_live_notification("alice", "started")

    assert requested_urls == [
        "http://127.0.0.1:8000/api/events/room-id-unavailable",
        "http://127.0.0.1:8000/api/test-notification",
    ]


def test_supervisor_has_no_legacy_localhost_callback_url() -> None:
    source = Path(selenium_supervisor.__file__).read_text(encoding="utf-8")
    assert "localhost:5001" not in source
