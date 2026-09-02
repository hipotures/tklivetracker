import pytest

from selenium_supervisor import SeleniumSupervisor


@pytest.mark.asyncio
async def test_room_id_alert_is_deduplicated_until_recorder_starts() -> None:
    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.room_id_unavailable_alerted_users = set()
    sent_alerts = []
    supervisor._send_room_id_unavailable_notification = (
        lambda username: sent_alerts.append(username) or True
    )

    supervisor._on_room_id_unavailable("madie_sky_hunter", "error")
    supervisor._on_room_id_unavailable("madie_sky_hunter", "error")

    assert sent_alerts == ["madie_sky_hunter"]

    await supervisor._on_recorder_started(
        "madie_sky_hunter",
        pid=123,
        process_id=456,
        recording_file_path="recording.mp4",
    )
    supervisor._on_room_id_unavailable("madie_sky_hunter", "error")

    assert sent_alerts == ["madie_sky_hunter", "madie_sky_hunter"]


def test_room_id_alert_retries_when_no_dashboard_received_it() -> None:
    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.room_id_unavailable_alerted_users = set()
    sent_alerts = []
    supervisor._send_room_id_unavailable_notification = (
        lambda username: sent_alerts.append(username) or False
    )

    supervisor._on_room_id_unavailable("madie_sky_hunter", "error")
    supervisor._on_room_id_unavailable("madie_sky_hunter", "error")

    assert sent_alerts == ["madie_sky_hunter", "madie_sky_hunter"]
