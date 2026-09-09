from datetime import datetime
from unittest.mock import AsyncMock, Mock
import signal

import psutil
import pytest

from persistent_live_manager import live_process_manager as manager_module
from persistent_live_manager.live_process_manager import LiveProcessManager, ProcessStartResult
from persistent_live_manager.process_info import ProcessInfo
from persistent_live_manager.process_inventory import RecorderProcess


@pytest.fixture
def target(tmp_path, monkeypatch):
    output = str(tmp_path / 'alice.mp4')
    args = ('python', '-m', 'recorder.main', '-user', 'alice',
            '--persistent-mode', '--output-file', output)
    expected = RecorderProcess(12345, 'alice', output, 100.0, args)
    process = Mock()
    process.cmdline.return_value = list(args)
    process.create_time.return_value = 100.0
    process.status.return_value = psutil.STATUS_RUNNING
    process.is_running.return_value = True
    constructor = Mock(return_value=process)
    monkeypatch.setattr(manager_module.psutil, 'Process', constructor)
    monkeypatch.setattr(manager_module.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(manager_module.os, 'kill', Mock(side_effect=AssertionError('Raw PID signal')))
    manager = LiveProcessManager(Mock(), {'recordings_path': str(tmp_path)})
    return manager, process, expected, constructor


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['command', 'username', 'output', 'create_time'])
async def test_mismatched_process_is_never_signaled(target, mismatch):
    manager, process, expected, _ = target
    args = list(expected.cmdline)
    if mismatch == 'command':
        args = ['unrelated-service']
    elif mismatch == 'username':
        args[4] = 'bob'
    elif mismatch == 'output':
        args[-1] += '.other'
    else:
        process.create_time.return_value = 200.0
    process.cmdline.return_value = args
    assert not await manager._terminate_process(expected.pid, expected=expected)
    process.send_signal.assert_not_called()


@pytest.mark.asyncio
async def test_newer_recorder_cannot_be_stopped_using_old_database_row(target):
    manager, process, expected, _ = target
    row = ProcessInfo(1, 1, 'alice', expected.pid, expected.output_file,
                      datetime.fromtimestamp(90), None, 'healthy', 0, 0, True, None)
    assert not await manager._terminate_process(expected.pid, expected=row)
    process.send_signal.assert_not_called()
    row.started_at = datetime.fromtimestamp(110)
    process.is_running.return_value = False
    assert await manager._terminate_process(expected.pid, expected=row)
    process.send_signal.assert_called_once_with(signal.SIGTERM)


@pytest.mark.asyncio
async def test_pid_reused_during_wait_is_not_killed(target):
    manager, process, expected, _ = target
    # The retained psutil handle reports false when its PID has been reused.
    process.is_running.return_value = False
    assert await manager._terminate_process(expected.pid, expected=expected)
    process.send_signal.assert_called_once_with(signal.SIGTERM)


@pytest.mark.asyncio
async def test_force_kill_rechecks_command(target):
    manager, process, expected, _ = target
    process.cmdline.side_effect = [list(expected.cmdline), list(expected.cmdline), ['other']]
    assert not await manager._terminate_process(expected.pid, expected=expected)
    process.send_signal.assert_called_once_with(signal.SIGTERM)


@pytest.mark.asyncio
async def test_verified_recorder_can_be_force_killed(target):
    manager, process, expected, _ = target
    def send(sig):
        if sig == signal.SIGKILL:
            process.is_running.return_value = False
    process.send_signal.side_effect = send
    assert await manager._terminate_process(expected.pid, expected=expected)
    assert [call.args[0] for call in process.send_signal.call_args_list] == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.asyncio
@pytest.mark.parametrize('error, stopped', [(psutil.NoSuchProcess(12345), True), (psutil.AccessDenied(12345), False)])
async def test_missing_or_unreadable_process(target, error, stopped):
    manager, process, expected, constructor = target
    constructor.side_effect = error
    assert await manager._terminate_process(expected.pid, expected=expected) is stopped
    process.send_signal.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['recording file size decreased', 'recorder produced no stream data'])
async def test_failed_stop_blocks_replacement(target, reason):
    manager, _, expected, _ = target
    manager.stop_process = AsyncMock(return_value=False)
    manager.start_live_recording = AsyncMock(return_value=ProcessStartResult(True, 2, 12346, None, True))
    assert not await manager.replace_exact_process(expected, reason, 'stopped', True)
    manager.start_live_recording.assert_not_awaited()
    assert manager.no_stream_data_cooldowns == {}
