from pathlib import Path

from scripts.selenium_live_monitor import (
    WebDriverManager,
    chrome_cmdline_owns_profile,
    cleanup_stale_chrome_profile_artifacts,
    cleanup_chrome_profile_owner,
)


def test_cleanup_stale_chrome_profile_artifacts_removes_dead_lock_and_related_files(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "SingletonLock").symlink_to("host-999999")
    (profile_dir / "SingletonCookie").symlink_to("cookie-target")
    (profile_dir / "SingletonSocket").symlink_to("/tmp/socket-target")
    (profile_dir / "DevToolsActivePort").write_text("12345\n/devtools/browser/test\n")

    removed = cleanup_stale_chrome_profile_artifacts(
        profile_dir,
        pid_exists=lambda pid: False,
    )

    assert removed is True
    assert not (profile_dir / "SingletonLock").exists()
    assert not (profile_dir / "SingletonCookie").exists()
    assert not (profile_dir / "SingletonSocket").exists()
    assert not (profile_dir / "DevToolsActivePort").exists()


def test_cleanup_stale_chrome_profile_artifacts_keeps_live_lock(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "SingletonLock").symlink_to("host-1234")
    (profile_dir / "SingletonCookie").symlink_to("cookie-target")
    (profile_dir / "SingletonSocket").symlink_to("/tmp/socket-target")
    (profile_dir / "DevToolsActivePort").write_text("12345\n/devtools/browser/test\n")

    removed = cleanup_stale_chrome_profile_artifacts(
        profile_dir,
        pid_exists=lambda pid: True,
    )

    assert removed is False
    assert (profile_dir / "SingletonLock").is_symlink()
    assert (profile_dir / "SingletonCookie").is_symlink()
    assert (profile_dir / "SingletonSocket").is_symlink()
    assert (profile_dir / "DevToolsActivePort").exists()


def test_cleanup_stale_chrome_profile_artifacts_removes_orphan_devtools_file_without_lock(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "DevToolsActivePort").write_text("12345\n/devtools/browser/test\n")

    removed = cleanup_stale_chrome_profile_artifacts(profile_dir)

    assert removed is True
    assert not (profile_dir / "DevToolsActivePort").exists()


def test_webdriver_manager_resolves_profile_and_cache_paths_from_config_base_dir(
    tmp_path: Path,
) -> None:
    manager = WebDriverManager(
        {
            "chrome_profile_path": "./tmp/selenium_chrome_profile",
            "cache_dir": "tmp/cache",
        },
        config_base_dir=str(tmp_path),
    )

    profile_path = manager._resolve_chrome_profile_path()
    cache_path = manager._resolve_cache_dir()
    chrome_options = manager._get_chrome_options()

    assert profile_path == tmp_path / "tmp" / "selenium_chrome_profile"
    assert cache_path == tmp_path / "tmp" / "cache"
    assert f"--user-data-dir={profile_path}" in chrome_options.arguments
    assert f"--disk-cache-dir={cache_path}" in chrome_options.arguments


def test_cleanup_chrome_profile_owner_terminates_chrome_using_profile(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "SingletonLock").symlink_to("host-1234")
    (profile_dir / "SingletonCookie").symlink_to("cookie-target")
    (profile_dir / "SingletonSocket").symlink_to("/tmp/socket-target")
    killed_signals: list[int] = []
    live_pids = {1234}

    def pid_exists(pid: int) -> bool:
        return pid in live_pids

    def read_cmdline(pid: int) -> list[str]:
        assert pid == 1234
        return ["chromium", f"--user-data-dir={profile_dir}"]

    def kill_pid(pid: int, signal_number: int) -> None:
        assert pid == 1234
        killed_signals.append(signal_number)
        live_pids.discard(pid)

    removed = cleanup_chrome_profile_owner(
        profile_dir,
        pid_exists=pid_exists,
        read_cmdline=read_cmdline,
        kill_pid=kill_pid,
        wait_timeout_seconds=0,
    )

    assert removed is True
    assert killed_signals
    assert not (profile_dir / "SingletonLock").exists()
    assert not (profile_dir / "SingletonCookie").exists()
    assert not (profile_dir / "SingletonSocket").exists()


def test_chrome_cmdline_owns_profile_handles_single_string_cmdline(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    assert chrome_cmdline_owns_profile(
        [
            "/usr/lib/chromium/chromium --headless --no-sandbox "
            f"--user-data-dir={profile_dir} about:blank"
        ],
        profile_dir,
    )


def test_cleanup_chrome_profile_owner_keeps_non_chrome_profile_owner(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "SingletonLock").symlink_to("host-1234")
    killed_signals: list[int] = []

    removed = cleanup_chrome_profile_owner(
        profile_dir,
        pid_exists=lambda pid: True,
        read_cmdline=lambda pid: ["python", "worker.py"],
        kill_pid=lambda pid, signal_number: killed_signals.append(signal_number),
        wait_timeout_seconds=0,
    )

    assert removed is False
    assert killed_signals == []
    assert (profile_dir / "SingletonLock").is_symlink()


def test_cleanup_chrome_profile_owner_keeps_chrome_using_different_profile(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    other_profile_dir = tmp_path / "other-profile"
    profile_dir.mkdir()
    other_profile_dir.mkdir()
    (profile_dir / "SingletonLock").symlink_to("host-1234")
    killed_signals: list[int] = []

    removed = cleanup_chrome_profile_owner(
        profile_dir,
        pid_exists=lambda pid: True,
        read_cmdline=lambda pid: ["chromium", f"--user-data-dir={other_profile_dir}"],
        kill_pid=lambda pid, signal_number: killed_signals.append(signal_number),
        wait_timeout_seconds=0,
    )

    assert removed is False
    assert killed_signals == []
    assert (profile_dir / "SingletonLock").is_symlink()
