import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import persistent_live_manager.recording_health_monitor as recording_health_monitor_module
from persistent_live_manager.live_process_manager import (
    LiveProcessManager,
    OrphanProcessCandidate,
    ProcessStartResult,
)
from persistent_live_manager.process_info import ProcessInfo
from persistent_live_manager.process_inventory import (
    RecorderProcess,
    parse_recorder_process,
)
from persistent_live_manager.recording_health_monitor import (
    HealthCheckResult,
    RecordingHealthMonitor,
)
from modules.db_user import end_live_session
from selenium_supervisor import SeleniumSupervisor


def make_process_info(process_id, username, pid, path=None, restart_count=0):
    return ProcessInfo(
        id=process_id,
        user_id=process_id,
        username=username,
        pid=pid,
        recording_file_path=str(path) if path else None,
        started_at=datetime.now(),
        last_health_check=None,
        health_status="healthy",
        restart_count=restart_count,
        file_size_bytes=0,
        is_active=True,
        room_id=None,
    )


def make_recorder(pid, username, output_file, create_time):
    args = (
        "/usr/bin/python3",
        "-m",
        "recorder.main",
        "-user",
        username,
        "--output-file",
        str(output_file),
        "--persistent-mode",
    )
    return RecorderProcess(pid, username, str(output_file), create_time, args)


class FakeMetadataStore:
    def __init__(self, active=None):
        self.active = list(active or [])
        self.stopped = []
        self.health_updates = []
        self.reset_users = []
        self.next_id = 100
        self.identities = {}
        self.closed_live_sessions = []

    def get_active_processes(self):
        return [process for process in self.active if process.is_active]

    def get_process_by_username(self, username):
        matches = [
            process
            for process in self.get_active_processes()
            if process.username == username
        ]
        return matches[-1] if matches else None

    def get_process_by_id(self, process_id):
        return next((process for process in self.active if process.id == process_id), None)

    def register_process(self, username, pid, room_id=None, recording_file_path=None, live_id=None):
        self.next_id += 1
        process = make_process_info(self.next_id, username, pid, recording_file_path)
        self.active.append(process)
        if live_id is not None:
            self.identities[process.id] = {'live_id': live_id}
        return process.id

    def get_process_live_identity(self, process_id):
        return self.identities.get(process_id)

    def close_process_live_session(self, process_id):
        self.closed_live_sessions.append(process_id)
        return True

    def mark_process_stopped(self, process_id, health_status="stopped"):
        process = self.get_process_by_id(process_id)
        if process:
            process.is_active = False
            process.health_status = health_status
        self.stopped.append((process_id, health_status))
        return True

    def update_health_status(self, process_id, status, file_size=0):
        self.health_updates.append((process_id, status, file_size))
        return True

    def reset_user_live_status(self, username):
        self.reset_users.append(username)
        return True

    def is_user_live(self, username):
        return True


def test_parse_recorder_process_requires_exact_recorder_arguments():
    recorder = parse_recorder_process(
        42,
        [
            "python3",
            "-m",
            "recorder.main",
            "-user",
            "alice",
            "--output-file",
            "/tmp/alice.mp4",
            "--persistent-mode",
        ],
        12.5,
    )

    assert recorder is not None
    assert recorder.username == "alice"
    assert recorder.output_file == "/tmp/alice.mp4"
    assert parse_recorder_process(43, ["grep", "recorder.main"], 0) is None


def test_parse_recorder_process_uses_canonical_username_validation():
    base = ["python", "-m", "recorder.main", "--persistent-mode", "-user"]
    assert parse_recorder_process(41, [*base, "leja..1"], 1.0) is not None
    assert parse_recorder_process(42, [*base, "../alice"], 1.0) is None


def test_cleanup_removes_only_exact_empty_output_for_dead_process(tmp_path, caplog):
    user_dir = tmp_path / "alice"
    user_dir.mkdir()
    exact_output = user_dir / "alice_20260718_120000.mp4"
    unrelated_output = user_dir / "alice_20260718_120001.mp4"
    exact_output.touch()
    unrelated_output.touch()

    process = make_process_info(1, "alice", 101, exact_output)
    manager = LiveProcessManager(
        FakeMetadataStore([process]),
        {"recordings_path": str(tmp_path)},
    )
    manager._is_process_running = lambda _pid: False
    manager.get_system_recorder_processes = lambda: []

    with caplog.at_level(logging.INFO, logger="PROC"):
        removed = manager.cleanup_zero_byte_output(process)

    assert removed is True
    assert not exact_output.exists()
    assert unrelated_output.exists()
    cleanup_records = [
        record for record in caplog.records
        if "Removed exact 0-byte recorder output" in record.getMessage()
    ]
    assert len(cleanup_records) == 1
    assert cleanup_records[0].levelno == logging.INFO


@pytest.mark.parametrize("guard", ["running", "nonempty", "outside", "active_owner"])
def test_cleanup_keeps_output_without_full_ownership_confirmation(tmp_path, guard):
    user_dir = tmp_path / "alice"
    user_dir.mkdir()
    output = user_dir / "alice_20260718_120000.mp4"
    output.touch()
    if guard == "nonempty":
        output.write_bytes(b"video")
    if guard == "outside":
        output = tmp_path / "outside.mp4"
        output.touch()

    process = make_process_info(1, "alice", 101, output)
    manager = LiveProcessManager(
        FakeMetadataStore([process]),
        {"recordings_path": str(tmp_path)},
    )
    manager._is_process_running = lambda _pid: guard == "running"
    manager.get_system_recorder_processes = lambda: (
        [make_recorder(202, "alice", output, 10)]
        if guard == "active_owner"
        else []
    )

    assert manager.cleanup_zero_byte_output(process) is False
    assert output.exists()


@pytest.mark.asyncio
async def test_confirmed_process_stop_removes_its_exact_empty_output(tmp_path):
    user_dir = tmp_path / "alice"
    user_dir.mkdir()
    output = user_dir / "alice_20260718_120000.mp4"
    output.touch()
    process = make_process_info(1, "alice", 101, output)
    store = FakeMetadataStore([process])
    manager = LiveProcessManager(store, {"recordings_path": str(tmp_path)})

    async def terminate(_pid, _graceful, *, expected):
        assert expected is process
        return True

    manager._terminate_process = terminate
    manager._is_process_running = lambda _pid: False
    manager.get_system_recorder_processes = lambda: []

    assert await manager.stop_process(process) is True
    assert not output.exists()
    assert store.stopped == [(1, "stopped")]


def test_cleanup_plan_keeps_freshest_active_process_and_includes_untracked(tmp_path):
    fresh_file = tmp_path / "fresh.mp4"
    old_file = tmp_path / "old.mp4"
    orphan_file = tmp_path / "orphan.mp4"
    for path in (fresh_file, old_file, orphan_file):
        path.touch()
    os.utime(old_file, (100, 100))
    os.utime(fresh_file, (200, 200))

    fresh = make_process_info(1, "alice", 101, fresh_file)
    old = make_process_info(2, "alice", 102, old_file)
    healthy = make_process_info(3, "bob", 201, tmp_path / "bob.mp4")
    store = FakeMetadataStore([fresh, old, healthy])
    manager = LiveProcessManager(store, {"recordings_path": str(tmp_path)})
    manager.get_system_recorder_processes = lambda: [
        make_recorder(101, "alice", fresh_file, 20),
        make_recorder(102, "alice", old_file, 10),
        make_recorder(201, "bob", tmp_path / "bob.mp4", 30),
        make_recorder(301, "carol", orphan_file, 40),
    ]

    candidates = manager.build_orphan_cleanup_plan()

    assert [(item.process.pid, item.reason, item.kept_pid) for item in candidates] == [
        (102, "redundant active duplicate", 101),
        (301, "no matching active database record", None),
    ]


class StartTestManager(LiveProcessManager):
    def __init__(self, metadata_store, config):
        super().__init__(metadata_store, config)
        self.spawn_count = 0

    async def _count_running_processes(self):
        return 0

    async def _start_detached_process(self, cmd, username):
        self.spawn_count += 1
        pid = 9000 + self.spawn_count
        await asyncio.sleep(0)
        return object(), pid, None

    def _is_process_running(self, pid):
        return pid >= 9000


@pytest.mark.asyncio
async def test_concurrent_same_user_starts_spawn_only_one_recorder(tmp_path):
    store = FakeMetadataStore()
    manager = StartTestManager(
        store,
        {
            "recordings_path": str(tmp_path),
            "detached_process_mode": True,
            "max_live_processes": 100,
        },
    )

    first, second = await asyncio.gather(
        manager.start_live_recording("alice"),
        manager.start_live_recording("alice"),
    )

    assert manager.spawn_count == 1
    assert first.started_new is True
    assert second.started_new is False
    assert first.success is True
    assert second.success is True


@pytest.mark.asyncio
async def test_system_recorder_blocks_duplicate_start_without_database_row(tmp_path):
    store = FakeMetadataStore()
    manager = StartTestManager(
        store,
        {
            "recordings_path": str(tmp_path),
            "detached_process_mode": True,
            "max_live_processes": 100,
        },
    )
    recorder = make_recorder(4321, "alice", tmp_path / "alice.mp4", 10)
    manager.get_system_recorder_processes = lambda: [recorder]

    result = await manager.start_live_recording("alice")

    assert result.success is True
    assert result.started_new is False
    assert result.pid == 4321
    assert manager.spawn_count == 0


@pytest.mark.asyncio
async def test_health_monitor_requests_stop_instead_of_hiding_running_process():
    process = make_process_info(1, "alice", 123, restart_count=3)
    store = FakeMetadataStore([process])
    monitor = RecordingHealthMonitor(store, {"max_restart_attempts": 3})
    calls = []

    async def stop_callback(process_info, final_status, reset_live_status):
        calls.append((process_info.id, final_status, reset_live_status))
        return True

    monitor.add_stop_callback(stop_callback)
    await monitor._handle_unhealthy_process(
        HealthCheckResult(process, False, ["stale"])
    )

    assert calls == [(1, "stopped_max_retries", True)]
    assert store.stopped == []


@pytest.mark.asyncio
async def test_health_monitor_checks_untracked_system_process_when_database_is_empty(tmp_path):
    process = make_recorder(222, "alice", tmp_path / "alice.mp4", 10)
    store = FakeMetadataStore()
    monitor = RecordingHealthMonitor(store, {})
    stop_calls = []

    monitor.set_system_process_provider(lambda: [process])
    monitor.set_live_check_callback(lambda username: asyncio.sleep(0, result=False))

    async def stop_callback(candidate):
        stop_calls.append(candidate)
        return True

    async def no_sync(*args):
        return None

    monitor.add_untracked_stop_callback(stop_callback)
    monitor._sync_database_status = no_sync

    await monitor.check_all_processes()

    assert stop_calls == [process]
    assert monitor.stats["untracked_processes_found"] == 1


@pytest.mark.asyncio
async def test_health_monitor_ignores_untracked_recorder_during_startup_grace(tmp_path):
    process = make_recorder(222, "alice", tmp_path / "alice.mp4", time.time())
    store = FakeMetadataStore()
    monitor = RecordingHealthMonitor(
        store,
        {"untracked_startup_grace_seconds": 10},
    )
    stop_calls = []

    monitor.set_system_process_provider(lambda: [process])
    monitor.set_live_check_callback(lambda username: asyncio.sleep(0, result=False))

    async def stop_callback(candidate):
        stop_calls.append(candidate)
        return True

    async def no_sync(*args):
        return None

    monitor.add_untracked_stop_callback(stop_callback)
    monitor._sync_database_status = no_sync

    await monitor.check_all_processes()

    assert stop_calls == []
    assert monitor.stats["untracked_processes_found"] == 0


@pytest.mark.asyncio
async def test_database_sync_treats_system_recorder_as_active(tmp_path):
    db_path = tmp_path / "health.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            is_live INTEGER NOT NULL
        );
        CREATE TABLE lives (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        INSERT INTO users (id, username, is_live) VALUES (1, 'alice', 1);
        INSERT INTO lives (id, user_id, started_at, ended_at)
        VALUES (10, 1, '2026-07-18 00:00:00', NULL);
        """
    )
    conn.close()

    store = FakeMetadataStore()
    store.db_path = str(db_path)
    monitor = RecordingHealthMonitor(store, {})
    process = make_recorder(222, "alice", tmp_path / "alice.mp4", 10)

    await monitor._sync_database_status([process])

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT is_live FROM users WHERE id = 1").fetchone()[0] == 1
    assert conn.execute("SELECT ended_at FROM lives WHERE id = 10").fetchone()[0] is None
    conn.close()


@pytest.mark.asyncio
async def test_health_monitor_warns_about_duplicate_system_recorders(tmp_path, caplog):
    first = make_recorder(101, "alice", tmp_path / "first.mp4", 10)
    second = make_recorder(202, "alice", tmp_path / "second.mp4", 20)
    store = FakeMetadataStore()
    monitor = RecordingHealthMonitor(store, {})

    monitor.set_system_process_provider(lambda: [first, second])
    monitor.set_live_check_callback(lambda username: asyncio.sleep(0, result=True))

    async def no_sync(*args):
        return None

    monitor._sync_database_status = no_sync

    with caplog.at_level("WARNING"):
        await monitor.check_all_processes()

    duplicate_messages = [
        record.getMessage()
        for record in caplog.records
        if "Duplicate recorders detected" in record.getMessage()
    ]
    assert len(duplicate_messages) == 1
    assert "PID=101" in duplicate_messages[0]
    assert "PID=202" in duplicate_messages[0]
    assert monitor.stats["duplicate_recorders_found"] == 1


@pytest.mark.asyncio
async def test_no_growth_keeps_recorder_when_tiktok_identity_is_unchanged(tmp_path):
    output = tmp_path / "alice.mp4"
    output.write_bytes(b"recording")
    old_timestamp = datetime.now().timestamp() - 301
    os.utime(output, (old_timestamp, old_timestamp))
    process = make_process_info(1, "alice", 123, output)
    process.started_at = datetime.fromtimestamp(old_timestamp)
    store = FakeMetadataStore([process])
    store.identities[1] = {
        'live_id': 10,
        'tiktok_stream_id': 'stream-1',
        'tiktok_started_at': 1000,
    }
    monitor = RecordingHealthMonitor(
        store,
        {'no_growth_identity_check_after': 300},
    )
    monitor.is_process_running = lambda pid: True
    replacements = []
    monitor.set_identity_check_callback(
        lambda username: asyncio.sleep(0, result={
            'state': 'live',
            'stream_id': 'stream-1',
            'start_time': 1000,
        })
    )

    async def replace_callback(*args):
        replacements.append(args)
        return True

    monitor.add_replacement_callback(replace_callback)
    await monitor._handle_file_progress(process, {})

    assert replacements == []


@pytest.mark.asyncio
async def test_no_growth_replaces_old_recorder_after_confirmed_new_identity(tmp_path):
    output = tmp_path / "alice.mp4"
    output.write_bytes(b"recording")
    old_timestamp = datetime.now().timestamp() - 301
    os.utime(output, (old_timestamp, old_timestamp))
    process = make_process_info(1, "alice", 123, output)
    process.started_at = datetime.fromtimestamp(old_timestamp)
    store = FakeMetadataStore([process])
    store.identities[1] = {
        'live_id': 10,
        'tiktok_stream_id': 'old-stream',
        'tiktok_started_at': 1000,
    }
    monitor = RecordingHealthMonitor(store, {})
    monitor.is_process_running = lambda pid: True
    identity_checks = []
    replacements = []

    async def identity_check(username):
        identity_checks.append(username)
        return {
            'state': 'live',
            'stream_id': 'new-stream',
            'start_time': 2000,
        }

    async def replace_callback(*args):
        replacements.append(args)
        return True

    monitor.set_identity_check_callback(identity_check)
    monitor.add_replacement_callback(replace_callback)
    await monitor._handle_file_progress(process, {})

    assert identity_checks == ["alice", "alice"]
    assert replacements == [(
        process,
        "TikTok live identity changed",
        "stopped_replaced_session",
        True,
    )]


@pytest.mark.asyncio
async def test_no_growth_identity_check_respects_interval(tmp_path):
    output = tmp_path / "alice.mp4"
    output.write_bytes(b"recording")
    old_timestamp = datetime.now().timestamp() - 301
    os.utime(output, (old_timestamp, old_timestamp))
    process = make_process_info(1, "alice", 123, output)
    process.started_at = datetime.fromtimestamp(old_timestamp)
    store = FakeMetadataStore([process])
    store.identities[1] = {
        'live_id': 10,
        'tiktok_stream_id': 'stream-1',
        'tiktok_started_at': 1000,
    }
    monitor = RecordingHealthMonitor(store, {})
    monitor.is_process_running = lambda pid: True
    identity_checks = []

    async def identity_check(username):
        identity_checks.append(username)
        return {
            'state': 'live',
            'stream_id': 'stream-1',
            'start_time': 1000,
        }

    monitor.set_identity_check_callback(identity_check)
    await monitor._handle_file_progress(process, {})
    await monitor._handle_file_progress(process, {})

    assert identity_checks == ["alice"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "message"),
    [
        ({'state': 'unknown'}, "decision=UNKNOWN; keeping process"),
        (
            {
                'state': 'live',
                'stream_id': 'stream-1',
                'start_time': 1000,
            },
            "complete linked identity is unavailable",
        ),
    ],
)
async def test_repeated_unknown_identity_warns_once_per_stalled_file(
    tmp_path,
    caplog,
    identity,
    message,
):
    output = tmp_path / "alice.mp4"
    output.write_bytes(b"recording")
    old_timestamp = datetime.now().timestamp() - 301
    os.utime(output, (old_timestamp, old_timestamp))
    process = make_process_info(1, "alice", 123, output)
    process.started_at = datetime.fromtimestamp(old_timestamp)
    store = FakeMetadataStore([process])
    monitor = RecordingHealthMonitor(
        store,
        {'stale_identity_check_interval': 0},
    )
    monitor.is_process_running = lambda pid: True
    monitor.set_identity_check_callback(
        lambda username: asyncio.sleep(0, result=identity)
    )

    with caplog.at_level("DEBUG"):
        await monitor._handle_file_progress(process, {})
        await monitor._handle_file_progress(process, {})

    messages = [
        record
        for record in caplog.records
        if message in record.getMessage()
    ]
    assert [record.levelname for record in messages] == ["WARNING", "DEBUG"]


@pytest.mark.asyncio
async def test_hard_timeout_replaces_without_identity_request(tmp_path):
    output = tmp_path / "alice.mp4"
    output.write_bytes(b"recording")
    old_timestamp = datetime.now().timestamp() - 21601
    os.utime(output, (old_timestamp, old_timestamp))
    process = make_process_info(1, "alice", 123, output)
    process.started_at = datetime.fromtimestamp(old_timestamp)
    store = FakeMetadataStore([process])
    monitor = RecordingHealthMonitor(store, {})
    monitor.is_process_running = lambda pid: True
    identity_checks = []
    replacements = []

    async def identity_check(username):
        identity_checks.append(username)
        return {'state': 'unknown'}

    async def replace_callback(*args):
        replacements.append(args)
        return True

    monitor.set_identity_check_callback(identity_check)
    monitor.add_replacement_callback(replace_callback)
    await monitor._handle_file_progress(process, {})

    assert identity_checks == []
    assert replacements == [(
        process,
        "no file growth for 21601 seconds",
        "stopped_same_session_timeout",
        True,
    )]


@pytest.mark.asyncio
async def test_missing_file_uses_startup_grace_period(tmp_path):
    process = make_process_info(1, "alice", 123, tmp_path / "missing.mp4")
    store = FakeMetadataStore([process])
    monitor = RecordingHealthMonitor(store, {})
    monitor.is_process_running = lambda pid: True

    assert await monitor._check_file_health(process) == []

    process.started_at = datetime.now() - timedelta(seconds=301)
    issues = await monitor._check_file_health(process)
    assert issues == [
        "Recorder produced no stream data within 300 seconds: "
        f"{process.recording_file_path}"
    ]


@pytest.mark.asyncio
async def test_missing_file_is_not_exempted_by_legacy_command_shape(
    monkeypatch, tmp_path
):
    process = make_process_info(1, "alice", 123, tmp_path / "missing.mp4")
    process.started_at = datetime.now() - timedelta(seconds=301)
    store = FakeMetadataStore([process])
    monitor = RecordingHealthMonitor(store, {})
    monitor.is_process_running = lambda pid: True

    class LegacyProcess:
        def cmdline(self):
            return ["python", "main.py", "-mode", "manual"]

    monkeypatch.setattr(
        recording_health_monitor_module.psutil,
        "Process",
        lambda pid: LegacyProcess(),
    )

    issues = await monitor._check_file_health(process)

    assert issues == [
        "Recorder produced no stream data within 300 seconds: "
        f"{process.recording_file_path}"
    ]


@pytest.mark.asyncio
async def test_missing_segmented_file_reports_expected_part001(tmp_path):
    process = make_process_info(1, "alice", 123, tmp_path / "missing.mp4")
    process.started_at = datetime.now() - timedelta(seconds=301)
    store = FakeMetadataStore([process])
    monitor = RecordingHealthMonitor(store, {"segment_on_reconnect": True})
    monitor.is_process_running = lambda pid: True

    issues = await monitor._check_file_health(process)

    assert issues == [
        "Recorder produced no stream data within 300 seconds: "
        f"{tmp_path / 'missing_part001.mp4'}"
    ]


@pytest.mark.asyncio
async def test_dead_process_requests_exact_replacement():
    process = make_process_info(1, "alice", 123)
    store = FakeMetadataStore([process])
    monitor = RecordingHealthMonitor(store, {})
    replacements = []
    monitor.set_live_check_callback(lambda username: asyncio.sleep(0, result=True))

    async def replace_callback(*args):
        replacements.append(args)
        return True

    monitor.add_replacement_callback(replace_callback)
    await monitor._handle_unhealthy_process(
        HealthCheckResult(process, False, ["Process PID 123 is not running"])
    )

    assert replacements == [(
        process,
        "recorder process is dead",
        "stopped_dead",
        True,
    )]


@pytest.mark.asyncio
async def test_dead_process_is_reported_once_as_normal_completion(caplog):
    process = make_process_info(1, "alice", 123, "/tmp/missing.mp4")
    monitor = RecordingHealthMonitor(FakeMetadataStore([process]), {})
    monitor.is_process_running = lambda pid: False

    with caplog.at_level("DEBUG", logger="HLTH"):
        result = await monitor.check_process_health(process)

    assert result.issues == ["Process PID 123 is not running"]
    matching = [
        record
        for record in caplog.records
        if "Process (PID=123)" in record.getMessage()
    ]
    assert [record.levelname for record in matching] == ["DEBUG"]


@pytest.mark.asyncio
async def test_zero_data_marker_explains_dead_recorder(tmp_path, caplog):
    process = make_process_info(1, "alice", 123, tmp_path / "missing.mp4")
    (tmp_path / "live_alice_123_token.err").write_text(
        "[*] 2026-07-19 16:00:00 - [NO_STREAM_DATA] Recorder produced no stream data\n",
        encoding="utf-8",
    )
    monitor = RecordingHealthMonitor(
        FakeMetadataStore([process]),
        {"recorder_log_path": str(tmp_path)},
    )
    monitor.is_process_running = lambda pid: False

    with caplog.at_level("WARNING", logger="HLTH"):
        result = await monitor.check_process_health(process)

    assert result.issues == ["Recorder exited without producing stream data"]
    matching = [
        record
        for record in caplog.records
        if "Process (PID=123)" in record.getMessage()
    ]
    assert [record.levelname for record in matching] == ["WARNING"]


def test_waf_cleanup_uses_configured_recorder_log_path(tmp_path):
    process = make_process_info(1, "alice", 123, tmp_path / "missing.mp4")

    class RemovingStore(FakeMetadataStore):
        def remove_process(self, process_id, username):
            self.stopped.append((process_id, username))
            return True

    store = RemovingStore([process])
    monitor = RecordingHealthMonitor(
        store,
        {"recorder_log_path": str(tmp_path / "recorder-logs")},
    )
    monitor.recorder_log_path.mkdir()
    (monitor.recorder_log_path / "live_alice_123_token.err").write_text(
        "TikTok WAF blocked this request",
        encoding="utf-8",
    )
    monitor._is_process_alive = lambda _pid: False
    actions = []

    monitor._cleanup_failed_waf_processes([process], actions)

    assert store.stopped == [(1, "alice")]
    assert actions == ["Removed failed WAF process for alice (PID=123)"]


@pytest.mark.asyncio
async def test_zero_data_exit_requests_exact_replacement():
    process = make_process_info(1, "alice", 123)
    monitor = RecordingHealthMonitor(FakeMetadataStore([process]), {})
    replacements = []
    monitor.set_live_check_callback(lambda username: asyncio.sleep(0, result=True))

    async def replace_callback(*args):
        replacements.append(args)
        return True

    monitor.add_replacement_callback(replace_callback)
    await monitor._handle_unhealthy_process(
        HealthCheckResult(
            process,
            False,
            ["Recorder exited without producing stream data"],
        )
    )

    assert replacements == [(
        process,
        "recorder produced no stream data",
        "stopped_no_stream_data",
        True,
    )]


@pytest.mark.asyncio
async def test_restart_aborts_when_existing_recorder_cannot_be_stopped(tmp_path):
    process = make_process_info(1, "alice", 123)
    store = FakeMetadataStore([process])
    manager = LiveProcessManager(
        store,
        {"recordings_path": str(tmp_path), "process_restart_delay": 0},
    )
    start_calls = []

    async def failed_cleanup(username):
        return False

    async def start_replacement(username, room_id=None, force_restart=False):
        start_calls.append(username)
        return None

    manager._cleanup_all_user_processes = failed_cleanup
    manager._start_live_recording_unlocked = start_replacement

    result = await manager.restart_live_recording(process, ["stale"])

    assert result is False
    assert start_calls == []


@pytest.mark.asyncio
async def test_restart_cleanup_counts_each_successfully_stopped_recorder(tmp_path):
    process = make_process_info(1, "alice", 123)
    manager = LiveProcessManager(
        FakeMetadataStore([process]),
        {"recordings_path": str(tmp_path)},
    )

    async def terminate(pid, graceful=True, *, expected):
        assert expected is process
        assert pid == 123
        assert graceful is False
        return True

    manager._terminate_process = terminate
    manager.get_system_recorder_processes = lambda: []

    assert await manager._cleanup_all_user_processes("alice") is True
    assert manager.stats["processes_stopped"] == 1


@pytest.mark.asyncio
async def test_restart_rejects_invalid_username_before_cleanup(tmp_path):
    process = make_process_info(1, "../alice", 123)
    manager = LiveProcessManager(
        FakeMetadataStore([process]),
        {"recordings_path": str(tmp_path), "process_restart_delay": 0},
    )
    cleanup_calls = []

    async def cleanup(username):
        cleanup_calls.append(username)
        return True

    manager._cleanup_all_user_processes = cleanup

    assert await manager.restart_live_recording(process, ["stale"]) is False
    assert cleanup_calls == []


@pytest.mark.asyncio
async def test_repeated_period_username_remains_operable_across_restart(tmp_path):
    process = make_process_info(1, "leja..1", 123)
    manager = LiveProcessManager(
        FakeMetadataStore([process]),
        {"recordings_path": str(tmp_path), "process_restart_delay": 0},
    )
    cleanup_calls = []
    start_calls = []

    async def cleanup(username):
        cleanup_calls.append(username)
        return True

    async def start_locked(username, **kwargs):
        start_calls.append((username, kwargs))
        return ProcessStartResult(True, 2, 456, None, started_new=True)

    manager._cleanup_all_user_processes = cleanup
    manager._start_live_recording_locked = start_locked

    assert await manager.restart_live_recording(process, ["stale"]) is True
    assert cleanup_calls == ["leja..1"]
    assert start_calls == [(
        "leja..1",
        {
            "room_id": None,
            "force_restart": True,
            "ignore_no_stream_data_cooldown": False,
        },
    )]


@pytest.mark.asyncio
async def test_room_id_unavailable_replacement_is_logged_as_info(tmp_path, caplog):
    process = make_process_info(1, "alice", 123)
    manager = LiveProcessManager(
        FakeMetadataStore([process]),
        {"recordings_path": str(tmp_path)},
    )

    async def stop_process(*args, **kwargs):
        return True

    async def start_recording(*args, **kwargs):
        return ProcessStartResult(
            False,
            None,
            None,
            LiveProcessManager._room_id_unavailable_message(),
        )

    manager.stop_process = stop_process
    manager.start_live_recording = start_recording

    with caplog.at_level("INFO", logger="PROC"):
        result = await manager.replace_exact_process(
            process,
            "no file growth",
            "stopped_same_session_timeout",
            True,
        )

    assert result is False
    matching = [
        record
        for record in caplog.records
        if "Failed to start replacement" in record.getMessage()
    ]
    assert [record.levelname for record in matching] == ["INFO"]


@pytest.mark.asyncio
@pytest.mark.parametrize("other_recorder_running", [False, True])
async def test_race_condition_replacement_logs_debug_and_resets_only_without_recorder(
    tmp_path,
    caplog,
    other_recorder_running,
):
    process = make_process_info(1, "alice", 123)
    store = FakeMetadataStore([process])
    manager = LiveProcessManager(
        store,
        {"recordings_path": str(tmp_path)},
    )

    async def stop_process(*args, **kwargs):
        return True

    async def start_recording(*args, **kwargs):
        return ProcessStartResult(
            False,
            None,
            None,
            "RACE_CONDITION: User went offline between detection and recording start",
        )

    manager.stop_process = stop_process
    manager.start_live_recording = start_recording
    system_recorders = []
    if other_recorder_running:
        system_recorders.append(make_recorder(456, "alice", tmp_path / "other.mp4", 0))
    manager.get_system_recorder_processes = lambda: system_recorders

    with caplog.at_level(logging.DEBUG, logger="PROC"):
        result = await manager.replace_exact_process(
            process,
            "recorder process is dead",
            "stopped_dead",
            True,
        )

    assert result is False
    expected_resets = [] if other_recorder_running else ["alice"]
    assert store.reset_users == expected_resets
    matching = [
        record
        for record in caplog.records
        if "Failed to start replacement" in record.getMessage()
    ]
    assert [record.levelname for record in matching] == ["DEBUG"]


@pytest.mark.asyncio
async def test_repeated_zero_data_replacement_obeys_cooldown(tmp_path):
    first = make_process_info(1, "alice", 123)
    second = make_process_info(2, "alice", 124)
    manager = LiveProcessManager(
        FakeMetadataStore([first, second]),
        {
            "recordings_path": str(tmp_path),
            "no_stream_data_retry_cooldown": 300,
        },
    )
    starts = []

    async def stop_process(*args, **kwargs):
        return True

    async def start_recording(*args, **kwargs):
        starts.append((args, kwargs))
        return ProcessStartResult(True, 3, 125, None, started_new=True)

    manager.stop_process = stop_process
    manager.start_live_recording = start_recording

    first_result = await manager.replace_exact_process(
        first,
        "recorder produced no stream data",
        "stopped_no_stream_data",
        True,
    )
    second_result = await manager.replace_exact_process(
        second,
        "recorder produced no stream data",
        "stopped_no_stream_data",
        True,
    )

    assert first_result is True
    assert second_result is True
    assert len(starts) == 1
    assert starts[0][1]["ignore_no_stream_data_cooldown"] is True
    assert manager.stats["processes_restarted"] == 1


@pytest.mark.asyncio
async def test_normal_start_is_blocked_during_zero_data_cooldown(tmp_path):
    manager = StartTestManager(
        FakeMetadataStore(),
        {
            "recordings_path": str(tmp_path),
            "detached_process_mode": True,
            "max_live_processes": 100,
        },
    )
    manager.no_stream_data_cooldowns["alice"] = time.monotonic() + 300

    result = await manager.start_live_recording("alice")

    assert result.success is False
    assert result.error.startswith("NO_STREAM_DATA_COOLDOWN:")
    assert manager.spawn_count == 0


@pytest.mark.asyncio
async def test_candidate_metadata_changes_only_after_verified_stop(tmp_path):
    output = tmp_path / "alice.mp4"
    output.touch()
    tracked = make_process_info(1, "alice", 101, output)
    keeper_info = make_process_info(2, "alice", 102, output)
    process = make_recorder(101, "alice", output, 10)
    keeper = make_recorder(102, "alice", output, 20)
    candidate = OrphanProcessCandidate(
        process,
        "redundant active duplicate",
        process_id=1,
        kept_pid=102,
        kept_fingerprint=keeper.fingerprint,
    )
    store = FakeMetadataStore([tracked, keeper_info])
    manager = LiveProcessManager(store, {"recordings_path": str(tmp_path)})
    manager.get_system_recorder_processes = lambda: [process, keeper]

    async def failed_terminate(pid, graceful=True, *, expected):
        return False

    manager._terminate_process = failed_terminate
    result = await manager.stop_orphan_candidates([candidate])

    assert result == {101: False}
    assert store.stopped == []
    assert store.health_updates == [(1, "termination_failed", 0)]

    async def successful_terminate(pid, graceful=True, *, expected):
        return True

    manager._terminate_process = successful_terminate
    result = await manager.stop_orphan_candidates([candidate])

    assert result == {101: True}
    assert store.stopped == [(1, "stopped")]


@pytest.mark.asyncio
async def test_orphans_only_requires_confirmation(monkeypatch, capsys):
    process = make_recorder(301, "carol", "/tmp/carol.mp4", 10)
    candidate = OrphanProcessCandidate(process, "no matching active database record")

    class FakeProcessManager:
        def __init__(self):
            self.stop_calls = []
            self.plan_calls = 0

        def build_orphan_cleanup_plan(self):
            self.plan_calls += 1
            return [candidate]

        async def stop_orphan_candidates(self, candidates, force=False):
            self.stop_calls.append((candidates, force))
            return {301: True}

    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.process_manager = FakeProcessManager()
    monkeypatch.setattr("builtins.input", lambda: "n")

    await supervisor.stop_all_processes(orphans_only=True)

    assert supervisor.process_manager.stop_calls == []
    assert "Cancelled" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_orphans_only_stops_exact_reviewed_candidates(monkeypatch, capsys):
    process = make_recorder(301, "carol", "/tmp/carol.mp4", 10)
    candidate = OrphanProcessCandidate(process, "no matching active database record")

    class FakeProcessManager:
        def __init__(self):
            self.stop_calls = []
            self.plan_calls = 0

        def build_orphan_cleanup_plan(self):
            self.plan_calls += 1
            return [candidate]

        async def stop_orphan_candidates(self, candidates, force=False):
            self.stop_calls.append((candidates, force))
            return {301: True}

    supervisor = object.__new__(SeleniumSupervisor)
    supervisor.process_manager = FakeProcessManager()
    monkeypatch.setattr("builtins.input", lambda: "y")

    await supervisor.stop_all_processes(force=False, orphans_only=True)

    assert supervisor.process_manager.stop_calls == [([candidate], False)]
    assert supervisor.process_manager.plan_calls == 1
    assert "1/1" in capsys.readouterr().out


def test_recorder_signal_handler_does_not_reset_global_user_status():
    source = Path("recorder/main.py").read_text(encoding="utf-8")
    assert "cleanup_user_status" not in source
    assert "recorder.request_stop()" in source


def test_recorder_session_end_does_not_reset_global_live_status():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, is_live INTEGER NOT NULL);
        CREATE TABLE lives (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            ended_at TEXT
        );
        INSERT INTO users (id, is_live) VALUES (1, 1);
        INSERT INTO lives (id, user_id, ended_at) VALUES (10, 1, NULL);
        """
    )

    end_live_session(
        conn,
        10,
        datetime(2026, 7, 18, 1, 2, 3),
        reset_user_status=False,
    )

    assert conn.execute("SELECT ended_at FROM lives WHERE id = 10").fetchone()[0] == "2026-07-18 01:02:03"
    assert conn.execute("SELECT is_live FROM users WHERE id = 1").fetchone()[0] == 1
