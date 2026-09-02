import pytest

from persistent_live_manager.live_process_manager import LiveProcessManager


class FakeMetadataStore:
    def get_process_by_username(self, username):
        return None


class RoomIdUnavailableManager(LiveProcessManager):
    async def _count_running_processes(self):
        return 0

    async def _start_detached_process(self, cmd, username):
        return None, None, self._room_id_unavailable_message()


def test_room_id_unavailable_detector_matches_recorder_startup_error():
    stderr_text = """
An early error occurred: Error extracting RoomID
Could not find ROOM_ID or user never live. Exiting with code 10.
"""

    assert LiveProcessManager._is_room_id_unavailable_error(stderr_text) is True


def test_room_id_unavailable_detector_ignores_unrelated_errors():
    assert LiveProcessManager._is_room_id_unavailable_error("network timeout") is False
    assert LiveProcessManager._is_room_id_unavailable_error("") is False


@pytest.mark.asyncio
async def test_room_id_unavailable_does_not_increment_startup_failures(tmp_path):
    manager = RoomIdUnavailableManager(
        FakeMetadataStore(),
        {
            "recordings_path": str(tmp_path),
            "detached_process_mode": True,
        },
    )

    result = await manager.start_live_recording("example_user")

    assert result.success is False
    assert result.error == LiveProcessManager._room_id_unavailable_message()
    assert manager.stats["startup_failures"] == 0
    assert manager.stats["room_id_unavailable"] == 1
    assert (tmp_path / "example_user").is_dir()


@pytest.mark.asyncio
async def test_room_id_unavailable_notifies_registered_callback(tmp_path):
    manager = RoomIdUnavailableManager(
        FakeMetadataStore(),
        {
            "recordings_path": str(tmp_path),
            "detached_process_mode": True,
        },
    )
    notifications = []
    manager.add_room_id_unavailable_callback(
        lambda username, error: notifications.append((username, error))
    )

    await manager.start_live_recording("example_user")

    assert notifications == [
        ("example_user", LiveProcessManager._room_id_unavailable_message())
    ]
