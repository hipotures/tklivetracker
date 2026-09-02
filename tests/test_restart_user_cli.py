import pytest

from selenium_supervisor import SeleniumSupervisor


class FakeProcessInfo:
    username = "leja..1"
    pid = 12345
    room_id = "room-1"
    health_status = "healthy"


class FakeMetadataStore:
    def __init__(self, process=None, live=True):
        self.process = process
        self.live = live

    def get_process_by_username(self, username):
        assert username == "leja..1"
        return self.process

    def is_user_live(self, username):
        assert username == "leja..1"
        return self.live


class FakeProcessManager:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def restart_live_recording(self, process_info, issues):
        self.calls.append((process_info, issues))
        return self.result


class FakeStatisticsComponent:
    def __init__(self, statistics):
        self.statistics = statistics

    def get_statistics(self):
        return self.statistics.copy()


def test_supervisor_synchronizes_authoritative_component_statistics():
    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.stats = {
        "recording_stops": 0,
        "health_checks": 0,
        "restarts": 0,
    }
    supervisor.process_manager = FakeStatisticsComponent({
        "processes_stopped": 7,
        "processes_restarted": 3,
    })
    supervisor.health_monitor = FakeStatisticsComponent({
        "health_checks_performed": 41,
    })

    supervisor._synchronize_component_statistics()

    assert supervisor.stats == {
        "recording_stops": 7,
        "health_checks": 41,
        "restarts": 3,
    }


@pytest.mark.asyncio
async def test_restart_user_uses_existing_restart_manager():
    process = FakeProcessInfo()
    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.metadata_store = FakeMetadataStore(process=process, live=True)
    supervisor.process_manager = FakeProcessManager(result=True)

    ok = await supervisor.restart_user("leja..1")

    assert ok is True
    assert supervisor.process_manager.calls == [
        (process, ["manual CLI restart"])
    ]


@pytest.mark.asyncio
async def test_restart_user_refuses_when_no_active_process():
    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.metadata_store = FakeMetadataStore(process=None, live=True)
    supervisor.process_manager = FakeProcessManager(result=True)

    ok = await supervisor.restart_user("leja..1")

    assert ok is False
    assert supervisor.process_manager.calls == []


@pytest.mark.asyncio
async def test_restart_user_refuses_when_user_not_marked_live():
    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.metadata_store = FakeMetadataStore(process=FakeProcessInfo(), live=False)
    supervisor.process_manager = FakeProcessManager(result=True)

    ok = await supervisor.restart_user("leja..1")

    assert ok is False
    assert supervisor.process_manager.calls == []


@pytest.mark.asyncio
async def test_restart_user_rejects_invalid_username_before_process_lookup():
    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.metadata_store = FakeMetadataStore(process=FakeProcessInfo(), live=True)
    supervisor.process_manager = FakeProcessManager(result=True)

    ok = await supervisor.restart_user("../alice")

    assert ok is False
    assert supervisor.process_manager.calls == []
