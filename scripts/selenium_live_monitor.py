#!/usr/bin/env python3
"""
TkLiveTracker Selenium live monitor with SQLite-backed observations.
"""

import time
import json
import os
import signal
import shlex
import sys
import sqlite3
import random
import logging
import shutil
import re
import stat
import tempfile
import yaml
import argparse
import warnings
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple, Callable
from contextlib import contextmanager
from urllib.parse import unquote

# Add project root to path for shared imports when running as a script
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

from utils.config_paths import (
    absolute_config_path,
    normalize_config_paths,
    resolve_path,
)
from utils.username import normalize_tiktok_username
from utils.screenshot_retention import (
    build_cleanup_candidate_dir,
    build_screenshot_path,
    delete_expired_screenshot_dir,
    resolve_screenshots_dir,
)

# Configure SQLite datetime adapter to fix Python 3.12+ deprecation warning
def adapt_datetime(ts):
    """Convert datetime to ISO 8601 string for SQLite storage"""
    return ts.isoformat()

def convert_datetime(ts):
    """Convert ISO 8601 string back to datetime object"""
    return datetime.fromisoformat(ts.decode())

# Register the adapter and converter
sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("DATETIME", convert_datetime)


class Constants:
    """Centralized constants to avoid magic numbers"""
    DEFAULT_REFRESH_INTERVAL = 60
    DEFAULT_PERIODIC_RESTART_INTERVAL = 3600
    PERIODIC_RESTART_POLL_INTERVAL = 5
    DEFAULT_HANG_WATCHDOG_TIMEOUT = 180
    DEFAULT_HANG_WATCHDOG_GRACE = 15
    HANG_WATCHDOG_POLL_INTERVAL = 5
    CAPTCHA_PAUSE_SECONDS = 300  # 5 minutes
    PAGE_LOAD_TIMEOUT = 3
    DEFAULT_WAIT = 2
    SHUTDOWN_CHECK_INTERVAL = 1
    WEBDRIVER_QUIT_TIMEOUT_SECONDS = 10
    CHROME_DEFAULT_WINDOW_SIZE = "3840,2160"  # 4K
    MAX_ANCESTOR_LEVELS = 4

    # File paths
    DEFAULT_LOG_FILE = 'selenium.log'
    DEFAULT_DB_PATH = './selenium_live.db'
    DEFAULT_SCREENSHOTS_DIR = 'tmp/screenshots'
    DEFAULT_SCREENSHOT_RETENTION_DAYS = 30
    DEFAULT_CHROME_PROFILE = './private/selenium_chrome_profile'
    SESSION_FILE = 'private/selenium_session.json'

    # Other constants
    VERIFICATION_PAUSE_SECONDS = 300
    ACTIVITY_SIM_DEFAULT_MIN = 10
    ACTIVITY_SIM_DEFAULT_MAX = 30
    DB_TIMEOUT = 30.0


FATAL_WEBDRIVER_ERROR_MARKERS = (
    "tab crashed",
    "session deleted because of page crash",
    "chrome not reachable",
    "target window already closed",
    "invalid session id",
    "disconnected",
    "not connected to devtools",
    "connection refused",
)

FATAL_WEBDRIVER_TRANSPORT_LOCALHOST_MARKERS = (
    "httpconnectionpool(host='localhost'",
    "httpconnection(host='localhost'",
)

FATAL_WEBDRIVER_TRANSPORT_FAILURE_MARKERS = (
    "read timed out",
    "max retries exceeded",
    "failed to establish a new connection",
    "connection refused",
)


def is_fatal_webdriver_error(error: Exception | str) -> bool:
    """Return True when Selenium session is no longer usable and needs restart."""
    error_message = str(error).lower()

    if (
        any(marker in error_message for marker in FATAL_WEBDRIVER_TRANSPORT_LOCALHOST_MARKERS)
        and any(marker in error_message for marker in FATAL_WEBDRIVER_TRANSPORT_FAILURE_MARKERS)
    ):
        return True

    if isinstance(error, WebDriverException):
        return any(marker in error_message for marker in FATAL_WEBDRIVER_ERROR_MARKERS)

    if "session info: chrome=" in error_message:
        return any(marker in error_message for marker in FATAL_WEBDRIVER_ERROR_MARKERS)

    return False


def raise_if_fatal_webdriver_error(error: Exception) -> None:
    """Re-raise Selenium crashes so the main loop can restart WebDriver."""
    if is_fatal_webdriver_error(error):
        raise error


def print_inverse_terminal_banner(lines: List[str], stream=None) -> None:
    """Print a high-visibility terminal banner without adding ANSI codes to files."""
    stream = stream or sys.stdout
    width = max(72, max(len(line) for line in lines) + 4)
    inverse = "\033[7m" if stream.isatty() else ""
    reset = "\033[0m" if inverse else ""

    print(file=stream)
    print(f"{inverse}{' ' * width}{reset}", file=stream)
    for line in lines:
        print(f"{inverse}{line.center(width)}{reset}", file=stream)
    print(f"{inverse}{' ' * width}{reset}", file=stream)
    print(file=stream)


def parse_chrome_singleton_lock_pid(profile_dir: Path) -> Optional[int]:
    """Extract Chrome owner pid from SingletonLock symlink, if present."""
    lock_path = Path(profile_dir) / "SingletonLock"
    if not lock_path.is_symlink():
        return None

    try:
        lock_target = os.readlink(lock_path)
        return int(lock_target.rsplit("-", 1)[-1])
    except Exception:
        return None


def cleanup_stale_chrome_profile_artifacts(
    profile_dir: Path,
    pid_exists: Optional[Callable[[int], bool]] = None,
) -> bool:
    """
    Remove stale Chrome singleton artifacts left after a crashed browser.

    Returns True when any stale artifact was removed.
    """
    if pid_exists is None:
        pid_exists = lambda pid: Path(f"/proc/{pid}").exists()

    removed_any = False
    profile_dir = Path(profile_dir)
    lock_path = profile_dir / "SingletonLock"

    def remove_artifact(path: Path) -> bool:
        if not path.exists() and not path.is_symlink():
            return False
        path.unlink()
        return True

    def remove_related_artifacts() -> bool:
        removed = False
        for artifact_name in (
            "SingletonLock",
            "SingletonCookie",
            "SingletonSocket",
            "DevToolsActivePort",
        ):
            artifact_path = profile_dir / artifact_name
            try:
                removed = remove_artifact(artifact_path) or removed
            except Exception as artifact_error:
                logging.warning(
                    "⚠️ Failed to remove stale Chrome artifact %s: %s",
                    artifact_path,
                    artifact_error,
                )
        return removed

    if lock_path.is_symlink():
        try:
            pid = parse_chrome_singleton_lock_pid(profile_dir)
            if pid is None:
                raise ValueError("could not parse SingletonLock pid")
            if not pid_exists(pid):
                logging.warning(
                    "🧹 Removing stale Chrome profile lock from dead pid=%s in %s",
                    pid,
                    profile_dir,
                )
                removed_any = remove_related_artifacts() or removed_any
        except Exception as lock_error:
            logging.warning(
                "⚠️ Could not inspect Chrome profile lock %s: %s. Removing stale artifacts defensively.",
                lock_path,
                lock_error,
            )
            removed_any = remove_related_artifacts() or removed_any
    elif not lock_path.exists():
        devtools_port = profile_dir / "DevToolsActivePort"
        try:
            if remove_artifact(devtools_port):
                logging.info("🧹 Removed orphaned DevToolsActivePort from %s", profile_dir)
                removed_any = True
        except Exception as devtools_error:
            logging.warning(
                "⚠️ Failed to remove orphaned DevToolsActivePort %s: %s",
                devtools_port,
                devtools_error,
            )

    return removed_any


def default_read_process_cmdline(pid: int) -> List[str]:
    """Read process command line from procfs."""
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        raw_cmdline = cmdline_path.read_bytes()
    except Exception:
        return []

    return [part.decode(errors="replace") for part in raw_cmdline.split(b"\0") if part]


def chrome_cmdline_owns_profile(cmdline: List[str], profile_dir: Path) -> bool:
    """Return True when cmdline appears to be Chrome using this profile directory."""
    if not cmdline:
        return False

    if len(cmdline) == 1 and " " in cmdline[0]:
        try:
            cmdline = shlex.split(cmdline[0])
        except ValueError:
            return False

    executable = Path(cmdline[0]).name.lower()
    if not any(name in executable for name in ("chrome", "chromium")):
        return False

    expected_profile_dir = str(Path(profile_dir).resolve())
    for arg in cmdline:
        if not arg.startswith("--user-data-dir="):
            continue
        configured_profile_dir = arg.split("=", 1)[1]
        try:
            configured_profile_dir = str(Path(configured_profile_dir).resolve())
        except Exception:
            pass
        return configured_profile_dir == expected_profile_dir

    return False


def cleanup_chrome_profile_owner(
    profile_dir: Path,
    pid_exists: Optional[Callable[[int], bool]] = None,
    read_cmdline: Optional[Callable[[int], List[str]]] = None,
    kill_pid: Optional[Callable[[int, int], None]] = None,
    wait_timeout_seconds: float = 5.0,
) -> bool:
    """
    Terminate a Chrome process that still owns this profile after WebDriver died.

    This is intentionally conservative: it only signals a process whose command
    line is Chrome/Chromium and whose --user-data-dir matches the configured
    profile directory.
    """
    profile_dir = Path(profile_dir)
    pid = parse_chrome_singleton_lock_pid(profile_dir)
    if pid is None:
        return cleanup_stale_chrome_profile_artifacts(profile_dir, pid_exists=pid_exists)

    if pid_exists is None:
        pid_exists = lambda checked_pid: Path(f"/proc/{checked_pid}").exists()
    if read_cmdline is None:
        read_cmdline = default_read_process_cmdline
    if kill_pid is None:
        kill_pid = os.kill

    if not pid_exists(pid):
        return cleanup_stale_chrome_profile_artifacts(profile_dir, pid_exists=pid_exists)

    cmdline = read_cmdline(pid)
    if not chrome_cmdline_owns_profile(cmdline, profile_dir):
        logging.warning(
            "⚠️ Chrome profile lock pid=%s is live but does not match expected Chrome profile owner; leaving it in place",
            pid,
        )
        return False

    logging.warning("🧹 Terminating Chrome profile owner pid=%s for %s", pid, profile_dir)
    try:
        kill_pid(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception as kill_error:
        logging.warning("⚠️ Failed to terminate Chrome profile owner pid=%s: %s", pid, kill_error)
        return False

    deadline = time.monotonic() + max(0.0, wait_timeout_seconds)
    while pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.2)

    if pid_exists(pid):
        logging.warning("🧹 Chrome profile owner pid=%s still alive; sending SIGKILL", pid)
        try:
            kill_pid(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as kill_error:
            logging.warning("⚠️ Failed to kill Chrome profile owner pid=%s: %s", pid, kill_error)
            return False

        deadline = time.monotonic() + max(0.0, wait_timeout_seconds)
        while pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.2)

    return cleanup_stale_chrome_profile_artifacts(profile_dir, pid_exists=pid_exists)


def quit_webdriver_with_timeout(driver, timeout_seconds: Optional[float] = None) -> bool:
    """Quit WebDriver without letting a dead local connection block recovery forever."""
    if timeout_seconds is None:
        timeout_seconds = Constants.WEBDRIVER_QUIT_TIMEOUT_SECONDS

    finished = threading.Event()
    result: Dict[str, Optional[Exception]] = {"error": None}

    def quit_driver() -> None:
        try:
            driver.quit()
        except Exception as error:
            result["error"] = error
        finally:
            finished.set()

    thread = threading.Thread(
        target=quit_driver,
        name="webdriver-quit-timeout",
        daemon=True,
    )
    thread.start()

    if not finished.wait(timeout=max(0.0, timeout_seconds)):
        logging.warning("⚠️ WebDriver quit timed out after %.1fs; continuing recovery", timeout_seconds)
        return False

    if result["error"] is not None:
        logging.warning(f"⚠️ Error during driver cleanup: {result['error']}")
        return False

    return True


class WebDriverManager:
    """Manages Chrome driver lifecycle and configuration"""

    def __init__(
        self,
        selenium_config: Dict[str, Any],
        headless_off: bool = False,
        config_base_dir: Optional[str] = None,
    ):
        self.selenium_config = selenium_config
        self.headless_off = headless_off
        self.config_base_dir = config_base_dir
        self.driver = None

    def create_driver(self) -> webdriver.Chrome:
        """Create and configure Chrome driver"""
        mode = "visible" if self.headless_off else "headless"
        logging.info(f"🔧 Setting up Chrome driver ({mode} mode)...")
        print(f"🔧 Setting up Chrome driver ({mode} mode)...")

        chrome_profile_path = self._resolve_chrome_profile_path()
        cache_dir = self._resolve_cache_dir()
        chrome_options = self._get_chrome_options()
        removed_stale_artifacts = cleanup_stale_chrome_profile_artifacts(chrome_profile_path)
        logging.info(
            "🗂️ Chrome paths: profile=%s cache=%s stale_cleanup=%s",
            chrome_profile_path,
            cache_dir,
            removed_stale_artifacts,
        )

        try:
            print("📱 Creating Chrome driver...")
            self.driver = webdriver.Chrome(
                options=chrome_options,
                service=self._get_chrome_service(chrome_profile_path),
            )
            print("✅ Chrome driver created")
            self._log_driver_capabilities()

            print("🔧 Setting up anti-detection...")
            self._setup_anti_detection()
            print("✅ Anti-detection configured")

            logging.info(f"✅ Chrome driver ready ({mode} mode)")
            return self.driver

        except Exception as e:
            logging.error(f"❌ Failed to setup Chrome: {e}")
            print(f"❌ Failed to setup Chrome: {e}")
            raise

    def _get_chrome_options(self) -> Options:
        """Get Chrome options with all configurations"""
        chrome_options = Options()

        chrome_binary_path = self._resolve_chrome_binary_path()
        if chrome_binary_path:
            chrome_options.binary_location = str(chrome_binary_path)

        # Use existing Chrome profile directory for persistent session
        chrome_profile_path = self._resolve_chrome_profile_path()
        chrome_options.add_argument(f"--user-data-dir={chrome_profile_path}")

        # Cache directory for faster page loading
        cache_dir = self._resolve_cache_dir()
        chrome_options.add_argument(f"--disk-cache-dir={cache_dir}")

        # Fast page loading - don't wait for images/stylesheets
        chrome_options.page_load_strategy = 'eager'

        # Only add headless if not disabled
        if not self.headless_off:
            chrome_options.add_argument("--headless")

        # Standard options
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument(f"--window-size={Constants.CHROME_DEFAULT_WINDOW_SIZE}")

        # Enhanced anti-detection measures
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # Additional stealth options
        chrome_options.add_argument("--disable-features=VizDisplayCompositor")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-background-networking")

        # Only disable images in headless mode for performance
        if not self.headless_off:
            chrome_options.add_argument("--disable-images")

        chrome_options.add_argument("--no-first-run")
        chrome_options.add_argument("--no-default-browser-check")
        chrome_options.add_argument("--disable-default-apps")

        # Real user agent
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        return chrome_options

    def _get_chrome_service(self, chrome_profile_path: Path) -> Service:
        """Create ChromeDriver service with verbose logs in the profile directory."""
        chrome_profile_path.mkdir(parents=True, exist_ok=True)
        driver_log_path = chrome_profile_path / "chromedriver.log"
        return Service(
            log_output=str(driver_log_path),
            service_args=["--verbose"],
        )

    def _log_driver_capabilities(self) -> None:
        """Log resolved browser/driver versions after a session is created."""
        if not self.driver:
            return

        capabilities = self.driver.capabilities or {}
        chrome_info = capabilities.get("chrome", {}) or {}
        logging.info(
            "🧭 Chrome session: browserName=%s browserVersion=%s chromedriver=%s binary=%s",
            capabilities.get("browserName"),
            capabilities.get("browserVersion"),
            chrome_info.get("chromedriverVersion"),
            self._describe_configured_or_discovered_binary(),
        )

    def _resolve_chrome_binary_path(self) -> Optional[Path]:
        """Resolve optional configured Chrome binary path."""
        raw_path = self.selenium_config.get('chrome_binary_path')
        if not raw_path:
            return None
        return Path(resolve_path(raw_path, base_dir=self.config_base_dir))

    def _describe_configured_or_discovered_binary(self) -> str:
        configured_binary = self._resolve_chrome_binary_path()
        if configured_binary:
            return f"configured:{configured_binary}"

        discovered = {
            name: shutil.which(name)
            for name in ("chromium", "google-chrome", "chrome")
            if shutil.which(name)
        }
        return f"auto:{discovered}"

    def _resolve_chrome_profile_path(self) -> Path:
        """Resolve Chrome profile path relative to config directory."""
        raw_path = self.selenium_config.get(
            'chrome_profile_path',
            Constants.DEFAULT_CHROME_PROFILE
        )
        return Path(resolve_path(raw_path, base_dir=self.config_base_dir))

    def _resolve_cache_dir(self) -> Path:
        """Resolve Chrome cache path relative to config directory."""
        raw_path = self.selenium_config.get('cache_dir', 'tmp/cache')
        return Path(resolve_path(raw_path, base_dir=self.config_base_dir))

    def _setup_anti_detection(self) -> None:
        """Setup anti-detection JavaScript"""
        if not self.driver:
            raise RuntimeError("Driver not created yet")

        # Enhanced anti-detection scripts
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.driver.execute_script("Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]})")
        self.driver.execute_script("Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']})")
        self.driver.execute_script("Object.defineProperty(navigator, 'permissions', {get: () => undefined})")
        self.driver.execute_script("window.chrome = { runtime: {} }")

    def cleanup(self) -> None:
        """Clean up driver resources"""
        if self.driver:
            try:
                logging.info("🔒 Closing browser...")
                # Suppress warnings during shutdown
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self.driver.quit()
                logging.info("🔒 Browser closed successfully")
            except Exception:
                # Silent shutdown - warnings are normal during browser shutdown
                logging.info("🔒 Browser closed")
            finally:
                self.driver = None


def _atomic_write_cookie_json(path: os.PathLike | str, data: Dict[str, str]) -> None:
    """Write credential JSON atomically without following a destination symlink."""
    destination = Path(path)
    parent = destination.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    try:
        if stat.S_ISLNK(destination.lstat().st_mode):
            raise ValueError(f"Refusing to replace symlinked cookie file: {destination}")
    except FileNotFoundError:
        pass

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=parent
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as cookie_file:
            descriptor = -1
            json.dump(data, cookie_file, indent=2)
            cookie_file.flush()
            os.fsync(cookie_file.fileno())
        os.replace(temporary_path, destination)
        if os.name == "posix":
            os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


class SessionManager:
    """Manages TikTok session authentication and cookies"""

    def __init__(self, driver, cookie_export_path: os.PathLike | str):
        self.driver = driver
        self.cookie_export_path = Path(cookie_export_path)

    def load_session(self) -> bool:
        """Load session cookies"""
        session_file = Constants.SESSION_FILE

        try:
            with open(session_file, 'r') as f:
                session_data = json.load(f)

            logging.info(f"📥 Loading session with {len(session_data)} cookies...")

            # Go to TikTok first
            self.driver.get("https://www.tiktok.com")
            time.sleep(Constants.DEFAULT_WAIT)

            # Add session cookies
            for name, value in session_data.items():
                try:
                    self.driver.add_cookie({
                        'name': name,
                        'value': str(value),
                        'domain': '.tiktok.com',
                        'path': '/'
                    })
                except Exception as e:
                    logging.warning(f"⚠️ Couldn't add cookie {name}: {e}")

            # Refresh to apply cookies
            self.driver.refresh()
            time.sleep(Constants.DEFAULT_WAIT)

            logging.info("✅ Session loaded")
            return True

        except FileNotFoundError:
            logging.warning(f"⚠️ No session file found: {session_file}")
            return False
        except Exception as e:
            raise_if_fatal_webdriver_error(e)
            logging.error(f"❌ Error loading session: {e}")
            return False

    def check_login_status(self) -> Optional[bool]:
        """Return True/False for a confirmed login state, or None when unclear."""
        try:
            self.driver.get("https://www.tiktok.com/live")
            time.sleep(Constants.PAGE_LOAD_TIMEOUT)

            current_url = str(getattr(self.driver, "current_url", "")).lower()
            if "/login" in current_url:
                logging.error("❌ TikTok session is logged out (redirected to login)")
                return False

            logged_out_indicators = [
                "//button[@data-e2e='top-login-button']",
                "//a[contains(@href, '/login')]",
                "//button[normalize-space(.)='Log in']",
                "//button[normalize-space(.)='Zaloguj się']",
                "//*[@role='dialog']//button[normalize-space()='Log in']",
                "//*[@role='dialog']//button[normalize-space()='Zaloguj się']",
                "//*[normalize-space(.)='What would you like to watch on TikTok?']",
            ]
            for indicator in logged_out_indicators:
                try:
                    elements = self.driver.find_elements(By.XPATH, indicator)
                    if any(element.is_displayed() for element in elements):
                        logging.error("❌ TikTok session is logged out (login prompt is visible)")
                        return False
                except Exception as error:
                    raise_if_fatal_webdriver_error(error)

            cookies = self.driver.get_cookies()
            authenticated_cookie_names = {"sessionid", "sessionid_ss", "sid_tt"}
            if any(
                cookie.get("name") in authenticated_cookie_names and cookie.get("value")
                for cookie in cookies
            ):
                logging.info("✅ Login confirmed by authenticated session cookie")
                return True

            profile_indicators = [
                "//div[@data-e2e='profile-icon']",
                "//button[@data-e2e='top-profile-avatar']",
                "//a[contains(@href, '/profile')]",
            ]

            for indicator in profile_indicators:
                try:
                    elements = self.driver.find_elements(By.XPATH, indicator)
                    if any(element.is_displayed() for element in elements):
                        logging.info("✅ Login confirmed")
                        return True
                except Exception as error:
                    raise_if_fatal_webdriver_error(error)

            logging.warning("⚠️ TikTok login status could not be confirmed")
            return None

        except Exception as e:
            raise_if_fatal_webdriver_error(e)
            logging.error(f"❌ Error checking login: {e}")
            return False

    def export_cookies_for_apps(self) -> bool:
        """Export current cookies for other applications"""
        try:
            cookies = self.driver.get_cookies()
            session_data = {}
            for cookie in cookies:
                session_data[cookie['name']] = cookie['value']

            _atomic_write_cookie_json(self.cookie_export_path, session_data)

            logging.info(f"📤 Cookies exported for other apps: {len(session_data)} cookies")
            return True

        except Exception as e:
            raise_if_fatal_webdriver_error(e)
            logging.warning(f"⚠️ Error exporting cookies: {e}")
            return False


class LiveUserDetector:
    """Detects live users on TikTok and handles verification/screenshots"""

    def __init__(
        self,
        driver,
        selenium_config: Dict[str, Any],
        config_base_dir: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None
    ):
        self.driver = driver
        self.selenium_config = selenium_config
        self.config_base_dir = config_base_dir
        self.progress_callback = progress_callback

    def _touch_progress(self, stage: str) -> None:
        """Report progress heartbeat to monitor, if callback is provided."""
        if self.progress_callback:
            try:
                self.progress_callback(stage)
            except Exception:
                pass

    def _wait_with_progress(self, total_seconds: int, stage: str, poll_seconds: int = 5) -> None:
        """Wait while periodically emitting heartbeat checkpoints."""
        if total_seconds <= 0:
            return

        deadline = time.monotonic() + total_seconds
        while True:
            self._touch_progress(stage)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(poll_seconds, remaining))

    def check_verification_page(self) -> bool:
        """Check if page has verification/captcha"""
        try:
            # Check URL and title
            current_url = self.driver.current_url
            page_title = self.driver.title.lower()

            if "captcha" in current_url or "verify" in current_url or "blocked" in page_title:
                logging.warning("🚫 TikTok verification/block detected")
                return True

            # Check for verification texts
            verification_texts = [
                "drag the slider to fit the puzzle",
                "play the audio and enter the code",
                "verify you are human",
                "complete the captcha",
                "security verification"
            ]

            page_source = self.driver.page_source.lower()
            for text in verification_texts:
                if text in page_source:
                    logging.warning(f"🚫 Verification challenge detected: '{text}'")
                    return True

            return False

        except Exception as e:
            raise_if_fatal_webdriver_error(e)
            logging.error(f"Error checking verification: {e}")
            return False

    def take_screenshot_if_enabled(self, element=None) -> None:
        """Take screenshot if enabled in config"""
        try:
            screenshots_enabled = self.selenium_config.get('screenshots_enabled', False)
            if not screenshots_enabled:
                return

            screenshots_dir = self._get_screenshots_dir_path()
            if screenshots_dir is None:
                logging.warning("⚠️ Screenshot directory is null/empty/invalid - skipping screenshot")
                return

            current_time = datetime.now().replace(microsecond=0)
            self._cleanup_expired_screenshot_dir(screenshots_dir, current_time)

            screenshot_path = build_screenshot_path(screenshots_dir, current_time)
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)

            # Take screenshot - element specific or full page
            if element:
                element.screenshot(str(screenshot_path))
                logging.info(f"📸 Element screenshot saved: {screenshot_path}")
            else:
                self.driver.save_screenshot(str(screenshot_path))
                logging.info(f"📸 Full page screenshot saved: {screenshot_path}")

        except Exception as e:
            raise_if_fatal_webdriver_error(e)
            logging.error(f"❌ Error taking screenshot: {e}")

    def _get_screenshots_dir_path(self) -> Optional[Path]:
        """Resolve screenshots_dir safely without falling back from explicit null/empty values."""
        raw_screenshots_dir = self.selenium_config.get('screenshots_dir')
        if 'screenshots_dir' not in self.selenium_config:
            raw_screenshots_dir = Constants.DEFAULT_SCREENSHOTS_DIR

        return resolve_screenshots_dir(raw_screenshots_dir, base_dir=self.config_base_dir)

    def _get_screenshots_retention_days(self) -> int:
        """Get screenshot retention days from config with a safe default."""
        raw_retention_days = self.selenium_config.get(
            'screenshots_retention_days',
            Constants.DEFAULT_SCREENSHOT_RETENTION_DAYS
        )

        try:
            return int(raw_retention_days)
        except (TypeError, ValueError):
            logging.warning(
                "⚠️ Invalid selenium.screenshots_retention_days=%r, using default=%s",
                raw_retention_days,
                Constants.DEFAULT_SCREENSHOT_RETENTION_DAYS
            )
            return Constants.DEFAULT_SCREENSHOT_RETENTION_DAYS

    def _cleanup_expired_screenshot_dir(self, screenshots_dir: Path, current_time: datetime) -> None:
        """Delete only the one date directory that is now older than retention."""
        retention_days = self._get_screenshots_retention_days()
        if retention_days <= 0:
            return

        cleanup_candidate = build_cleanup_candidate_dir(
            screenshots_dir,
            current_time,
            retention_days
        )

        try:
            deleted = delete_expired_screenshot_dir(
                screenshots_dir,
                cleanup_candidate,
                project_dir=Path(self.config_base_dir).resolve() if self.config_base_dir else None
            )
        except Exception as e:
            logging.warning(f"⚠️ Failed to cleanup expired screenshot directory: {e}")
            return

        if deleted:
            logging.info(f"🧹 Deleted expired screenshot directory: {cleanup_candidate}")

    def filter_navigation_elements(self, elements: List) -> List:
        """Filter out navigation and UI elements"""
        filtered_elements = []

        for element in elements:
            try:
                element_class = element.get_attribute("class") or ""
                if "TUXButton" in element_class or "navigation" in element_class.lower():
                    continue
                filtered_elements.append(element)
            except Exception:
                filtered_elements.append(element)

        return filtered_elements

    @staticmethod
    def extract_username_from_href(href: str) -> str:
        if not href:
            return ""

        match = re.search(r"/@([^/?#]+)", href)
        if not match:
            return ""

        username = unquote(match.group(1)).strip()
        return normalize_tiktok_username(username, strip_at=False) or ""

    def extract_username_from_live_element(self, element) -> str:
        try:
            link = element.find_element(By.XPATH, "./ancestor::a[contains(@href, '/@')][1]")
            username = self.extract_username_from_href(link.get_attribute("href") or "")
            if username:
                return username
        except Exception as e:
            raise_if_fatal_webdriver_error(e)

        username = element.text.strip()
        return normalize_tiktok_username(username, strip_at=False) or ""

    def check_live_users(self) -> List[Tuple[str, int]]:
        """Check live users on /live page - returns list of (username, viewer_count) tuples"""
        logging.info("🔴 Checking live users...")

        try:
            # Navigate to live page
            self.driver.get("https://www.tiktok.com/live")
            time.sleep(Constants.PAGE_LOAD_TIMEOUT)

            # Check for verification page
            if self.check_verification_page():
                self._wait_with_progress(Constants.VERIFICATION_PAUSE_SECONDS, "captcha_pause")
                return []

            # Quick scroll to load content
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)

            # Get usernames and viewer counts from first section (Following)
            live_users_with_viewers = []
            try:
                # Find all sections with live-side-nav-channel
                all_sections = self.driver.find_elements(By.XPATH, "//div[@data-e2e='live-side-nav-channel']")

                # Look for Following section specifically
                following_section = None
                for section in all_sections:
                    try:
                        # Check if this section contains Following text or is the first section when multiple exist
                        # The Following section should be first when it exists with content
                        section_text = section.text.lower()
                        if 'following' in section_text or 'obserwowani' in section_text or len(all_sections) > 1:
                            # When there are multiple sections, first is Following
                            if all_sections.index(section) == 0 and len(all_sections) > 1:
                                following_section = section
                                logging.info("📍 Found Following section (first of multiple sections)")
                                break
                            # Or if section explicitly says "following"
                            elif 'following' in section_text or 'obserwowani' in section_text:
                                following_section = section
                                logging.info("📍 Found Following section by text match")
                                break
                    except Exception as e:
                        logging.debug(f"Error checking section text: {e}")
                        continue

                # If no Following section found or only one section exists (which would be Suggested)
                if not following_section:
                    logging.info("😴 No Following section found - no followed users are live")
                    return []

                # Check if Following section has any users
                username_elements_check = following_section.find_elements(By.XPATH, ".//span[@data-e2e='live-side-nav-name']")
                if not username_elements_check:
                    logging.info("😴 Following section is empty - no followed users are live")
                    return []

                # Try to click "Show all" button in Following section
                try:
                    show_all_button = following_section.find_element(By.XPATH, ".//div[@data-e2e='live-side-more-button']")
                    if show_all_button.is_displayed():
                        # Wait for button to be clickable then click
                        wait = WebDriverWait(self.driver, 5)
                        wait.until(EC.element_to_be_clickable((By.XPATH, ".//div[@data-e2e='live-side-more-button']")))
                        show_all_button.click()

                        # Wait for new elements to load after expanding list
                        try:
                            wait.until(lambda driver: len(driver.find_elements(By.XPATH, "//div[@data-e2e='live-side-nav-name']")) > 0)
                        except TimeoutException:
                            pass  # List may already be fully expanded

                        # Scroll the entire page down to load all elements
                        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

                        # Wait for scroll to complete and content to stabilize
                        try:
                            wait.until(lambda driver: driver.execute_script("return document.readyState") == "complete")
                        except TimeoutException:
                            pass

                        # Scroll again to ensure we reach the bottom
                        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                        logging.info("📖 Clicked 'Show all' button in Following section")
                except:
                    logging.info("📖 No 'Show all' button found or already expanded")

                # Scroll within the Following section to load all users
                try:
                    self.driver.execute_script("arguments[0].scrollTo(0, arguments[0].scrollHeight);", following_section)
                    time.sleep(1)
                except:
                    pass

                # Find all live username elements only within the first section
                username_elements = following_section.find_elements(By.XPATH, ".//span[@data-e2e='live-side-nav-name']")

                logging.info(f"📍 Found {len(username_elements)} users in Following section")

                # Track usernames to avoid duplicates
                processed_usernames = set()

                for element in username_elements:
                        username = self.extract_username_from_live_element(element)
                        if username and username not in processed_usernames:
                            processed_usernames.add(username)

                            # Find viewer count in the same container - improved XPath
                            viewer_count = -1  # Default: viewer count unavailable (TikTok UI issue)
                            try:
                                # First try to find user container - but don't require person-count to exist
                                user_container = None

                                # Method 1: Try to find container that has person-count descendant
                                try:
                                    user_container = element.find_element(By.XPATH, "./ancestor::*[descendant::*[@data-e2e='person-count']][1]")
                                except NoSuchElementException:
                                    # Method 2: If no person-count found, try to find the closest parent container
                                    try:
                                        user_container = element.find_element(By.XPATH, "./ancestor::*[contains(@class, 'item') or self::div or self::li][1]")
                                    except NoSuchElementException:
                                        # Method 3: Fallback to immediate parent
                                        user_container = element.find_element(By.XPATH, "./..")

                                if user_container:
                                    # Look for person-count within this container
                                    viewer_elements = user_container.find_elements(By.XPATH, ".//div[@data-e2e='person-count']")

                                    if viewer_elements:
                                        viewer_element = viewer_elements[0]
                                        viewer_text = viewer_element.text.strip()

                                        logging.debug(f"🔍 Raw viewer text for @{username}: '{viewer_text}'")

                                        # Convert viewer count to integer (handle potential 'K', 'M' suffixes)
                                        if viewer_text:
                                            if viewer_text.endswith('K') or viewer_text.endswith('k'):
                                                viewer_count = int(float(viewer_text[:-1]) * 1000)
                                            elif viewer_text.endswith('M') or viewer_text.endswith('m'):
                                                viewer_count = int(float(viewer_text[:-1]) * 1000000)
                                            else:
                                                # Remove any non-digit characters except dots for decimals
                                                clean_text = ''.join(c for c in viewer_text if c.isdigit() or c == '.')
                                                if clean_text:
                                                    viewer_count = int(float(clean_text))
                                                else:
                                                    viewer_count = 0
                                        else:
                                            viewer_count = 0
                                    else:
                                        # No person-count element found - this is OK during TikTok UI glitches
                                        logging.debug(f"🔍 No viewer count displayed for @{username} (TikTok UI issue)")
                                        viewer_count = -1  # Unavailable, not zero
                                else:
                                    logging.debug(f"🔍 Could not locate user container for @{username}")
                                    viewer_count = -1  # Unavailable, not zero

                            except NoSuchElementException as e:
                                logging.debug(f"🔍 Could not find viewer count container for @{username}: {e}")
                                viewer_count = -1  # Unavailable, not zero
                            except ValueError as e:
                                logging.warning(f"⚠️ Could not parse viewer count for @{username}: {e}")
                                viewer_count = 0
                            except Exception as e:
                                raise_if_fatal_webdriver_error(e)
                                logging.error(f"❌ Unexpected error getting viewer count for @{username}: {e}")
                                viewer_count = -1  # Unavailable due to error

                            live_users_with_viewers.append((username, viewer_count))
                            viewer_display = "viewers unavailable" if viewer_count == -1 else f"{viewer_count} viewers"
                            logging.info(f"🔴 Found live user: @{username} ({viewer_display})")

            except Exception as e:
                raise_if_fatal_webdriver_error(e)
                logging.error(f"❌ Error finding live usernames in Following section: {e}")
                live_users_with_viewers = []

            logging.info(f"📊 Total live users found: {len(live_users_with_viewers)}")

            # Take screenshot if enabled (element screenshot only)
            try:
                self.take_screenshot_if_enabled(following_section)
            except:
                self.take_screenshot_if_enabled()

            return live_users_with_viewers

        except Exception as e:
            raise_if_fatal_webdriver_error(e)
            logging.error(f"❌ Error checking live users: {e}")
            return []


class ConfigurationManager:
    """Handles all configuration loading and management"""

    def __init__(self, config_path: str = 'config.yaml'):
        self.config_path = absolute_config_path(config_path)
        self.config_dir = os.path.dirname(self.config_path)
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        try:
            with open(self.config_path, 'r') as f:
                config = yaml.safe_load(f)
            return normalize_config_paths(config or {}, self.config_path)
        except FileNotFoundError:
            logging.warning(f"Config file not found: {self.config_path}, using defaults")
            return {}
        except Exception as e:
            logging.error(f"Error loading config: {e}, using defaults")
            return {}

    def get_selenium_config(self) -> Dict[str, Any]:
        """Get selenium-specific configuration"""
        return self.config.get('selenium', {})

    def get_log_file(self) -> str:
        """Get log file path from configuration, combining log_path with selenium log_file"""
        # Get base log directory from paths.log_path
        log_path_raw = self.config.get('paths', {}).get('log_path', './logs')
        log_dir = resolve_path(log_path_raw, base_dir=self.config_dir)

        # Get selenium log filename from logging.selenium_log_file or fallback to selenium.log_file
        log_filename = (self.config.get('logging', {}).get('selenium_log_file') or
                       self.get_selenium_config().get('log_file', Constants.DEFAULT_LOG_FILE))

        # Combine directory and filename
        return os.path.join(log_dir, log_filename)

    def get_database_path(self) -> str:
        """Get database path from configuration"""
        selenium_config = self.get_selenium_config()
        return resolve_path(
            selenium_config.get('database_path') or selenium_config.get('db_path', Constants.DEFAULT_DB_PATH),
            base_dir=self.config_dir
        )

    def get_cookie_json_path(self) -> str:
        """Return the shared cookie JSON path used by producer and consumers."""
        cookie_path = self.config.get("cookies", {}).get(
            "cookie_json_file", "./private/cookies_full.json"
        )
        return resolve_path(cookie_path, base_dir=self.config_dir)

    def get_refresh_interval(self) -> int:
        """Get refresh interval from configuration"""
        return self.get_selenium_config().get('refresh_interval', Constants.DEFAULT_REFRESH_INTERVAL)

    def get_periodic_restart_interval(self) -> int:
        """Get hard restart interval for Selenium monitor process (seconds)"""
        configured_value = self.get_selenium_config().get(
            'periodic_restart_interval',
            Constants.DEFAULT_PERIODIC_RESTART_INTERVAL
        )

        try:
            interval = int(configured_value)
            return max(60, interval)  # Safety floor
        except (TypeError, ValueError):
            logging.warning(
                "Invalid selenium.periodic_restart_interval=%r, using default=%s",
                configured_value,
                Constants.DEFAULT_PERIODIC_RESTART_INTERVAL
            )
            return Constants.DEFAULT_PERIODIC_RESTART_INTERVAL

    def is_periodic_restart_enabled(self) -> bool:
        """Get periodic hard restart flag"""
        return bool(self.get_selenium_config().get('periodic_restart_enabled', True))

    def is_hang_watchdog_enabled(self) -> bool:
        """Get hang watchdog flag"""
        return bool(self.get_selenium_config().get('hang_watchdog_enabled', True))

    def get_hang_timeout_seconds(self) -> int:
        """Get no-progress timeout for hang watchdog (seconds)"""
        configured_value = self.get_selenium_config().get(
            'hang_timeout_seconds',
            Constants.DEFAULT_HANG_WATCHDOG_TIMEOUT
        )
        try:
            return max(30, int(configured_value))
        except (TypeError, ValueError):
            logging.warning(
                "Invalid selenium.hang_timeout_seconds=%r, using default=%s",
                configured_value,
                Constants.DEFAULT_HANG_WATCHDOG_TIMEOUT
            )
            return Constants.DEFAULT_HANG_WATCHDOG_TIMEOUT

    def get_hang_grace_seconds(self) -> int:
        """Get grace period after unblocking action before hard restart (seconds)"""
        configured_value = self.get_selenium_config().get(
            'hang_grace_seconds',
            Constants.DEFAULT_HANG_WATCHDOG_GRACE
        )
        try:
            return max(5, int(configured_value))
        except (TypeError, ValueError):
            logging.warning(
                "Invalid selenium.hang_grace_seconds=%r, using default=%s",
                configured_value,
                Constants.DEFAULT_HANG_WATCHDOG_GRACE
            )
            return Constants.DEFAULT_HANG_WATCHDOG_GRACE


class SeleniumLiveDB:
    """SQLite database handler for selenium live data with improved resource management"""

    def __init__(self, db_path: str):
        self.db_path = resolve_path(db_path)
        self.init_database()

    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        conn = sqlite3.connect(
            self.db_path,
            timeout=Constants.DB_TIMEOUT,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
        try:
            yield conn
        finally:
            conn.close()

    def init_database(self) -> None:
        """Initialize SQLite database with started_at/ended_at schema"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Create table with new schema
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS selenium_live (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user TEXT NOT NULL,
                    started_at DATETIME NOT NULL,
                    ended_at DATETIME DEFAULT NULL,
                    max_viewers INTEGER DEFAULT 0,
                    UNIQUE(user, started_at)
                )
            ''')

            # Create indexes for performance
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_started_at ON selenium_live(started_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ended_at ON selenium_live(ended_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_started ON selenium_live(user, started_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_max_viewers ON selenium_live(max_viewers)')

            conn.commit()
            logging.info(f"✅ SQLite database initialized: {self.db_path}")

    def get_last_live_users(self) -> List[str]:
        """Get users who were processed in the most recent monitoring cycle"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            try:
                # Find the most recent monitoring cycle by looking at the latest started_at time
                # This represents when the monitoring system last ran
                cursor.execute('''
                    SELECT MAX(started_at) FROM selenium_live
                ''')

                result = cursor.fetchone()
                if not result or not result[0]:
                    logging.info("📋 No previous monitoring data found")
                    return []

                most_recent_cycle = result[0]

                # Get users with most recent ended_at (those who were live in last check)
                cursor.execute('''
                    SELECT user FROM selenium_live
                    WHERE ended_at = (SELECT MAX(ended_at) FROM selenium_live WHERE ended_at IS NOT NULL)
                       OR ended_at IS NULL
                ''')

                users = [row[0] for row in cursor.fetchall()]
                logging.info(f"📋 Retrieved {len(users)} users from last check")
                return users

            except sqlite3.Error as e:
                logging.error(f"❌ Database error getting last live users: {e}")
                return []
            except Exception as e:
                logging.error(f"❌ Unexpected error getting last live users: {e}")
                return []

    def update_live_users(self, current_live_users: List[Tuple[str, int]]) -> None:
        """Update live users with viewer count tracking"""
        current_time = datetime.now().replace(microsecond=0)
        last_users = self.get_last_live_users()

        with self.get_connection() as conn:
            cursor = conn.cursor()

            try:
                inserts = 0
                updates = 0

                if current_live_users:
                    for user, viewer_count in current_live_users:
                        if user not in last_users:
                            # INSERT - new live user (started streaming)
                            cursor.execute('''
                                INSERT INTO selenium_live (user, started_at, ended_at, max_viewers) VALUES (?, ?, ?, ?)
                            ''', (user, current_time, current_time, viewer_count))
                            inserts += 1
                            viewer_display = "viewers unavailable" if viewer_count == -1 else f"{viewer_count} viewers"
                            logging.info(f"🟢 STARTED: New live user @{user} with {viewer_display}")
                        else:
                            # UPDATE - continuing live user (update ended_at and max_viewers if higher)
                            cursor.execute('''
                                UPDATE selenium_live
                                SET ended_at = ?,
                                    max_viewers = CASE
                                        WHEN ? > max_viewers THEN ?
                                        ELSE max_viewers
                                    END
                                WHERE user = ? AND started_at = (
                                    SELECT MAX(started_at) FROM selenium_live WHERE user = ?
                                )
                            ''', (current_time, viewer_count, viewer_count, user, user))
                            updates += 1

                            # Get current max_viewers for logging
                            cursor.execute('''
                                SELECT max_viewers FROM selenium_live
                                WHERE user = ? AND started_at = (
                                    SELECT MAX(started_at) FROM selenium_live WHERE user = ?
                                )
                            ''', (user, user))
                            max_viewers_result = cursor.fetchone()
                            max_viewers = max_viewers_result[0] if max_viewers_result else viewer_count

                            viewer_display = "viewers unavailable" if viewer_count == -1 else f"{viewer_count} viewers"
                            max_viewer_display = "unavailable" if max_viewers == -1 else str(max_viewers)
                            logging.info(f"🔄 ACTIVE: Continuing live user @{user} ({viewer_display}, max: {max_viewer_display})")

                if current_live_users:
                    logging.info(f"✅ Database updated: {inserts} NEW streams, {updates} ACTIVE streams @ {current_time}")
                else:
                    logging.info("😴 No live users detected")

                conn.commit()

            except sqlite3.Error as e:
                logging.error(f"❌ Database error updating live users: {e}")
                conn.rollback()
            except Exception as e:
                logging.error(f"❌ Unexpected error updating live users: {e}")
                conn.rollback()


class CaptchaDetector:
    """Detect and handle CAPTCHA challenges"""

    def __init__(self, driver):
        self.driver = driver
        self.captcha_keywords = [
            "captcha", "verify", "robot", "human", "challenge",
            "security", "suspicious", "unusual", "blocked"
        ]

    def detect_captcha(self) -> bool:
        """Detect if CAPTCHA is present on current page"""
        try:
            # Check page source for actual CAPTCHA text that exists
            page_source = self.driver.page_source.lower()
            captcha_phrases = [
                "drag the slider"
            ]

            for phrase in captcha_phrases:
                if phrase in page_source:
                    logging.warning(f"🚨 CAPTCHA detected: phrase '{phrase}' found")
                    return True

            return False

        except Exception as e:
            raise_if_fatal_webdriver_error(e)
            logging.error(f"❌ Error detecting CAPTCHA: {e}")
            return False

    def handle_captcha(self, pause_seconds: Optional[int] = None) -> None:
        """Handle CAPTCHA by pausing monitoring"""
        if pause_seconds is None:
            pause_seconds = Constants.CAPTCHA_PAUSE_SECONDS

        pause_minutes = pause_seconds / 60
        logging.warning(f"⏸️ CAPTCHA detected - pausing for {pause_minutes:.1f} minutes")
        time.sleep(pause_seconds)
        logging.info("▶️ Resuming after CAPTCHA pause")


class ActivitySimulator:
    """Simulate user activity between live checks"""

    def __init__(self, driver, config: Dict[str, Any]):
        self.driver = driver
        self.config = config
        self.simulation_pages = config.get('simulation_pages', ['/'])
        self.wait_min = config.get('simulation_wait_min', Constants.ACTIVITY_SIM_DEFAULT_MIN)
        self.wait_max = config.get('simulation_wait_max', Constants.ACTIVITY_SIM_DEFAULT_MAX)
        self.enabled = config.get('simulation_enabled', True)

    def simulate_activity(self) -> None:
        """Simulate user activity by visiting random pages"""
        if not self.enabled:
            logging.info("🚫 Activity simulation disabled")
            return

        try:
            # Choose random page
            page = random.choice(self.simulation_pages)
            url = f"https://www.tiktok.com{page}"

            logging.info(f"🎭 Simulating activity: visiting {url}")
            self.driver.get(url)

            # Wait for page to load
            time.sleep(Constants.DEFAULT_WAIT)

            # Try to interact with page elements
            self._interact_with_page()

            # Random wait
            wait_time = random.randint(self.wait_min, self.wait_max)
            logging.info(f"⏳ Simulation wait: {wait_time} seconds")
            time.sleep(wait_time)

        except Exception as e:
            raise_if_fatal_webdriver_error(e)
            logging.error(f"❌ Error in activity simulation: {e}")

    def _interact_with_page(self) -> None:
        """Try to interact with page elements"""
        try:
            # Look for safe clickable elements
            safe_selectors = [
                "//button[contains(@class, 'tiktok')]",
                "//div[contains(@class, 'video')]",
                "//a[contains(@href, '/@')]",
                "//div[contains(@class, 'user')]"
            ]

            for selector in safe_selectors:
                try:
                    elements = self.driver.find_elements(By.XPATH, selector)
                    if elements:
                        element = random.choice(elements)
                        if element.is_displayed():
                            # Scroll to element
                            self.driver.execute_script("arguments[0].scrollIntoView(true);", element)
                            time.sleep(1)

                            # Optional: click element (commented out for safety)
                            # element.click()
                            # logging.info(f"🖱️ Clicked element: {selector}")

                            break
                except Exception:
                    continue

        except Exception as e:
            raise_if_fatal_webdriver_error(e)
            logging.error(f"❌ Error interacting with page: {e}")


class EnhancedLiveMonitor:
    """TkLiveTracker Selenium monitor with SQLite and activity simulation."""

    def __init__(self, config_path: str = 'config.yaml', headless_off: bool = False):
        # Use ConfigurationManager for all config handling
        self.config_manager = ConfigurationManager(config_path)
        self.selenium_config = self.config_manager.get_selenium_config()
        self.headless_off = headless_off
        self.driver = None
        self.driver_manager = None
        self.running = True
        self.shutdown_requested = False
        self.shutdown_event = threading.Event()
        self.force_restart_requested = False
        self._force_restart_lock = threading.Lock()
        self._process_start_monotonic = time.monotonic()
        self._periodic_restart_thread = None
        self._hang_watchdog_thread = None
        self._restart_driver_lock = threading.Lock()
        self._restart_driver_requested = False
        self._restart_driver_reason = ""
        self._progress_lock = threading.Lock()
        self._cycle_id = 0
        self._last_progress_ts = time.monotonic()
        self._last_progress_stage = "init"
        self._last_progress_cycle = 0
        self._hang_recovery_in_progress = False

        # Setup logging AFTER loading config
        self._setup_logging()
        self._touch_progress("init_logging_done")

        # Start watchdog before WebDriver init, so it can recover from hard hangs.
        if not self.headless_off:
            self._start_periodic_restart_watchdog()
            self._start_hang_watchdog()

        # Initialize components
        self._touch_progress("init_db_start")
        self.db = SeleniumLiveDB(self.config_manager.get_database_path())
        self._touch_progress("init_db_done")

        # Initialize WebDriverManager
        self.driver_manager = WebDriverManager(
            self.selenium_config,
            headless_off,
            config_base_dir=self.config_manager.config_dir,
        )
        self._touch_progress("init_webdriver_start")
        self.driver = self.driver_manager.create_driver()
        self._touch_progress("init_webdriver_done")

        # Initialize dependent components
        self.session_manager = SessionManager(
            self.driver, self.config_manager.get_cookie_json_path()
        )
        self.live_user_detector = LiveUserDetector(
            self.driver,
            self.selenium_config,
            config_base_dir=self.config_manager.config_dir,
            progress_callback=self._touch_progress
        )
        self.captcha_detector = CaptchaDetector(self.driver)
        self.activity_simulator = ActivitySimulator(self.driver, self.selenium_config)

        # Setup signal handlers
        self._setup_signal_handlers()

        logging.info("🚀 Enhanced Live Monitor initialized")

    def _setup_logging(self) -> None:
        """Setup enhanced logging with centralized warning suppression"""
        log_file = self.config_manager.get_log_file()
        self._rotate_existing_log_file(log_file)

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file, mode='w'),
                logging.StreamHandler()
            ]
        )

        # Centralized suppression of connection-related warnings
        self._suppress_connection_warnings()

        logging.info(f"📝 Logging initialized: {log_file}")

    def _rotate_existing_log_file(self, log_file: str) -> None:
        """Rotate the existing selenium log at startup to keep the active file small."""
        if not os.path.exists(log_file):
            return

        mtime = os.path.getmtime(log_file)
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(mtime))
        rotated_log = f"{os.path.splitext(log_file)[0]}-{timestamp}.log"
        os.replace(log_file, rotated_log)
        print(f"Rotated existing log file to: {rotated_log}")

    def _suppress_connection_warnings(self) -> None:
        """Centralized warning suppression for connection-related messages"""
        # Suppress urllib3 and requests warnings
        warnings.filterwarnings("ignore", message=".*urllib3.*")
        warnings.filterwarnings("ignore", message=".*connection.*")
        warnings.filterwarnings("ignore", message=".*Connection refused.*")
        warnings.filterwarnings("ignore", message=".*NewConnectionError.*")
        warnings.filterwarnings("ignore", message=".*Retrying.*")

        # Suppress specific loggers
        loggers_to_suppress = [
            'urllib3.connectionpool',
            'urllib3.util.retry',
            'requests.packages.urllib3.connectionpool',
            'selenium.webdriver.remote.remote_connection'
        ]

        for logger_name in loggers_to_suppress:
            logging.getLogger(logger_name).setLevel(logging.CRITICAL)

    def _setup_signal_handlers(self) -> None:
        """Setup graceful shutdown"""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _touch_progress(self, stage: str) -> None:
        """Update progress heartbeat for hang watchdog."""
        with self._progress_lock:
            self._last_progress_ts = time.monotonic()
            self._last_progress_stage = stage
            self._last_progress_cycle = self._cycle_id
            self._hang_recovery_in_progress = False

    def _request_driver_restart(self, reason: str) -> None:
        """Queue WebDriver restart to be executed by the main monitor loop."""
        with self._restart_driver_lock:
            if self._restart_driver_requested:
                return
            self._restart_driver_requested = True
            self._restart_driver_reason = reason
        logging.info(f"🧭 Driver restart requested: {reason}")

    def _consume_driver_restart_request(self) -> Optional[str]:
        """Consume pending WebDriver restart request, if any."""
        with self._restart_driver_lock:
            if not self._restart_driver_requested:
                return None
            reason = self._restart_driver_reason or "unspecified"
            self._restart_driver_requested = False
            self._restart_driver_reason = ""
            return reason

    def _handle_pending_driver_restart(self) -> bool:
        """
        Handle queued WebDriver restart request.
        Returns True if a restart was attempted and caller should start a new cycle.
        """
        reason = self._consume_driver_restart_request()
        if not reason:
            return False

        logging.info(f"🔄 Executing queued WebDriver restart: {reason}")
        if self._restart_webdriver():
            self._touch_progress("after_queued_driver_restart")
            return True

        logging.error("❌ Queued WebDriver restart failed - forcing process restart")
        self._force_process_restart("queued WebDriver restart failed")
        return True

    def _sleep_with_progress(self, total_seconds: float, stage: str) -> None:
        """Sleep in short chunks and keep heartbeat fresh."""
        deadline = time.monotonic() + max(0.0, total_seconds)
        while not self.shutdown_requested and not self.force_restart_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            sleep_time = min(Constants.HANG_WATCHDOG_POLL_INTERVAL, remaining)
            self.shutdown_event.wait(timeout=sleep_time)
            self._touch_progress(stage)

    def _start_hang_watchdog(self) -> None:
        """Start watchdog for no-progress hangs in monitor loop."""
        if not self.config_manager.is_hang_watchdog_enabled():
            logging.info("⏭️ Hang watchdog disabled by config")
            return

        timeout_seconds = self.config_manager.get_hang_timeout_seconds()
        grace_seconds = self.config_manager.get_hang_grace_seconds()

        def watchdog_loop():
            while self.running and not self.shutdown_requested and not self.force_restart_requested:
                self.shutdown_event.wait(timeout=Constants.HANG_WATCHDOG_POLL_INTERVAL)
                if self.shutdown_requested or self.force_restart_requested:
                    return

                with self._progress_lock:
                    if self._hang_recovery_in_progress:
                        continue
                    stalled_for = time.monotonic() - self._last_progress_ts
                    if stalled_for < timeout_seconds:
                        continue
                    self._hang_recovery_in_progress = True
                    detection_ts = self._last_progress_ts
                    detection_stage = self._last_progress_stage
                    detection_cycle = self._last_progress_cycle

                # Re-check right before intervention to avoid acting on stale snapshot.
                with self._progress_lock:
                    if self._last_progress_ts > detection_ts:
                        self._hang_recovery_in_progress = False
                        continue

                # Keep this at INFO so the incident is always visible in normal logs.
                logging.info(
                    "🛟 HANG WATCHDOG: no progress for %.1fs (stage=%s, cycle=%s)",
                    stalled_for,
                    detection_stage,
                    detection_cycle
                )
                logging.warning("🛟 Hang watchdog action: killing browser descendants to unblock Selenium")

                self._request_driver_restart(
                    f"hang watchdog: stalled {stalled_for:.1f}s at stage={detection_stage}, cycle={detection_cycle}"
                )
                self._kill_descendant_processes_now()
                self.shutdown_event.wait(timeout=grace_seconds)

                with self._progress_lock:
                    recovered = self._last_progress_ts > detection_ts
                    recovered_stage = self._last_progress_stage
                    recovered_cycle = self._last_progress_cycle
                    if recovered:
                        self._hang_recovery_in_progress = False

                if recovered:
                    logging.info(
                        "✅ HANG WATCHDOG: progress resumed at stage=%s, cycle=%s",
                        recovered_stage,
                        recovered_cycle
                    )
                    continue

                logging.info(
                    "🛟 HANG WATCHDOG: still no progress after %ss grace period - forcing process restart",
                    grace_seconds
                )
                self._force_process_restart(
                    f"hang watchdog hard restart after {stalled_for:.1f}s stall "
                    f"(stage={detection_stage}, cycle={detection_cycle})"
                )
                return

        self._hang_watchdog_thread = threading.Thread(
            target=watchdog_loop,
            name="hang-watchdog",
            daemon=False
        )
        self._hang_watchdog_thread.start()
        logging.info(
            "⏱️ Hang watchdog enabled: timeout=%ss, grace=%ss",
            timeout_seconds,
            grace_seconds
        )

    def _start_periodic_restart_watchdog(self) -> None:
        """Start watchdog that hard-restarts the process on interval."""
        if not self.config_manager.is_periodic_restart_enabled():
            logging.info("⏭️ Periodic Selenium hard restart disabled by config")
            return

        interval = self.config_manager.get_periodic_restart_interval()

        def watchdog_loop():
            while self.running and not self.shutdown_requested and not self.force_restart_requested:
                elapsed = time.monotonic() - self._process_start_monotonic
                if elapsed >= interval:
                    self._force_process_restart(
                        f"scheduled hard restart after {interval}s uptime"
                    )
                    return

                remaining = max(0, interval - elapsed)
                sleep_time = min(Constants.PERIODIC_RESTART_POLL_INTERVAL, remaining)
                self.shutdown_event.wait(timeout=sleep_time)

        self._periodic_restart_thread = threading.Thread(
            target=watchdog_loop,
            name="periodic-hard-restart-watchdog",
            daemon=False
        )
        self._periodic_restart_thread.start()
        logging.info(f"⏱️ Periodic Selenium hard restart enabled: every {interval}s")

    def _kill_descendant_processes_now(self) -> None:
        """Kill all child processes immediately without waiting for Selenium response."""
        try:
            # Build ppid -> children map from ps output.
            result = subprocess.run(
                ["ps", "-e", "-o", "pid=,ppid="],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode != 0:
                logging.warning("⚠️ Failed to list child processes before forced restart")
                return

            tree: Dict[int, List[int]] = {}
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) != 2:
                    continue
                try:
                    pid = int(parts[0])
                    ppid = int(parts[1])
                except ValueError:
                    continue
                tree.setdefault(ppid, []).append(pid)

            descendants: List[int] = []
            stack = list(tree.get(os.getpid(), []))
            while stack:
                pid = stack.pop()
                descendants.append(pid)
                stack.extend(tree.get(pid, []))

            for pid in sorted(descendants, reverse=True):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as kill_error:
                    logging.warning(f"⚠️ Failed to kill child pid={pid}: {kill_error}")

            if descendants:
                logging.warning(f"💥 Force-killed {len(descendants)} child processes before restart")
        except Exception as e:
            logging.warning(f"⚠️ Failed during forced child process cleanup: {e}")

    def _force_process_restart(self, reason: str) -> None:
        """Hard-restart current process immediately, without graceful WebDriver shutdown."""
        with self._force_restart_lock:
            if self.force_restart_requested:
                return
            self.force_restart_requested = True

        self.shutdown_requested = True
        self.shutdown_event.set()

        logging.warning(f"🔁 FORCED RESTART: {reason}")
        print(f"\n🔁 FORCED RESTART: {reason}")
        print("💥 Skipping driver.quit(); killing child processes and re-execing now...")

        self._kill_descendant_processes_now()

        python_exec = sys.executable
        argv = [python_exec] + sys.argv

        try:
            os.execv(python_exec, argv)
        except Exception as e:
            logging.error(f"❌ Forced restart failed during execv: {e}")
            os._exit(1)

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals gracefully"""
        if not self.shutdown_requested:
            self.shutdown_requested = True
            self.shutdown_event.set()  # Wake up any waiting threads
            signal_name = "SIGINT (Ctrl+C)" if signum == signal.SIGINT else f"signal {signum}"
            logging.info(f"🛑 Received {signal_name} - will shutdown after current cycle completes...")
            print(f"\n🛑 Received {signal_name} - will shutdown after current cycle completes...")
            print("⏳ Please wait for graceful shutdown...")
        else:
            logging.warning("⚠️ Shutdown already in progress, forcing exit...")
            print("\n⚠️ Shutdown already in progress, forcing exit...")
            if self.driver:
                try:
                    # Suppress warnings during forced shutdown
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        self.driver.quit()
                except Exception:
                    pass
            sys.exit(1)



    def run_monitor(self) -> None:
        """Main monitoring loop with improved shutdown handling"""
        refresh_interval = self.config_manager.get_refresh_interval()

        logging.info(f"🚀 Starting enhanced live monitor (refresh every {refresh_interval}s)")
        self._touch_progress("monitor_started")

        # Chrome profile handles session persistence
        logging.info("🔐 Using Chrome profile for session persistence")
        login_status = self.session_manager.check_login_status()
        if login_status is True:
            print("✅ TikTok login status: LOGGED IN")
        elif login_status is False:
            print_inverse_terminal_banner([
                "⛔ TIKTOK SESSION IS LOGGED OUT",
                "LIVE DETECTION RESULTS ARE NOT RELIABLE",
                "Run with --headless-off to restore the authenticated session.",
            ])
        else:
            print("⚠️ TikTok login status: UNKNOWN (could not confirm the session)")

        # Main loop
        while self.running and not self.shutdown_requested and not self.force_restart_requested:
            try:
                self._cycle_id += 1
                self._touch_progress("cycle_start")

                if self._handle_pending_driver_restart():
                    continue

                # Record start time of this cycle
                cycle_start_time = time.time()

                # Check live users
                self._touch_progress("before_check_live_users")
                live_users = self.live_user_detector.check_live_users()
                self._touch_progress("after_check_live_users")

                if self._handle_pending_driver_restart():
                    continue

                # Update database
                self._touch_progress("before_db_update")
                self.db.update_live_users(live_users)
                self._touch_progress("after_db_update")

                # Export cookies for other applications
                self._touch_progress("before_export_cookies")
                self.session_manager.export_cookies_for_apps()
                self._touch_progress("after_export_cookies")

                # Simulate activity
                self._touch_progress("before_simulate_activity")
                self.activity_simulator.simulate_activity()
                self._touch_progress("after_simulate_activity")

                # Calculate remaining time to wait
                elapsed_time = time.time() - cycle_start_time
                remaining_time = max(0, refresh_interval - elapsed_time)

                # Wait for next check with responsive shutdown
                if remaining_time > 0 and not self.shutdown_requested:
                    logging.info(f"⏳ Waiting {remaining_time:.1f}s until next check...")
                    self._sleep_with_progress(remaining_time, "waiting_next_cycle")

                elif remaining_time <= 0:
                    logging.info("⚡ Cycle took longer than refresh interval, starting next check immediately")
                    self._touch_progress("cycle_overtime")

            except KeyboardInterrupt:
                logging.info("⏹️ KeyboardInterrupt caught, graceful shutdown...")
                self.shutdown_requested = True
                break
            except Exception as e:
                self._touch_progress("monitor_exception")
                error_msg = str(e)
                if is_fatal_webdriver_error(e):
                    logging.warning(f"🔄 WebDriver crashed ({error_msg}), attempting restart...")
                    if self._restart_webdriver():
                        logging.info("✅ WebDriver restarted successfully, continuing monitoring...")
                        continue  # Try again immediately
                    else:
                        logging.error("❌ Failed to restart WebDriver - forcing process restart")
                        self._force_process_restart("WebDriver restart failed after crash")
                        return
                else:
                    logging.error(f"❌ Error in monitoring loop: {e}")
                    self._sleep_with_progress(10, "retry_after_monitor_error")

        # Graceful shutdown
        self._perform_shutdown()

    def _restart_webdriver(self) -> bool:
        """Restart WebDriver after crash"""
        try:
            logging.info("🔄 Attempting to restart WebDriver...")
            self._touch_progress("restarting_webdriver_start")

            # Clean up existing driver
            if self.driver:
                quit_webdriver_with_timeout(self.driver)
                self.driver = None

            profile_path = self.driver_manager._resolve_chrome_profile_path()
            cleanup_chrome_profile_owner(profile_path)

            if getattr(self, "shutdown_requested", False):
                logging.info("⏭️ Skipping WebDriver recreation because shutdown was requested")
                self._touch_progress("restarting_webdriver_skipped_shutdown")
                return False

            # Recreate driver using driver manager
            self.driver = self.driver_manager.create_driver()

            # Reinitialize components that use driver
            if hasattr(self, 'live_user_detector'):
                self.live_user_detector.driver = self.driver
            if hasattr(self, 'session_manager'):
                self.session_manager.driver = self.driver
            if hasattr(self, 'captcha_detector'):
                self.captcha_detector.driver = self.driver
            if hasattr(self, 'activity_simulator'):
                self.activity_simulator.driver = self.driver

            logging.info("✅ WebDriver restarted successfully")
            self._touch_progress("restarting_webdriver_done")
            return True

        except Exception as e:
            logging.error(f"❌ Failed to restart WebDriver: {e}")
            self._touch_progress("restarting_webdriver_failed")
            return False

    def _perform_shutdown(self) -> None:
        """Perform graceful shutdown"""
        if self.force_restart_requested:
            logging.info("⏭️ Skipping graceful shutdown because forced restart was requested")
            return

        if self.shutdown_requested:
            logging.info("✅ Graceful shutdown completed - cycle finished safely")
            print("✅ Graceful shutdown completed - cycle finished safely")
        else:
            logging.info("🛑 Enhanced monitoring stopped")

        # Clean up driver
        if self.driver:
            try:
                logging.info("🔒 Closing browser...")
                # Suppress warnings during shutdown
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self.driver.quit()
                logging.info("🔒 Browser closed successfully")
            except Exception:
                # Silent shutdown - warnings are normal during browser shutdown
                logging.info("🔒 Browser closed")

    def run_session_refresh(self) -> None:
        """Run in session refresh mode - just open browser and wait"""
        logging.info("🔄 Session refresh mode - opening browser for manual session update...")

        # Open TikTok
        self.driver.get("https://www.tiktok.com")
        time.sleep(Constants.DEFAULT_WAIT)

        logging.info("🔐 Using Chrome profile session - should be logged in if profile exists")

        print("\n" + "="*60)
        print("🔄 SESSION REFRESH MODE")
        print("="*60)
        print("1. Browser window is open - you can now:")
        print("   - Log in with a different account")
        print("   - Refresh current session by navigating around")
        print("   - Update cookies if needed")
        print("2. When done, press Enter in this terminal")
        print("3. The browser will close automatically")
        print("="*60)
        print("\nPress Enter to exit and save session...")

        try:
            # Keep browser open until user presses Enter
            input()
            logging.info("⏹️ Session refresh stopped by user")

            # Chrome profile handles persistence
            logging.info("ℹ️ Session refresh completed - Chrome profile updated")
            print("✅ Session refresh completed! Chrome profile and existing JSON cookies preserved.")
        except KeyboardInterrupt:
            logging.info("⏹️ Session refresh stopped by Ctrl+C")
            print("\nℹ️ Session not saved.")

        finally:
            if self.driver:
                try:
                    # Suppress warnings during shutdown
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        self.driver.quit()
                    logging.info("🔒 Browser closed")
                except Exception:
                    # Silent shutdown
                    logging.info("🔒 Browser closed")


def main():
    """Main function"""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='TkLiveTracker Selenium Live Monitor')
    parser.add_argument('--config', default='config.yaml',
                       help='Path to configuration file (default: config.yaml)')
    parser.add_argument('--headless-off', action='store_true',
                       help='Run in visible browser mode (for session refresh)')

    args = parser.parse_args()

    print("🔴 TkLiveTracker Selenium Live Monitor")
    print("=" * 50)

    if args.headless_off:
        print("🔄 Running in visible browser mode...")
        monitor = EnhancedLiveMonitor(config_path=args.config, headless_off=True)
        monitor.run_session_refresh()
    else:
        print("🚀 Running in headless monitoring mode...")
        monitor = EnhancedLiveMonitor(config_path=args.config)
        monitor.run_monitor()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logging.exception("❌ Unhandled fatal error in selenium_live_monitor")
        raise
