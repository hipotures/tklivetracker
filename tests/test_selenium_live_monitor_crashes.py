import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError, ReadTimeout
from selenium.common.exceptions import WebDriverException

from scripts import selenium_live_monitor
from scripts.selenium_live_monitor import (
    ActivitySimulator,
    EnhancedLiveMonitor,
    LiveUserDetector,
    SessionManager,
    quit_webdriver_with_timeout,
)


class DriverWithCrashOnGet:
    def get(self, _url):
        raise WebDriverException("Message: tab crashed\n  (Session info: chrome=146.0.7680.177)")


class DriverWithCrashOnCookies:
    def get_cookies(self):
        raise WebDriverException("Message: tab crashed\n  (Session info: chrome=146.0.7680.177)")


class DriverWithReadTimeoutOnGet:
    def get(self, _url):
        raise ReadTimeout(
            "HTTPConnectionPool(host='localhost', port=55219): Read timed out. (read timeout=120)"
        )


class DriverWithReadTimeoutOnCookies:
    def get_cookies(self):
        raise ReadTimeout(
            "HTTPConnectionPool(host='localhost', port=55219): Read timed out. (read timeout=120)"
        )


class DriverWithConnectionRefusedOnGet:
    def get(self, _url):
        raise RequestsConnectionError(
            'HTTPConnectionPool(host=\'localhost\', port=49201): Max retries exceeded with url: '
            '/session/e09ecf95789eb49077338f838fe4f9b8/url (Caused by '
            'NewConnectionError("HTTPConnection(host=\'localhost\', port=49201): '
            'Failed to establish a new connection: [Errno 111] Connection refused"))'
        )


class DriverWithConnectionRefusedOnCookies:
    def get_cookies(self):
        raise RequestsConnectionError(
            'HTTPConnectionPool(host=\'localhost\', port=49201): Max retries exceeded with url: '
            '/session/e09ecf95789eb49077338f838fe4f9b8/cookie (Caused by '
            'NewConnectionError("HTTPConnection(host=\'localhost\', port=49201): '
            'Failed to establish a new connection: [Errno 111] Connection refused"))'
        )


def test_check_live_users_propagates_tab_crash() -> None:
    detector = LiveUserDetector(DriverWithCrashOnGet(), {})

    with pytest.raises(WebDriverException, match="tab crashed"):
        detector.check_live_users()


def test_export_cookies_propagates_tab_crash() -> None:
    manager = SessionManager(DriverWithCrashOnCookies(), "unused.json")

    with pytest.raises(WebDriverException, match="tab crashed"):
        manager.export_cookies_for_apps()


def test_simulate_activity_propagates_tab_crash() -> None:
    simulator = ActivitySimulator(
        DriverWithCrashOnGet(),
        {
            "simulation_enabled": True,
            "simulation_pages": ["/explore"],
            "simulation_wait_min": 0,
            "simulation_wait_max": 0,
        },
    )

    with pytest.raises(WebDriverException, match="tab crashed"):
        simulator.simulate_activity()


def test_check_live_users_propagates_webdriver_transport_timeout() -> None:
    detector = LiveUserDetector(DriverWithReadTimeoutOnGet(), {})

    with pytest.raises(ReadTimeout, match="Read timed out"):
        detector.check_live_users()


def test_export_cookies_propagates_webdriver_transport_timeout() -> None:
    manager = SessionManager(DriverWithReadTimeoutOnCookies(), "unused.json")

    with pytest.raises(ReadTimeout, match="Read timed out"):
        manager.export_cookies_for_apps()


def test_simulate_activity_propagates_webdriver_transport_timeout() -> None:
    simulator = ActivitySimulator(
        DriverWithReadTimeoutOnGet(),
        {
            "simulation_enabled": True,
            "simulation_pages": ["/explore"],
            "simulation_wait_min": 0,
            "simulation_wait_max": 0,
        },
    )

    with pytest.raises(ReadTimeout, match="Read timed out"):
        simulator.simulate_activity()


def test_check_live_users_propagates_webdriver_connection_refused() -> None:
    detector = LiveUserDetector(DriverWithConnectionRefusedOnGet(), {})

    with pytest.raises(RequestsConnectionError, match="Connection refused"):
        detector.check_live_users()


def test_export_cookies_propagates_webdriver_connection_refused() -> None:
    manager = SessionManager(DriverWithConnectionRefusedOnCookies(), "unused.json")

    with pytest.raises(RequestsConnectionError, match="Connection refused"):
        manager.export_cookies_for_apps()


def test_simulate_activity_propagates_webdriver_connection_refused() -> None:
    simulator = ActivitySimulator(
        DriverWithConnectionRefusedOnGet(),
        {
            "simulation_enabled": True,
            "simulation_pages": ["/explore"],
            "simulation_wait_min": 0,
            "simulation_wait_max": 0,
        },
    )

    with pytest.raises(RequestsConnectionError, match="Connection refused"):
        simulator.simulate_activity()


def test_restart_webdriver_cleans_up_profile_owner_after_dead_session(monkeypatch, tmp_path) -> None:
    class DeadDriver:
        def quit(self):
            raise RequestsConnectionError("Connection refused")

    class DriverManager:
        def __init__(self):
            self.create_driver_called = False

        def _resolve_chrome_profile_path(self):
            return tmp_path / "profile"

        def create_driver(self):
            self.create_driver_called = True
            return object()

    cleanup_calls = []

    def cleanup_profile_owner(profile_path):
        cleanup_calls.append(profile_path)
        return True

    monkeypatch.setattr(
        selenium_live_monitor,
        "cleanup_chrome_profile_owner",
        cleanup_profile_owner,
    )

    monitor = EnhancedLiveMonitor.__new__(EnhancedLiveMonitor)
    monitor.driver = DeadDriver()
    monitor.driver_manager = DriverManager()
    monitor._touch_progress = lambda stage: None

    restarted = monitor._restart_webdriver()

    assert restarted is True
    assert cleanup_calls == [tmp_path / "profile"]
    assert monitor.driver_manager.create_driver_called is True


def test_quit_webdriver_with_timeout_returns_when_quit_hangs() -> None:
    class HangingDriver:
        def quit(self):
            import time

            time.sleep(60)

    assert quit_webdriver_with_timeout(HangingDriver(), timeout_seconds=0.01) is False


def test_restart_webdriver_continues_after_quit_timeout(monkeypatch, tmp_path) -> None:
    class HangingDriver:
        def quit(self):
            import time

            time.sleep(60)

    class DriverManager:
        def __init__(self):
            self.create_driver_called = False

        def _resolve_chrome_profile_path(self):
            return tmp_path / "profile"

        def create_driver(self):
            self.create_driver_called = True
            return object()

    cleanup_calls = []

    def cleanup_profile_owner(profile_path):
        cleanup_calls.append(profile_path)
        return True

    monkeypatch.setattr(
        selenium_live_monitor,
        "cleanup_chrome_profile_owner",
        cleanup_profile_owner,
    )
    monkeypatch.setattr(
        selenium_live_monitor.Constants,
        "WEBDRIVER_QUIT_TIMEOUT_SECONDS",
        0.01,
    )

    monitor = EnhancedLiveMonitor.__new__(EnhancedLiveMonitor)
    monitor.driver = HangingDriver()
    monitor.driver_manager = DriverManager()
    monitor._touch_progress = lambda stage: None

    restarted = monitor._restart_webdriver()

    assert restarted is True
    assert cleanup_calls == [tmp_path / "profile"]
    assert monitor.driver_manager.create_driver_called is True


def test_restart_webdriver_skips_recreate_when_shutdown_requested(monkeypatch, tmp_path) -> None:
    class DeadDriver:
        def quit(self):
            raise RequestsConnectionError("Connection refused")

    class DriverManager:
        def __init__(self):
            self.create_driver_called = False

        def _resolve_chrome_profile_path(self):
            return tmp_path / "profile"

        def create_driver(self):
            self.create_driver_called = True
            return object()

    cleanup_calls = []

    def cleanup_profile_owner(profile_path):
        cleanup_calls.append(profile_path)
        return True

    monkeypatch.setattr(
        selenium_live_monitor,
        "cleanup_chrome_profile_owner",
        cleanup_profile_owner,
    )

    monitor = EnhancedLiveMonitor.__new__(EnhancedLiveMonitor)
    monitor.driver = DeadDriver()
    monitor.driver_manager = DriverManager()
    monitor.shutdown_requested = True
    monitor._touch_progress = lambda stage: None

    restarted = monitor._restart_webdriver()

    assert restarted is False
    assert cleanup_calls == []
    assert monitor.driver_manager.create_driver_called is False


def test_run_monitor_does_not_restart_after_shutdown_during_webdriver_crash() -> None:
    class ConfigManager:
        def get_refresh_interval(self):
            return 60

    class SessionManagerStub:
        def check_login_status(self):
            return True

    monitor = EnhancedLiveMonitor.__new__(EnhancedLiveMonitor)
    monitor.config_manager = ConfigManager()
    monitor.session_manager = SessionManagerStub()
    monitor.running = True
    monitor.shutdown_requested = False
    monitor.force_restart_requested = False
    monitor._cycle_id = 0
    monitor._touch_progress = lambda stage: None
    monitor._handle_pending_driver_restart = lambda: False

    def crash_during_shutdown():
        monitor.shutdown_requested = True
        raise RequestsConnectionError("Connection refused")

    monitor.live_user_detector = type(
        "LiveUserDetectorStub",
        (),
        {"check_live_users": staticmethod(crash_during_shutdown)},
    )()
    monitor._restart_webdriver = lambda: pytest.fail("WebDriver restart attempted during shutdown")
    monitor._force_process_restart = lambda reason: pytest.fail(
        "Process restart attempted during shutdown"
    )
    shutdown_calls = []
    monitor._perform_shutdown = lambda: shutdown_calls.append(True)

    monitor.run_monitor()

    assert shutdown_calls == [True]
