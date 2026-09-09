import json
import os

import pytest

from utils.supervisor_lock import SupervisorLock


@pytest.fixture
def locks(tmp_path, monkeypatch):
    monkeypatch.setattr(SupervisorLock, '_get_version', lambda self: 'test')
    monkeypatch.setattr(SupervisorLock, '_is_process_running', lambda self, pid: True)
    path = tmp_path / 'supervisor.lock'
    first, second = SupervisorLock(str(path)), SupervisorLock(str(path))
    yield first, second, path
    first.remove_server_lock()
    second.remove_server_lock()


def test_racing_supervisors_cannot_both_acquire(locks, monkeypatch):
    first, second, path = locks
    assert first.check_server_lock() == second.check_server_lock() == (False, None)
    # Even an outdated advisory check must not bypass the OS lock.
    monkeypatch.setattr(second, 'check_server_lock', lambda: (False, None))
    assert first.create_server_lock()
    assert not second.create_server_lock()
    original = path.read_text()
    second.remove_server_lock()
    assert path.read_text() == original
    first.update_config_path(str(path.parent / 'custom.yaml'))
    assert json.loads(path.read_text())['config_path'].endswith('custom.yaml')
    assert first.remove_server_lock()
    assert second.create_server_lock()
    assert path.with_name(path.name + '.guard').exists()


def test_incomplete_metadata_does_not_allow_second_owner(locks):
    first, second, path = locks
    assert first.create_server_lock()
    path.write_text('{')
    assert second.check_server_lock() == (False, None)
    assert path.read_text() == '{'
    assert not second.create_server_lock()


def test_legacy_live_owner_is_respected(locks):
    first, _, path = locks
    path.write_text(json.dumps(dict(pid=12345, started_at='test', config_path='test', hostname='test')))
    assert not first.create_server_lock()


def test_stale_owner_can_be_replaced_after_guard_closes(locks, monkeypatch):
    first, second, path = locks
    assert first.create_server_lock()
    # Simulate kernel descriptor cleanup after exit, leaving stale metadata.
    os.close(first._guard_fd)
    first._guard_fd = None
    monkeypatch.setattr(second, '_is_process_running', lambda pid: False)
    assert second.create_server_lock()


@pytest.mark.parametrize('suffix', ['', '.guard'])
def test_lock_refuses_symlinks(locks, suffix):
    first, _, path = locks
    sentinel = path.parent / 'sentinel'
    sentinel.write_text('preserved')
    path.with_name(path.name + suffix).symlink_to(sentinel)
    assert not first.create_server_lock()
    assert sentinel.read_text() == 'preserved'
