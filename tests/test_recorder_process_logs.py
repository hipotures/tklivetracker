import json
import stat
from pathlib import Path as RealPath
from types import SimpleNamespace

import pytest

import persistent_live_manager.live_process_manager as live_process_manager_module
from persistent_live_manager.live_process_manager import LiveProcessManager
from persistent_live_manager.recording_health_monitor import RecordingHealthMonitor


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return None


class TerminatedProcess(FakeProcess):
    def __init__(self, pid, returncode):
        super().__init__(pid)
        self.returncode = returncode

    def poll(self):
        return self.returncode


class CommunicatingTerminatedProcess(TerminatedProcess):
    def __init__(self, pid, returncode, stderr=b""):
        super().__init__(pid, returncode)
        self.stderr = stderr

    def communicate(self):
        return b"", self.stderr


@pytest.mark.asyncio
async def test_detached_recorder_logs_include_pid_and_preserve_previous_run(
    monkeypatch, tmp_path
):
    manager = LiveProcessManager(
        object(),
        {
            "recordings_path": str(tmp_path / "recordings"),
            "recorder_log_path": str(tmp_path / "recorder-logs"),
        },
    )
    pids = iter((4101, 4102))

    def fake_popen(_command, **kwargs):
        process = FakeProcess(next(pids))
        kwargs["stdout"].write(f"output from {process.pid}")
        kwargs["stdout"].flush()
        return process

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(live_process_manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(live_process_manager_module.asyncio, "sleep", no_sleep)

    await manager._start_detached_process(["recorder"], "alice")
    log_dir = tmp_path / "recorder-logs"
    first_log = next(log_dir.glob("live_alice_4101_*.out"))
    assert first_log.read_text() == "output from 4101"
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(first_log.stat().st_mode) == 0o600
    first_error_log = next(log_dir.glob("live_alice_4101_*.err"))
    assert stat.S_IMODE(first_error_log.stat().st_mode) == 0o600
    first_error_log.write_text("[NO_STREAM_DATA] fixture", encoding="utf-8")

    monitor = RecordingHealthMonitor(
        object(),
        {"recorder_log_path": str(log_dir)},
    )
    process_info = SimpleNamespace(username="alice", pid=4101)
    assert monitor._recorder_log_contains(process_info, "[NO_STREAM_DATA]")

    await manager._start_detached_process(["recorder"], "alice")
    second_log = next(log_dir.glob("live_alice_4102_*.out"))

    assert first_log.read_text() == "output from 4101"
    assert second_log.read_text() == "output from 4102"
    assert not (log_dir / "live_alice.out").exists()


@pytest.mark.asyncio
async def test_explicit_offline_start_is_classified_as_race_condition(
    monkeypatch,
    tmp_path,
):
    manager = LiveProcessManager(object(), {"recordings_path": str(tmp_path)})

    def fake_path(value):
        if str(value) == "/tmp/tiktok_live_logs":
            return tmp_path
        return RealPath(value)

    def fake_popen(_command, **kwargs):
        process = TerminatedProcess(4103, 11)
        kwargs["stderr"].write(
            "The user is not hosting a live stream at the moment. "
            "Exiting with code 11."
        )
        kwargs["stderr"].flush()
        return process

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(live_process_manager_module, "Path", fake_path)
    monkeypatch.setattr(live_process_manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(live_process_manager_module.asyncio, "sleep", no_sleep)

    process, pid, error = await manager._start_detached_process(
        ["recorder"],
        "alice",
    )

    assert process is None
    assert pid is None
    assert error == (
        "RACE_CONDITION: User went offline between detection and recording start"
    )


@pytest.mark.asyncio
async def test_detached_no_stream_exit_warns_without_suppressing_retry(
    monkeypatch,
    tmp_path,
    caplog,
):
    manager = LiveProcessManager(
        object(),
        {
            "recordings_path": str(tmp_path),
            "no_stream_data_retry_cooldown": 300,
        },
    )

    def fake_path(value):
        if str(value) == "/tmp/tiktok_live_logs":
            return tmp_path
        return RealPath(value)

    def fake_popen(_command, **kwargs):
        process = TerminatedProcess(4104, 12)
        kwargs["stderr"].write("[NO_STREAM_DATA] No stream URL")
        kwargs["stderr"].flush()
        return process

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(live_process_manager_module, "Path", fake_path)
    monkeypatch.setattr(live_process_manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(live_process_manager_module.asyncio, "sleep", no_sleep)

    with caplog.at_level("DEBUG", logger="PROC"):
        process, pid, error = await manager._start_detached_process(
            ["recorder"],
            "alice",
        )

    assert process is None
    assert pid is None
    assert error.startswith("NO_STREAM_DATA:")
    assert manager._no_stream_data_cooldown_remaining("alice") == 0
    matching = [
        record
        for record in caplog.records
        if "NO_STREAM_DATA:" in record.getMessage()
    ]
    assert [record.levelname for record in matching] == ["WARNING"]
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


@pytest.mark.asyncio
async def test_attached_no_stream_exit_is_not_logged_as_error(
    monkeypatch,
    tmp_path,
    caplog,
):
    manager = LiveProcessManager(
        object(),
        {
            "recordings_path": str(tmp_path),
            "detached_process_mode": False,
        },
    )

    def fake_popen(_command, **_kwargs):
        return CommunicatingTerminatedProcess(
            4105,
            12,
            b"[NO_STREAM_DATA] No stream URL",
        )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(live_process_manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(live_process_manager_module.asyncio, "sleep", no_sleep)

    with caplog.at_level("DEBUG", logger="PROC"):
        process, pid, error = await manager._start_attached_process(
            ["recorder"],
            "alice",
        )

    assert process is None
    assert pid is None
    assert error.startswith("NO_STREAM_DATA:")
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


@pytest.mark.asyncio
async def test_detached_waf_failure_does_not_launch_external_recorder(
    monkeypatch,
    tmp_path,
):
    manager = LiveProcessManager(object(), {"recordings_path": str(tmp_path)})

    def fake_path(value):
        if str(value) == "/tmp/tiktok_live_logs":
            return tmp_path
        return RealPath(value)

    def fake_popen(_command, **kwargs):
        process = TerminatedProcess(4106, 1)
        kwargs["stderr"].write("TikTok WAF blocked this request")
        kwargs["stderr"].flush()
        return process

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(live_process_manager_module, "Path", fake_path)
    monkeypatch.setattr(live_process_manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(live_process_manager_module.asyncio, "sleep", no_sleep)

    process, pid, error = await manager._start_detached_process(
        ["recorder"],
        "alice",
    )

    assert process is None
    assert pid is None
    assert error.startswith("WAF block detected:")
    assert not hasattr(manager, "_try_original_recorder_fallback")


@pytest.mark.asyncio
async def test_attached_waf_failure_does_not_launch_external_recorder(
    monkeypatch,
    tmp_path,
):
    manager = LiveProcessManager(
        object(),
        {
            "recordings_path": str(tmp_path),
            "detached_process_mode": False,
        },
    )

    def fake_popen(_command, **_kwargs):
        return CommunicatingTerminatedProcess(
            4107,
            1,
            b"TikTok WAF blocked this request",
        )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(live_process_manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(live_process_manager_module.asyncio, "sleep", no_sleep)

    process, pid, error = await manager._start_attached_process(
        ["recorder"],
        "alice",
    )

    assert process is None
    assert pid is None
    assert error.startswith("WAF block detected:")
    assert not hasattr(manager, "_try_original_recorder_fallback")


@pytest.mark.asyncio
async def test_registration_race_returns_marker_without_error_log(
    monkeypatch,
    tmp_path,
    caplog,
):
    class RegistrationRaceStore:
        def get_process_by_username(self, _username):
            return None

        def register_process(self, **_kwargs):
            raise ValueError("User alice is not live (is_live=0) - race condition")

    manager = LiveProcessManager(
        RegistrationRaceStore(),
        {"recordings_path": str(tmp_path)},
    )
    manager.get_system_recorder_processes = lambda: []
    manager._build_recording_command = lambda *_args: ["recorder"]

    async def no_processes():
        return 0

    async def start_process(_command, _username):
        return SimpleNamespace(pid=4106), 4106, None

    async def terminate_process(_pid, *, expected):
        assert expected.username == 'alice'
        return True

    manager._count_running_processes = no_processes
    manager._start_detached_process = start_process
    manager._terminate_process = terminate_process

    with caplog.at_level("DEBUG", logger="PROC"):
        result = await manager.start_live_recording("alice")

    assert result.success is False
    assert result.error.startswith("RACE_CONDITION:")
    race_logs = [
        record
        for record in caplog.records
        if "race condition" in record.getMessage().lower()
    ]
    assert race_logs
    assert not [record for record in race_logs if record.levelname == "ERROR"]


def test_missing_startup_metadata_is_debug_while_delayed_link_is_pending(
    tmp_path,
    caplog,
):
    manager = LiveProcessManager(object(), {"recordings_path": str(tmp_path)})
    process = SimpleNamespace(pid=123)
    metadata_path = tmp_path / "pending.json"

    with caplog.at_level("DEBUG", logger="PROC"):
        manager._attach_startup_metadata(
            process,
            ["recorder", "--startup-metadata-file", str(metadata_path)],
            "alice",
        )

    matching = [
        record
        for record in caplog.records
        if "Startup metadata not ready" in record.getMessage()
    ]
    assert [record.levelname for record in matching] == ["DEBUG"]


@pytest.mark.asyncio
async def test_failed_delayed_startup_metadata_link_remains_warning(
    tmp_path,
    caplog,
):
    class RejectingMetadataStore:
        def link_process_live_session(self, process_id, live_id):
            return False

    manager = LiveProcessManager(
        RejectingMetadataStore(),
        {"recordings_path": str(tmp_path)},
    )
    metadata_path = tmp_path / "startup.json"
    metadata_path.write_text(
        json.dumps({"pid": 123, "live_id": 456}),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="PROC"):
        await manager._link_delayed_startup_metadata(
            process_id=1,
            pid=123,
            username="alice",
            metadata_path=metadata_path,
        )

    matching = [
        record
        for record in caplog.records
        if "Could not link delayed startup metadata" in record.getMessage()
    ]
    assert [record.levelname for record in matching] == ["WARNING"]
