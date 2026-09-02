#!/usr/bin/env python3
"""
Selenium-based TikTok Live Supervisor

Modified version of Persistent TikTok Live Supervisor that uses selenium_live.db
for live detection instead of HTTP API.

Key Features:
- Uses selenium_live.db for live user detection
- Maintains all persistent supervisor functionality
- Dry-run mode for testing without starting recordings
- Detached recording processes that survive supervisor shutdown
- Health monitoring with automatic restart of failed recordings
- Graceful shutdown with --stop-all-lives option
- Zero-byte file detection and recovery
- Independent process lifecycle management
"""

import asyncio
import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import tempfile
import yaml
import requests
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

# Import UserContextLogger for username-prefixed logging
from utils.user_context_logger import UserContextLogger
from utils.config_paths import (
    absolute_config_path,
    normalize_config_paths,
    resolve_path,
)
from utils.username import normalize_tiktok_username
# Import SupervisorLock for preventing multiple --server instances
from utils.supervisor_lock import SupervisorLock
# Import DiskAlertHandler for bell signal on disk full errors
from utils.disk_alert_handler import wrap_handlers_with_disk_alert

# Import persistent live manager components
from persistent_live_manager import (
    ProcessMetadataStore,
    RecordingHealthMonitor,
    LiveProcessManager,
    GracefulShutdownHandler
)

# Import existing modules
from modules.db_setup import create_connection, initialize_db
from modules.db_users import get_all_usernames
from modules.db_user import add_user, set_user_active_status
from modules.supervisor_status import SupervisorStatusManager
# Removed HTTP-based imports - will use selenium_live.db instead
# from tiktok_supervisor.services.http_live_detection_service import HttpLiveDetectionService
# from tiktok_supervisor.utils.config_manager import ConfigManager
from recorder.utils.cookie_extractor import extract_and_save_cookies
from recorder.utils.custom_exceptions import IPBlockedByWAF
from recorder.utils.utils import read_cookies
from recorder.utils.security import redact_secrets
from recorder.core.tiktok_api import TikTokAPI


def get_web_monitor_local_base_url(config: Dict[str, Any]) -> str:
    """Return the same-host URL used for supervisor callbacks to the dashboard."""
    web_config = config.get("web_monitor", {})
    host = str(web_config.get("host", "0.0.0.0")).strip()
    port = int(web_config.get("port", 5001))

    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    return f"http://{host}:{port}"


def _atomic_write_private_json(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write a JSON state file with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


class ConsoleLogFilter(logging.Filter):
    """Filter to show only important messages on console"""

    def filter(self, record):
        # Show startup/shutdown messages from main
        if record.name == '__main__':
            return True

        # Show errors and warnings always
        if record.levelno >= logging.WARNING:
            return True

        # Show specific live detection events
        message = record.getMessage()
        if ('is now LIVE!' in message or
            'is no longer live' in message or
            'Recording started for' in message or
            'Recording completed for' in message or
            'Auto-cleanup:' in message or
            'Cleaned up dead process:' in message):
            return True

        # Hide everything else (including detailed monitoring logs)
        return False


def _route_logger_through_root(logger_name: str, log_level: int) -> None:
    """Send a named logger through the supervisor file/console handlers only."""
    named_logger = logging.getLogger(logger_name)
    named_logger.handlers.clear()
    named_logger.setLevel(log_level)
    named_logger.propagate = True


def get_db_connection(db_path: str, timeout: float = 30.0) -> sqlite3.Connection:
    """
    Create a database connection with proper timeout and WAL mode settings.

    Args:
        db_path: Path to the SQLite database
        timeout: Timeout in seconds for database locks (default: 30.0)

    Returns:
        sqlite3.Connection with optimal settings for concurrent access
    """
    resolved_db_path = resolve_path(db_path)
    conn = sqlite3.connect(
        resolved_db_path,
        timeout=timeout,
        check_same_thread=False,
        isolation_level=None  # Autocommit mode
    )
    # Ensure WAL mode is enabled for better concurrency
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class TelegramNotifier:
    """Telegram notifications with simple state-based deduplication."""

    DEFAULT_STATE_FILE = "/tmp/selenium_supervisor_telegram_state.json"

    def __init__(self, supervisor_config: Dict[str, Any], logger: logging.Logger):
        self.logger = logger
        self.enabled = self._as_bool(supervisor_config.get('enabled', False))
        self.bot_token = str(supervisor_config.get('bot_token', '')).strip()
        self.chat_id = str(supervisor_config.get('chat_id', '')).strip()
        self.startup_message_enabled = self._as_bool(
            supervisor_config.get('startup_message_enabled', True)
        )
        self.inactivity_alert_enabled = self._as_bool(
            supervisor_config.get('inactivity_alert_enabled', True)
        )
        self.recovery_message_enabled = self._as_bool(
            supervisor_config.get('recovery_message_enabled', True)
        )
        self.threshold_seconds = self._as_int(
            supervisor_config.get('inactivity_threshold_seconds', 600),
            default=600,
            min_value=60
        )
        self.active_window_enabled = self._as_bool(
            supervisor_config.get('active_window_enabled', True)
        )
        self.active_window_start_hour = self._as_int(
            supervisor_config.get('active_window_start_hour', 8),
            default=8,
            min_value=0,
            max_value=23
        )
        self.active_window_end_hour = self._as_int(
            supervisor_config.get('active_window_end_hour', 21),
            default=21,
            min_value=0,
            max_value=23
        )
        self.request_timeout_seconds = float(
            self._as_int(supervisor_config.get('request_timeout_seconds', 5), default=5, min_value=1)
        )
        self.disable_notification = self._as_bool(
            supervisor_config.get('disable_notification', True)
        )
        self.state_file = Path(
            str(supervisor_config.get('state_file', self.DEFAULT_STATE_FILE))
        )
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        self._state = self._load_state() if self.enabled else {
            "status": "unknown",
            "last_alert_reason": None,
            "timestamp": None
        }

        if self.enabled and not self._is_configured():
            self.logger.warning(
                "Telegram notifications enabled but bot_token/chat_id are missing - notifications disabled."
            )

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _as_int(value: Any, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default

        if min_value is not None:
            parsed = max(min_value, parsed)
        if max_value is not None:
            parsed = min(max_value, parsed)
        return parsed

    def _is_configured(self) -> bool:
        return self.enabled and bool(self.bot_token) and bool(self.chat_id)

    def _load_state(self) -> Dict[str, Any]:
        default_state = {
            "status": "unknown",
            "last_alert_reason": None,
            "timestamp": None
        }
        try:
            if self.state_file.exists():
                loaded = json.loads(self.state_file.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    return {
                        "status": loaded.get("status", default_state["status"]),
                        "last_alert_reason": loaded.get("last_alert_reason"),
                        "timestamp": loaded.get("timestamp")
                    }
        except Exception as e:
            self.logger.warning(f"Failed to load Telegram notifier state: {e}")
        return default_state

    def _save_state(self) -> None:
        try:
            _atomic_write_private_json(self.state_file, self._state)
        except Exception as e:
            self.logger.warning(f"Failed to save Telegram notifier state: {e}")

    def get_status(self) -> str:
        return str(self._state.get("status", "unknown"))

    def set_status(self, status: str, reason: Optional[str] = None) -> None:
        self._state["status"] = status
        self._state["last_alert_reason"] = reason
        self._state["timestamp"] = datetime.now().isoformat(timespec='seconds')
        self._save_state()

    def get_active_window_label(self) -> str:
        if not self.active_window_enabled:
            return "always"
        return f"{self.active_window_start_hour:02d}:00-{self.active_window_end_hour:02d}:59"

    def is_active_window(self, now: datetime) -> bool:
        if not self.active_window_enabled:
            return True

        hour = now.hour
        start = self.active_window_start_hour
        end = self.active_window_end_hour

        if start <= end:
            return start <= hour <= end
        return hour >= start or hour <= end

    def _send_message(self, text: str) -> None:
        if not self._is_configured():
            return

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_notification": self.disable_notification,
            "link_preview_options": {"is_disabled": True}
        }

        try:
            response = requests.post(
                f"{self.api_base}/sendMessage",
                json=payload,
                timeout=self.request_timeout_seconds
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data)
        except Exception as e:
            self.logger.error(
                "Failed to send Telegram notification: %s",
                redact_secrets(e, self.bot_token),
            )

    def send_startup_message(self, start_time: datetime, mode: str, config_path: str, db_path: str) -> None:
        if not (self._is_configured() and self.startup_message_enabled):
            return

        message = (
            "✅ Selenium Supervisor started\n"
            f"Time: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Mode: {mode}\n"
            f"Config: {config_path}\n"
            f"Selenium DB: {db_path}\n"
            f"Inactivity threshold: {self.threshold_seconds}s\n"
            f"Alert active window: {self.get_active_window_label()}\n"
            f"PID: {os.getpid()}"
        )
        self._send_message(message)

    def send_inactivity_alert(self, max_ended_at: datetime, age_seconds: int) -> None:
        if not (self._is_configured() and self.inactivity_alert_enabled):
            return

        message = (
            "⚠️ Selenium DB inactivity alert\n"
            f"Now: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"MAX(ended_at): {max_ended_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Age: {age_seconds}s\n"
            f"Threshold: {self.threshold_seconds}s\n"
            f"Active window: {self.get_active_window_label()}"
        )
        self._send_message(message)

    def send_recovery_message(self, max_ended_at: datetime, age_seconds: int) -> None:
        if not (self._is_configured() and self.recovery_message_enabled):
            return

        message = (
            "✅ Selenium DB activity recovered\n"
            f"Now: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"MAX(ended_at): {max_ended_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Age: {age_seconds}s\n"
            f"Threshold: {self.threshold_seconds}s"
        )
        self._send_message(message)

    @staticmethod
    def _mask_username_for_new_account(username: str) -> str:
        """Mask username for concise new-account Telegram alerts."""
        normalized = str(username).strip()
        length = len(normalized)

        if length < 4:
            return normalized
        if length < 6:
            return f"{normalized[:2]}*{normalized[-2:]}"
        return f"{normalized[:3]}*{normalized[-3:]}"

    def send_new_account_message(self, username: str) -> None:
        """Send concise Telegram alert for newly created account only."""
        if not self._is_configured():
            return

        if username is None:
            self.logger.debug("Skipping new-account Telegram alert: username is None")
            return

        normalized = str(username).strip()
        if not normalized:
            self.logger.debug("Skipping new-account Telegram alert: username is empty")
            return

        masked_username = self._mask_username_for_new_account(normalized)
        self._send_message(f"🆕 New account: {masked_username}")


class SeleniumSupervisor:
    """
    Selenium-based TikTok Live Supervisor

    Uses selenium_live.db for live detection while maintaining all persistent
    supervisor functionality for managing recording processes.
    """

    def __init__(self, config_path: str = "config.yaml", debug: bool = False, rotate_logs: bool = True, no_cookies: bool = False, dry_run: bool = False):
        self.config_path = absolute_config_path(config_path)
        self.debug = debug
        self.running = False
        self.rotate_logs = rotate_logs
        self.no_cookies = no_cookies
        self.dry_run = dry_run
        self.shutdown_event = asyncio.Event()
        self._shutdown_in_progress = False
        self._identity_request_lock = asyncio.Lock()
        self._last_identity_request_at = 0.0
        self.room_id_unavailable_alerted_users = set()

        # Load configuration
        self._load_configuration()

        # Setup logging
        self._setup_logging()
        self.logger = logging.getLogger('SUPV')
        self.supervisor_config = self.config.get('selenium', {}).get('supervisor', {})
        self.telegram_notifier = TelegramNotifier(
            self.config.get('telegram', {}).get('notifications', {}), self.logger
        )

        # Initialize components
        self._initialize_components()

        # Setup signal handlers
        self._setup_signal_handlers()

        # Initialize supervisor lock for --server mode
        lock_file_path = self.persistent_config.get('lock_file_path') or resolve_path(
            './supervisor.lock', base_dir=str(Path(self.config_path).parent)
        )
        self.supervisor_lock = SupervisorLock(lock_file_path)

        # Initialize supervisor status manager
        self.supervisor_status = SupervisorStatusManager(self.config)

        # Statistics
        self.stats = {
            'supervisor_starts': 0,
            'live_detections': 0,
            'recording_starts': 0,
            'recording_stops': 0,
            'health_checks': 0,
            'restarts': 0,
            'inactive_live_detections': 0,
            'new_users_added': 0,
            'users_deactivated': 0,
            'room_id_unavailable': 0
        }

        # In-memory lock to prevent duplicate recording starts
        # Stores usernames that are currently being started
        self.starting_recordings = set()
        self.starting_recordings_lock = asyncio.Lock()

        # Track unique inactive users detected as live (reset with stats)
        self.inactive_live_users = set()

        self.start_time = None

    def _log_configured_paths(self) -> None:
        """Log resolved application paths without exposing credentials."""
        try:
            paths_config = self.config.get('paths', {})
            if paths_config:
                self.logger.info("📂 Resolved paths from configuration:")
                for path_key, path_value in paths_config.items():
                    # Truncate long paths for cleaner logging
                    display_path = path_value if len(str(path_value)) <= 80 else f"{str(path_value)[:77]}..."
                    self.logger.info(f"   {path_key}: {display_path}")

        except Exception as e:
            self.logger.warning(f"Could not log configured paths: {e}")

    def _extract_cookies_if_needed(self) -> None:
        """Extract cookies from browser if enabled and configured"""
        try:
            cookies_config = self.config.get('cookies', {})

            if not cookies_config.get('enabled', False):
                self.logger.debug("Cookie extraction disabled in configuration")
                return

            # Get cookies source type (default to firefox for backward compatibility)
            cookies_source = cookies_config.get('cookies_source', 'firefox').lower()

            if cookies_source == 'json':
                # For JSON source, only validate that cookie_json_file is provided
                cookie_json_file = cookies_config.get('cookie_json_file')
                if not cookie_json_file:
                    self.logger.warning("Cookie JSON file path not specified for cookies_source='json'")
                    return

                # Check if the JSON file exists
                import os
                if not os.path.exists(cookie_json_file):
                    self.logger.warning(f"Cookie JSON file not found: {cookie_json_file}")
                    return

                self.logger.info(f"🍪 Using cookies from JSON file: {cookie_json_file}")
                # No extraction needed - cookies will be read directly from this file

            elif cookies_source == 'firefox':
                # Validate Firefox-specific configuration
                required_fields = ['firefox_cookie_db_path', 'target_cookie_json_path', 'cookies_to_extract']
                missing_fields = [field for field in required_fields if not cookies_config.get(field)]

                if missing_fields:
                    self.logger.warning(f"Cookie extraction configuration incomplete. Missing: {', '.join(missing_fields)}")
                    return

                self.logger.info("🍪 Extracting cookies from Firefox...")

                # Call the existing cookie extraction function
                success = extract_and_save_cookies(cookies_config)

                if success:
                    self.logger.info("✅ Cookies extracted successfully")
                else:
                    self.logger.warning("⚠️ Cookie extraction failed - will use existing cookies if available")

            else:
                self.logger.warning(f"Cookies source '{cookies_source}' not supported. Supported: 'firefox', 'json'")
                return

        except Exception as e:
            self.logger.error(f"Error during cookie extraction: {e}")
            self.logger.warning("⚠️ Continuing with existing cookies if available")

    def _load_configuration(self) -> None:
        """Load configuration from YAML file"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)

            self.config = normalize_config_paths(self.config or {}, self.config_path)
            config_dir = str(Path(self.config_path).parent)
            self.config.setdefault('database', {}).setdefault(
                'path', resolve_path('./db.sqlite', base_dir=config_dir)
            )
            self.config.setdefault('paths', {}).setdefault(
                'log_path', resolve_path('./logs', base_dir=config_dir)
            )

            # Ensure persistent_live_system config exists
            if 'persistent_live_system' not in self.config:
                self.config['persistent_live_system'] = {
                    'enabled': True,
                    'health_check_interval': 60,
                    'file_check_interval': 60,
                    'process_restart_delay': 10,
                    'max_restart_attempts': 3,
                    'detached_process_mode': True,
                    'metadata_storage': 'database'
                }

            self.persistent_config = self.config['persistent_live_system']
            self.persistent_config.setdefault(
                'lock_file_path',
                resolve_path('./supervisor.lock', base_dir=config_dir),
            )

        except Exception as e:
            print(f"Error loading configuration: {e}")
            sys.exit(1)

    def _setup_logging(self) -> None:
        """Setup logging configuration"""
        log_level_str = self.config.get('logging', {}).get('log_level', 'INFO')
        rotated_log_path = None

        # Determine log level
        if self.debug:
            log_level = logging.DEBUG
        else:
            log_level = getattr(logging, log_level_str.upper(), logging.INFO)

        # Clear existing handlers
        logging.getLogger().handlers.clear()

        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)-4s - %(levelname)-5s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        if self.rotate_logs:
            # Server mode - log to file and console
            log_path = self._get_supervisor_log_path()
            log_file = Path(log_path)

            # Validate that log directory exists and is writable
            log_dir = log_file.parent
            if not log_dir.exists():
                print(f"Error: Log directory does not exist: {log_dir}")
                print("Please create the directory and ensure it has write permissions.")
                sys.exit(1)

            # Check if directory is writable
            if not os.access(log_dir, os.W_OK):
                print(f"Error: No write permission to log directory: {log_dir}")
                print("Please ensure the directory has write permissions.")
                sys.exit(1)

            # Check if we can write to the specific log file location
            try:
                # Test write access by attempting to create/touch the file
                test_file = log_dir / f".write_test_{os.getpid()}"
                test_file.touch()
                test_file.unlink()  # Clean up test file
            except (PermissionError, OSError) as e:
                print(f"Error: Cannot write to log directory {log_dir}: {e}")
                print("Please check directory permissions.")
                sys.exit(1)

            # Rotate existing log file only at startup
            import shutil
            if os.path.exists(log_path):
                mtime = os.path.getmtime(log_path)
                timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(mtime))
                rotated = f"{os.path.splitext(log_path)[0]}-{timestamp}.log"
                shutil.move(log_path, rotated)
                rotated_log_path = rotated

            # File handler - write mode for fresh aplikacja.log after rotation
            file_handler = logging.FileHandler(log_path, mode='w', encoding='utf-8')
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)

            # Console handler (filtered)
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.WARNING)  # Only warnings and errors to console
            console_handler.setFormatter(formatter)

            # Add handlers to root logger
            root_logger = logging.getLogger()
            root_logger.setLevel(log_level)
            root_logger.addHandler(file_handler)
            root_logger.addHandler(console_handler)

            # Wrap all handlers with DiskAlertHandler for bell signal on disk full
            wrap_handlers_with_disk_alert(root_logger)

            # TikTokAPI imports the recorder logger, which otherwise keeps its own
            # INFO StreamHandler and bypasses the supervisor console threshold.
            _route_logger_through_root('logger', log_level)

            if rotated_log_path:
                logging.getLogger('SUPV').info(
                    f"Rotated existing log file to: {rotated_log_path}"
                )
        else:
            # CLI mode - log only to console, no file logging
            console_handler = logging.StreamHandler()
            console_handler.setLevel(log_level)
            console_handler.setFormatter(formatter)

            # Add only console handler to root logger
            root_logger = logging.getLogger()
            root_logger.setLevel(log_level)
            root_logger.addHandler(console_handler)

            # Wrap all handlers with DiskAlertHandler for bell signal on disk full
            wrap_handlers_with_disk_alert(root_logger)

        # Reduce noise from libraries
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('requests').setLevel(logging.WARNING)

        # ProcessMetadataStore may inherit a direct console handler from the
        # launcher. Keep its output on the supervisor's dated PMDS formatter.
        _route_logger_through_root('PMDS', log_level)

    def _print_logger_legend(self) -> None:
        """Log logger aliases without writing informational text to the console."""
        self.logger.debug(
            "Logger aliases: SUPV=Supervisor, LIVE=Live Detection, PROC=Process Manager, "
            "PMDS=Process Data Store, HLTH=Health Monitor, SHUT=Shutdown Handler, "
            "PINF=Process Info, STAT=Status Module, COOK=Cookie Extractor, "
            "CONF=Config Manager, LOCK=Supervisor Lock"
        )

    def _get_supervisor_log_path(self) -> str:
        """Get supervisor log file path from configuration"""
        import os
        log_dir_raw = self.config.get('paths', {}).get('log_path', './logs')

        log_dir = log_dir_raw

        # Get supervisor log filename
        log_filename = self.config.get('logging', {}).get('supervisor_log_file', 'supervisor.log')
        return os.path.join(log_dir, log_filename)

    def _initialize_components(self) -> None:
        """Initialize all persistent live manager components"""
        try:
            # Database setup
            config_dir = os.path.dirname(os.path.abspath(self.config_path))
            self.database_path = resolve_path(
                self.config.get('database', {}).get('path', './db.sqlite'),
                base_dir=config_dir
            )

            # Initialize database
            conn = create_connection(self.database_path)
            initialize_db(conn)
            conn.close()

            # Create core components
            self.metadata_store = ProcessMetadataStore(self.database_path)
            if not self.metadata_store.has_live_process_link_column():
                raise RuntimeError(
                    "Missing required live_processes.live_id column. Apply manually: "
                    "ALTER TABLE live_processes ADD COLUMN live_id INTEGER "
                    "REFERENCES lives(id);"
                )

            # Determine cookie file path based on cookies_source
            cookie_file_path = None
            cookies_config = self.config.get('cookies', {})
            if cookies_config.get('enabled', False):
                cookies_source = cookies_config.get('cookies_source', 'firefox').lower()
                if cookies_source == 'json':
                    cookie_file_path = cookies_config.get('cookie_json_file')
                elif cookies_source == 'firefox':
                    # For Firefox, use the target_cookie_json_path where cookies are extracted to
                    cookie_file_path = cookies_config.get('target_cookie_json_path')

            self.identity_cookie_file_path = cookie_file_path

            self.process_manager = LiveProcessManager(self.metadata_store, {
                **self.persistent_config,
                'recordings_path': self.config.get('paths', {}).get('recordings_path'),  # NO DEFAULT!
                'log_path': self._get_supervisor_log_path(),
                'config_path': self.config_path,
                'no_cookies': self.no_cookies,
                'cookie_file_path': cookie_file_path
            })
            self.process_manager.add_room_id_unavailable_callback(
                self._on_room_id_unavailable
            )
            self.process_manager.add_startup_callback(
                self._on_recorder_started
            )
            self.health_monitor = RecordingHealthMonitor(self.metadata_store, self.persistent_config)
            self.shutdown_handler = GracefulShutdownHandler(
                self.metadata_store,
                self.process_manager,
                self.persistent_config
            )

            # Setup health monitor lifecycle callbacks
            self.health_monitor.add_restart_callback(self._on_process_restart_needed)
            self.health_monitor.add_stop_callback(self._on_process_stop_needed)
            self.health_monitor.set_system_process_provider(
                self.process_manager.get_system_recorder_processes
            )
            self.health_monitor.add_untracked_stop_callback(
                self._on_untracked_process_stop_needed
            )
            self.health_monitor.add_replacement_callback(
                self._on_exact_process_replacement_needed
            )
            self.health_monitor.set_identity_check_callback(
                self._async_check_identity_for_health_monitor
            )

            # Selenium database path
            selenium_config = self.config.get('selenium', {})
            self.selenium_db_path = resolve_path(
                selenium_config.get('database_path') or selenium_config.get('db_path', './selenium_live.db'),
                base_dir=config_dir
            )

            # No need for HTTP detection service - we'll use selenium_live.db
            # Set live check callback for health monitor
            self.health_monitor.set_live_check_callback(self._async_check_live_for_health_monitor)

        except Exception as e:
            print(f"Error initializing components: {e}")
            sys.exit(1)

    def _get_selenium_live_users(self, raise_on_error: bool = False) -> List[str]:
        """
        Query selenium_live.db to get currently live users.
        Uses the documented query to find users with the most recent ended_at timestamp.
        """
        try:
            conn = get_db_connection(self.selenium_db_path)
            cursor = conn.cursor()

            # Get users who are currently live
            query = """
                SELECT user
                FROM selenium_live
                WHERE ended_at = (
                    SELECT MAX(ended_at)
                    FROM selenium_live
                )
            """

            cursor.execute(query)
            users = [row[0] for row in cursor.fetchall()]

            conn.close()

            if users:
                self.logger.debug(f"Found {len(users)} live users from Selenium: {', '.join(users)}")

            return users

        except sqlite3.Error as e:
            self.logger.error(f"Error querying selenium_live.db: {e}")
            if raise_on_error:
                raise
            return []

    def _parse_sqlite_datetime(self, value: Any) -> Optional[datetime]:
        """Parse SQLite datetime value into naive local datetime."""
        if value is None:
            return None

        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip()
            if not text:
                return None

            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                parsed = None

            if parsed is None:
                formats = [
                    '%Y-%m-%d %H:%M:%S.%f',
                    '%Y-%m-%d %H:%M:%S',
                    '%Y-%m-%dT%H:%M:%S.%f',
                    '%Y-%m-%dT%H:%M:%S',
                ]
                for fmt in formats:
                    try:
                        parsed = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue

            if parsed is None:
                self.logger.warning(f"Unable to parse SQLite datetime value: {value!r}")
                return None

        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)

        return parsed

    def _get_selenium_max_ended_at(self) -> Optional[datetime]:
        """Get MAX(ended_at) timestamp from selenium_live.db."""
        try:
            conn = get_db_connection(self.selenium_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(ended_at) FROM selenium_live")
            row = cursor.fetchone()
            conn.close()

            raw_value = row[0] if row else None
            return self._parse_sqlite_datetime(raw_value)
        except sqlite3.Error as e:
            self.logger.error(f"Error querying MAX(ended_at) from selenium_live.db: {e}")
            return None

    def _check_selenium_db_freshness_and_notify(self, now: datetime) -> None:
        """Alert via Telegram if selenium_live.db has no fresh MAX(ended_at) updates."""
        if not self.telegram_notifier.enabled or not self.telegram_notifier.inactivity_alert_enabled:
            return

        max_ended_at = self._get_selenium_max_ended_at()
        if max_ended_at is None:
            self.logger.debug("Selenium freshness check skipped: MAX(ended_at) is NULL")
            return

        if not self.telegram_notifier.is_active_window(now):
            return

        age_seconds = int(max(0, (now - max_ended_at).total_seconds()))
        threshold = self.telegram_notifier.threshold_seconds
        status = self.telegram_notifier.get_status()

        if age_seconds > threshold:
            if status != 'inactivity_alerted':
                self.logger.warning(
                    f"⚠️ Selenium DB inactivity detected: MAX(ended_at) age={age_seconds}s "
                    f"(threshold={threshold}s)"
                )
                self.telegram_notifier.send_inactivity_alert(max_ended_at, age_seconds)
                self.telegram_notifier.set_status(
                    'inactivity_alerted',
                    reason=f"age={age_seconds}s threshold={threshold}s max_ended_at={max_ended_at.isoformat()}"
                )
            return

        if status == 'inactivity_alerted':
            self.logger.info(
                f"✅ Selenium DB activity recovered: MAX(ended_at) age={age_seconds}s "
                f"(threshold={threshold}s)"
            )
            self.telegram_notifier.send_recovery_message(max_ended_at, age_seconds)
            self.telegram_notifier.set_status(
                'ok',
                reason=f"recovered age={age_seconds}s max_ended_at={max_ended_at.isoformat()}"
            )
        elif status == 'unknown':
            self.telegram_notifier.set_status(
                'ok',
                reason=f"initial_freshness age={age_seconds}s max_ended_at={max_ended_at.isoformat()}"
            )

    async def _check_and_start_recording_if_needed(self, username: str) -> None:
        """
        Check if user needs recording started based on Selenium live detection.
        This is the main method for Selenium-based live detection.
        """
        try:
            if normalize_tiktok_username(username, strip_at=False) != username:
                self.logger.warning("Ignoring invalid TikTok username from live detection")
                return
            # Create user context logger for this user
            user_logger = UserContextLogger(self.logger, username)

            # Check if recording is already being started for this user
            async with self.starting_recordings_lock:
                if username in self.starting_recordings:
                    user_logger.debug("Recording start already in progress, skipping duplicate attempt")
                    return
                # Mark as starting to prevent duplicates
                self.starting_recordings.add(username)

            try:
                # Check if user exists and is active in our database
                conn = create_connection(self.database_path)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT is_active, is_live
                    FROM users
                    WHERE username = ?
                """, (username,))
                row = cursor.fetchone()

                if not row:
                    # User not in database - add them automatically
                    user_logger.info("🆕 User not in database, adding automatically")

                    # Use default interval from config
                    default_interval = self.config.get('intervals', {}).get('default_interval', 300)

                    # Add user to database
                    result = add_user(conn, username, check_interval=default_interval)

                    if result.get('action') == 'added':
                        user_logger.info("✅ New user created in database")
                        self.telegram_notifier.send_new_account_message(username)
                    elif result['exists']:
                        user_logger.info(f"✅ User added successfully (was {'active' if result.get('was_active') else 'inactive'})")
                    else:
                        user_logger.info("✅ New user created in database")

                    # Re-query to get the user's status
                    cursor.execute("""
                        SELECT is_active, is_live
                        FROM users
                        WHERE username = ?
                    """, (username,))
                    row = cursor.fetchone()

                    if not row:
                        user_logger.error("Failed to add user to database")
                        conn.close()
                        return

                conn.close()

                is_active, is_live = row

                if is_active <= 0:  # Skip inactive (0) and deleted (-1) users
                    if is_active == 0:
                        user_logger.info("Inactive (is_active=0) user detected as LIVE by Selenium!")
                    else:  # is_active == -1
                        user_logger.debug("Deleted user (is_active=-1) detected as LIVE by Selenium - ignoring")
                    self.inactive_live_users.add(username)
                    return

                # Check if user already has an active recording
                existing_process = self.metadata_store.get_process_by_username(username)

                if not is_live:
                    # User is active but not marked as live - Selenium detected them as live
                    self.stats['live_detections'] += 1
                    self.supervisor_status.increment_stat('live_detections')
                    user_logger.info("🔴 Selenium detected user as LIVE!")

                    # Notify immediately on Selenium detection, before recorder/RoomID resolution.
                    self._send_live_notification(username, 'live_start', f'🔴 {username} is now LIVE!')

                    # Update database is_live status to 2 (starting)
                    await self._update_user_live_status(username, 2)

                    if not existing_process:
                        # Start recording if not in dry-run mode
                        if self.dry_run:
                            user_logger.info("🔍 DRY-RUN: Would start recording")
                        else:
                            user_logger.info("🎬 Starting recording")

                            # Start recording (no room_id from Selenium)
                            result = await self.process_manager.start_live_recording(username, None)

                            if result.success:
                                if result.started_new:
                                    self.stats['recording_starts'] += 1
                                    self.supervisor_status.increment_stat('recording_starts')
                                    self.logger.info(f"📹 Started recording for {username} (PID: {result.pid})")
                                else:
                                    user_logger.info(f"Recording already running (PID: {result.pid})")
                                # Update to is_live=1 after successful start
                                await self._update_user_live_status(username, 1)
                            else:
                                error_text = str(result.error or "")
                                # Check error type and handle appropriately
                                if "WAF" in error_text or "blocked" in error_text.lower():
                                    waf_pause = self.config.get('intervals', {}).get('waf_block_pause_duration', 60)
                                    user_logger.error(f"🚫 WAF block detected! Pausing for {waf_pause}s: {result.error}")
                                    self.logger.error(f"WAF block detected during recording start. Pausing system for {waf_pause}s")
                                    await asyncio.sleep(waf_pause)
                                    # Mark this as a WAF block in stats
                                    self.stats['waf_blocks'] = self.stats.get('waf_blocks', 0) + 1
                                    raise IPBlockedByWAF(str(result.error))
                                elif "RACE_CONDITION" in error_text:
                                    user_logger.debug("⏱️ Race condition: User went offline between detection and recording start")
                                    self.stats['race_conditions'] = self.stats.get('race_conditions', 0) + 1
                                    # Don't log as error, this is normal behavior
                                elif "NO_STREAM_DATA" in error_text:
                                    user_logger.debug(error_text)
                                elif "ROOM_ID_UNAVAILABLE" in error_text:
                                    user_logger.info("RoomID unavailable; will retry on next live detection")
                                    self.stats['room_id_unavailable'] = self.stats.get('room_id_unavailable', 0) + 1
                                else:
                                    user_logger.error(f"❌ Failed to start recording: {result.error}")
                                # Reset to is_live=0 on failure
                                await self._update_user_live_status(username, 0)
                    else:
                        user_logger.debug("Already has active recording")
                else:
                    # User already marked as live
                    if existing_process:
                        user_logger.debug("User marked as live and has active recording")
                    else:
                        # User marked as live but no active recording - attempt recovery
                        user_logger.info("User marked as live but no active recording - attempting recovery")

                        if not self.dry_run:
                            # Try to start recording again
                            result = await self.process_manager.start_live_recording(username, None)

                            if result.success:
                                if result.started_new:
                                    self.stats['recording_starts'] += 1
                                    self.supervisor_status.increment_stat('recording_starts')
                                    user_logger.info(f"✅ Recovery successful - started recording (PID: {result.pid})")
                                else:
                                    user_logger.info(f"Recovery found an existing recording (PID: {result.pid})")
                            else:
                                error_text = str(result.error or "")
                                # Check error type and handle appropriately
                                if "WAF" in error_text or "blocked" in error_text.lower():
                                    waf_pause = self.config.get('intervals', {}).get('waf_block_pause_duration', 60)
                                    user_logger.error(f"🚫 WAF block detected during recovery! Pausing for {waf_pause}s: {result.error}")
                                    self.logger.error(f"WAF block detected during recovery. Pausing system for {waf_pause}s")
                                    await asyncio.sleep(waf_pause)
                                    # Mark this as a WAF block in stats
                                    self.stats['waf_blocks'] = self.stats.get('waf_blocks', 0) + 1
                                    raise IPBlockedByWAF(str(result.error))
                                elif "RACE_CONDITION" in error_text:
                                    user_logger.debug("⏱️ Recovery race condition: User went offline during recovery attempt")
                                    self.stats['race_conditions'] = self.stats.get('race_conditions', 0) + 1
                                    # Don't log as error, this is normal behavior
                                elif "NO_STREAM_DATA" in error_text:
                                    user_logger.debug(error_text)
                                elif "ROOM_ID_UNAVAILABLE" in error_text:
                                    user_logger.info("Recovery skipped because RoomID is unavailable; will retry on next live detection")
                                    self.stats['room_id_unavailable'] = self.stats.get('room_id_unavailable', 0) + 1
                                else:
                                    # If recovery fails, reset is_live status
                                    user_logger.error(f"❌ Recovery failed: {result.error}")
                                await self._update_user_live_status(username, 0)
                                user_logger.info("Reset is_live status to 0")

            finally:
                # Always remove from starting set when done
                async with self.starting_recordings_lock:
                    self.starting_recordings.discard(username)

        except IPBlockedByWAF:
            # WAF block already handled above with pause, just re-raise to stop processing more users
            if 'user_logger' in locals():
                user_logger.warning("WAF block - stopping user processing cycle")
            else:
                self.logger.warning(f"WAF block for {username} - stopping user processing cycle")
            # Remove from starting set before re-raising
            async with self.starting_recordings_lock:
                self.starting_recordings.discard(username)
            raise  # Re-raise to stop processing more users in this cycle
        except Exception as e:
            if 'user_logger' in locals():
                user_logger.error(f"Error checking user: {e}")
            else:
                self.logger.error(f"Error checking user {username}: {e}")
            # Remove from starting set on error
            async with self.starting_recordings_lock:
                self.starting_recordings.discard(username)

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown and zombie cleanup"""
        def signal_handler(signum, frame):
            # Prevent multiple shutdown attempts
            if self._shutdown_in_progress:
                self.logger.info("Shutdown already in progress, please wait...")
                return

            self._shutdown_in_progress = True
            self.logger.info(f"Received signal {signum}, initiating graceful shutdown...")
            self.running = False

            # Set shutdown event to wake up any waiting coroutines
            if hasattr(self, 'shutdown_event'):
                self.shutdown_event.set()

            # Cancel the main event loop to force immediate shutdown
            try:
                # Get the current event loop
                loop = asyncio.get_event_loop()
                # Get all running tasks (compatible with Python 3.7+)
                if hasattr(asyncio, 'all_tasks'):
                    all_tasks = asyncio.all_tasks(loop)
                else:
                    all_tasks = asyncio.Task.all_tasks(loop)

                # Cancel all tasks except the current one
                for task in all_tasks:
                    if not task.done():
                        task.cancel()
            except Exception as e:
                self.logger.error(f"Error cancelling tasks: {e}")

            # Remove supervisor lock file on signal
            if hasattr(self, 'supervisor_lock'):
                self.supervisor_lock.cleanup_on_signal(signum)

        def sigchld_handler(signum, frame):
            """Handle SIGCHLD to reap zombie processes"""
            # A signal can interrupt Python's logging machinery while it is
            # flushing a buffered stream.  Logging from here would then enter
            # the same stream recursively and produce a ``Logging error``
            # traceback.  Keep this handler limited to non-blocking waitpid
            # calls and do not perform logging or any other buffered I/O.
            try:
                # Reap all available zombie children without blocking
                while True:
                    try:
                        pid, _status = os.waitpid(-1, os.WNOHANG)
                        if pid == 0:  # No more children to reap
                            break
                    except ChildProcessError:
                        # No more child processes
                        break
            except OSError:
                # Signal handlers must not invoke logging: this handler may
                # have interrupted a logging call in the main thread.
                pass

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGCHLD, sigchld_handler)

    async def _on_process_restart_needed(self, process_info, issues: List[str]) -> None:
        """Handle process restart request from health monitor"""
        try:
            self.logger.info(f"Health monitor requested restart for {process_info.username}: {', '.join(issues)}")

            # Check if user is still live before restarting
            is_live = await self._async_check_live_for_health_monitor(process_info.username)

            if is_live is not False:
                # User is still live, proceed with restart
                success = await self.process_manager.restart_live_recording(process_info, issues)

                if success:
                    self.logger.info(f"Successfully restarted process for {process_info.username}")
                else:
                    # Don't log as error if it's just a race condition
                    self.logger.debug(f"Failed to restart process for {process_info.username} (likely went offline)")
            else:
                # User is no longer live, stop the process instead of restarting
                self.logger.info(f"⚫ {process_info.username} is no longer live, stopping recording instead of restarting...")
                success = await self.process_manager.stop_live_recording(process_info.username)
                if success:
                    self.logger.info(f"⏹️ Stopped recording for {process_info.username}")
                else:
                    self.logger.error(f"❌ Failed to stop recording for {process_info.username}")

        except Exception as e:
            self.logger.error(f"Error handling process restart for {process_info.username}: {e}")

    async def _on_process_stop_needed(
        self,
        process_info,
        final_status: str,
        reset_live_status: bool,
    ) -> bool:
        """Stop the exact process requested by the health monitor."""
        return await self.process_manager.stop_process(
            process_info,
            graceful=True,
            final_status=final_status,
            reset_live_status=reset_live_status,
        )

    async def _on_untracked_process_stop_needed(self, process) -> bool:
        """Stop one exact system recorder missing from active metadata."""
        return await self.process_manager.stop_untracked_process(
            process,
            graceful=True,
        )

    async def _on_exact_process_replacement_needed(
        self,
        process_info,
        reason: str,
        final_status: str,
        start_new: bool,
    ) -> bool:
        """Replace only the recorder selected by the health monitor."""
        return await self.process_manager.replace_exact_process(
            process_info,
            reason,
            final_status,
            start_new,
        )

    def _on_room_id_unavailable(self, username: str, error: str) -> None:
        """Notify the web UI once when a live user cannot start a recorder."""
        if username in self.room_id_unavailable_alerted_users:
            return

        if self._send_room_id_unavailable_notification(username):
            self.room_id_unavailable_alerted_users.add(username)

    async def _on_recorder_started(
        self,
        username: str,
        pid: int,
        process_id: int,
        recording_file_path: str,
    ) -> None:
        """Allow a future RoomID failure to notify after a successful start."""
        self.room_id_unavailable_alerted_users.discard(username)

    def _synchronize_component_statistics(self) -> None:
        """Expose authoritative process-manager counters in supervisor status."""
        process_stats = self.process_manager.get_statistics()
        health_stats = self.health_monitor.get_statistics()
        self.stats['recording_stops'] = process_stats.get('processes_stopped', 0)
        self.stats['health_checks'] = health_stats.get('health_checks_performed', 0)
        self.stats['restarts'] = process_stats.get('processes_restarted', 0)

    def _send_room_id_unavailable_notification(self, username: str) -> bool:
        """Send a system alert that does not depend on per-user preferences."""
        try:
            response = requests.post(
                f'{get_web_monitor_local_base_url(self.config)}/api/events/room-id-unavailable',
                json={'username': username},
                timeout=2,
            )
            if response.status_code != 200:
                self.logger.debug(
                    f"RoomID web alert failed for {username}: HTTP {response.status_code}"
                )
                return False

            active_clients = response.json().get('active_clients', 0)
            if active_clients < 1:
                self.logger.debug(
                    f"RoomID web alert has no connected clients for {username}"
                )
                return False

            self.logger.debug(f"RoomID web alert sent for {username}")
            return True
        except (requests.exceptions.RequestException, ValueError) as error:
            self.logger.debug(f"Web app not available for RoomID alert: {error}")
            return False

    def _send_live_notification(self, username: str, event_type: str, message: str = None):
        """Send live notification to web interface via SSE"""
        try:
            # Check if user has notifications enabled
            if not self._should_send_notification(username):
                return

            # Send notification to web app
            notification_data = {
                'username': username,
                'type': event_type,
                'message': message or f'{username} {event_type}'
            }

            # Try to send to local web app (non-blocking)
            try:
                response = requests.post(
                    f'{get_web_monitor_local_base_url(self.config)}/api/test-notification',
                    json=notification_data,
                    timeout=2
                )
                if response.status_code == 200:
                    self.logger.debug(f"Live notification sent: {event_type} for {username}")
                else:
                    self.logger.warning(f"Failed to send notification: HTTP {response.status_code}")
            except requests.exceptions.RequestException as e:
                self.logger.debug(f"Web app not available for notifications: {e}")

        except Exception as e:
            self.logger.error(f"Error sending live notification: {e}")

    def _should_send_notification(self, username: str) -> bool:
        """Check if user has notifications enabled"""
        try:
            db_path = self.config.get('database', {}).get('path', './db.sqlite')
            conn = get_db_connection(db_path)
            cursor = conn.cursor()

            cursor.execute("SELECT notifications_enabled FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            conn.close()

            return bool(row[0]) if row else False

        except Exception as e:
            self.logger.error(f"Error checking notification settings for {username}: {e}")
            return False

    def check_server_lock(self) -> bool:
        """
        Check if another supervisor instance is running in --server mode

        Returns:
            True if lock exists and is valid, False otherwise
        """
        lock_exists, lock_data = self.supervisor_lock.check_server_lock()
        if lock_exists and lock_data:
            self.logger.error(self.supervisor_lock.get_lock_info_message(lock_data))
            return True
        return False

    def create_server_lock(self) -> bool:
        """
        Create lock file for current supervisor instance

        Returns:
            True if successful, False otherwise
        """
        success = self.supervisor_lock.create_server_lock()
        if success:
            # Update lock file with actual config path
            self.supervisor_lock.update_config_path(self.config_path)
        return success

    def remove_server_lock(self) -> None:
        """Remove supervisor lock file"""
        self.supervisor_lock.remove_server_lock()

    async def start_supervisor(self) -> None:
        """Start the persistent supervisor"""
        if not self.persistent_config.get('enabled', True):
            self.logger.warning("Persistent live system is disabled in configuration")
            return

        self.running = True
        self.start_time = datetime.now()
        self.stats['supervisor_starts'] += 1

        # Record supervisor startup
        self.supervisor_status.start_supervisor()

        # Print logger legend for better readability
        self._print_logger_legend()

        self.logger.info("🚀 Starting Selenium-based TikTok Live Supervisor")
        self.logger.info(f"Configuration: {self.config_path}")
        self.logger.info(f"Mode: {'DRY-RUN (no recordings will start)' if self.dry_run else 'NORMAL'}")
        self.logger.info(f"Selenium DB: {self.selenium_db_path}")
        self.logger.info(f"Detached mode: {self.persistent_config.get('detached_process_mode', True)}")
        self.logger.info(f"Health check interval: {self.persistent_config.get('health_check_interval', 60)}s")

        self._log_configured_paths()

        try:
            # Extract cookies from browser before starting operations
            self._extract_cookies_if_needed()

            # Cleanup orphaned processes first
            await self._cleanup_orphaned_processes()

            # Cleanup dead database records
            dead_cleanup_count = self.metadata_store.cleanup_dead_database_entries()
            if dead_cleanup_count > 0:
                self.logger.info(f"✅ Startup cleanup: Removed {dead_cleanup_count} dead database records")

            # Start components
            # No need to start HTTP detection service - using selenium_live.db
            await self.health_monitor.start_monitoring()

            self.logger.info("✅ Persistent supervisor started successfully")
            self.telegram_notifier.send_startup_message(
                start_time=self.start_time or datetime.now(),
                mode='DRY-RUN' if self.dry_run else 'NORMAL',
                config_path=self.config_path,
                db_path=self.selenium_db_path
            )
            self.logger.info("Press Ctrl+C to stop...")

            # Main supervisor loop
            await self._supervisor_loop()

        except Exception as e:
            self.logger.error(f"Error in supervisor: {e}")
            raise
        finally:
            await self._shutdown_supervisor()

    async def _cleanup_orphaned_processes(self) -> None:
        """Clean up orphaned processes on startup"""
        try:
            # Get currently running PIDs
            import psutil
            running_pids = [p.pid for p in psutil.process_iter()]

            for process_info in self.metadata_store.get_active_processes():
                if process_info.pid not in running_pids:
                    self.process_manager.cleanup_zero_byte_output(process_info)

            # Clean up orphaned database records
            cleaned = self.metadata_store.cleanup_orphaned_processes(running_pids)
            if cleaned > 0:
                self.logger.info(f"Cleaned up {cleaned} orphaned process records on startup")

        except Exception as e:
            self.logger.error(f"Error cleaning up orphaned processes: {e}")

    async def _supervisor_loop(self) -> None:
        """Main supervisor monitoring loop - Selenium version"""
        self.logger.debug("Starting Selenium supervisor monitoring loop")

        # Configuration
        selenium_check_interval = self.config.get('selenium', {}).get('refresh_interval', 60) // 2  # Use half of configured interval
        check_interval = 30  # Check for new users every 30 seconds
        min_user_interval = self.config.get('intervals', {}).get('min_user_interval', 1)

        last_user_discovery = datetime.now() - timedelta(seconds=check_interval)
        stats_interval = self.config.get('selenium', {}).get('supervisor', {}).get('stats_interval', 300)
        last_stats_log = datetime.now() - timedelta(seconds=stats_interval)  # Show stats on first iteration
        last_selenium_check = datetime.now() - timedelta(seconds=selenium_check_interval)  # Force immediate check
        last_heartbeat = datetime.now() - timedelta(seconds=30)  # Force immediate heartbeat
        heartbeat_interval = self.config.get('intervals', {}).get('supervisor_heartbeat_interval', 30)

        try:
            while self.running:
                loop_start = time.time()

                # Discover new users periodically
                now = datetime.now()
                if (now - last_user_discovery).total_seconds() >= check_interval:
                    await self._discover_new_users()
                    last_user_discovery = now

                # Check selenium_live.db for live users periodically
                if (now - last_selenium_check).total_seconds() >= selenium_check_interval:
                    # Get live users from Selenium database
                    selenium_live_users = self._get_selenium_live_users()
                    self._check_selenium_db_freshness_and_notify(now)

                    if selenium_live_users:
                        self.logger.info(f"📡 Selenium detected {len(selenium_live_users)} live users")

                        # Check each live user detected by Selenium
                        for username in selenium_live_users:
                            try:
                                await self._check_and_start_recording_if_needed(username)
                            except IPBlockedByWAF:
                                # WAF block occurred - stop processing more users this cycle
                                self.logger.warning("WAF block detected - skipping remaining users in this cycle")
                                break

                    last_selenium_check = now

                # Log statistics periodically
                if (now - last_stats_log).total_seconds() >= stats_interval:
                    await self._log_statistics()
                    last_stats_log = now

                # No need to refresh service - we're using selenium_live.db

                # Update supervisor heartbeat
                if (now - last_heartbeat).total_seconds() >= heartbeat_interval:
                    self._synchronize_component_statistics()
                    self.supervisor_status.update_heartbeat(self.stats)
                    last_heartbeat = now

                # Sleep before next iteration (interruptible by shutdown event)
                elapsed = time.time() - loop_start
                sleep_time = max(1, 10 - elapsed)  # At least 1 second, target 10 second cycles
                try:
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=sleep_time)
                    # If we get here, shutdown was requested
                    break
                except asyncio.TimeoutError:
                    # Normal timeout, continue loop
                    pass

        except asyncio.CancelledError:
            self.logger.debug("Supervisor loop cancelled")
            raise
        except Exception as e:
            self.logger.error(f"Error in supervisor loop: {e}")
            raise

    async def _discover_new_users(self) -> None:
        """Discover new users from recordings directory and sync with database"""
        try:
            # Get database connection
            conn = create_connection(self.database_path)

            # Get all users from database (only active ones)
            db_users = set(get_all_usernames(conn))

            # Get users from recordings directory
            recordings_path_str = self.config.get('paths', {}).get('recordings_path')
            if not recordings_path_str:
                self.logger.error("❌ CRITICAL: No recordings_path in configuration!")
                raise ValueError("recordings_path not configured")
            recordings_path = Path(recordings_path_str)
            dir_users = set()
            if recordings_path.exists():
                dir_users = {
                    d.name
                    for d in recordings_path.iterdir()
                    if d.is_dir()
                    and not d.is_symlink()
                    and normalize_tiktok_username(d.name, strip_at=False) == d.name
                }

            # Add new users (in directory but not in DB)
            new_users = dir_users - db_users
            users_to_check_immediately = []

            for username in new_users:
                default_interval = self.config.get('intervals', {}).get('default_check_interval', 300)
                result = add_user(conn, username, check_interval=default_interval)
                if result.get('action') == 'added':
                    self.logger.info(f"📁 Added new user from directory: {username}")
                    self.telegram_notifier.send_new_account_message(username)
                    self.stats['new_users_added'] += 1
                    users_to_check_immediately.append(username)
                else:
                    # User exists - check if was forced_check or reactivated
                    if result.get('action') in ['reactivated', 'forced_check']:
                        self.logger.info(f"📁 User {username} found in directory - will check immediately")
                        users_to_check_immediately.append(username)

            # Update active status based on directory presence
            # First, activate users with directories
            for username in dir_users:
                if username in db_users:
                    # Check current status to avoid unnecessary updates
                    cursor = conn.cursor()
                    cursor.execute("SELECT is_active FROM users WHERE username = ?", (username,))
                    row = cursor.fetchone()
                    if row and row[0] == 0:  # User exists but is inactive (not deleted, is_active=-1)
                        set_user_active_status(
                            conn, username, True, path_config=self.config
                        )
                        self.logger.info(f"✅ Activated user {username} (directory found)")

            # No need to add users to HTTP detection service - using Selenium

            # Get count of active users for logging
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
            active_count = cursor.fetchone()[0]

            conn.close()
            self.logger.debug(f"Directory sync complete: {active_count} active users, {len(new_users)} new")

            # Immediately check any new or reactivated users
            if users_to_check_immediately:
                self.logger.info(f"🔍 Immediately checking {len(users_to_check_immediately)} new/reactivated users")

                # Get current live users from Selenium to avoid double-checking
                selenium_live_users = self._get_selenium_live_users()

                for username in users_to_check_immediately:
                    # Skip if already detected as live by Selenium
                    if username in selenium_live_users:
                        self.logger.debug(f"Skipping {username} - already detected by Selenium")
                        continue

                    try:
                        await self._check_and_start_recording_if_needed(username)
                    except IPBlockedByWAF:
                        # WAF block occurred - stop processing more new users
                        self.logger.warning("WAF block detected while checking new users - stopping discovery")
                        break
                    except Exception as e:
                        self.logger.error(f"Error checking new user {username}: {e}")

        except Exception as e:
            self.logger.error(f"Error discovering users: {e}")

    def _get_user_check_interval(self, username: str) -> int:
        """Get user's check_interval from database, with fallback to default"""
        try:
            db_path = self.config.get('database', {}).get('path', './db.sqlite')
            conn = create_connection(db_path)
            cursor = conn.cursor()

            cursor.execute("SELECT check_interval FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            conn.close()

            if row and row[0]:
                return int(row[0])
            else:
                # Fallback to config default
                return self.config.get('tiktok', {}).get('default_check_interval', 300)

        except Exception as e:
            self.logger.warning(f"Could not get check_interval for {username}: {e}, using default")
            return self.config.get('tiktok', {}).get('default_check_interval', 300)

    # Method _get_users_ready_for_check removed - not needed with Selenium approach
    """
    async def _get_users_ready_for_check(self) -> List[str]:
        # Get list of users ready for live status check
        try:
            # Get users from live detection service that are ready for check
            ready_users = []

            # Check if active_connections exists and has items() method
            if hasattr(self.live_detection_service.active_connections, 'items'):
                for username, session in self.live_detection_service.active_connections.items():
                    if session.is_ready_for_check():
                        ready_users.append(username)
            else:
                # If active_connections is empty or not properly initialized, get some users from DB
                db_path = self.config.get('database', {}).get('path', './db.sqlite')
                conn = create_connection(db_path)

                # Get users from database that are actually ready for check (next_check <= now)
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT username FROM users
                    WHERE is_active = 1
                    AND is_live = 0
                    AND (next_check IS NULL OR DATETIME(next_check) <= DATETIME('now', 'localtime'))
                    ORDER BY COALESCE(next_check, '1970-01-01') ASC
                    LIMIT 5
                ''')
                rows = cursor.fetchall()
                ready_users = [row[0] for row in rows]
                conn.close()

                if ready_users:
                    self.logger.debug(f"Found {len(ready_users)} users ready for check from DB")

            return ready_users[:5]  # Limit to 5 users per cycle

        except Exception as e:
            self.logger.error(f"Error getting users ready for check: {e}")
            return []
    """

    # Method _check_user_live_status removed - replaced by _check_and_start_recording_if_needed
    # Original method was used for HTTP-based live detection
    """
    async def _check_user_live_status(self, username: str) -> None:
        # Check if a user is live and start recording if needed
        try:
            # Create user context logger for this user
            user_logger = UserContextLogger(self.logger, username)

            # DEBUG: Check if user is added to live detection service
            session = self.live_detection_service.get_user_session(username)
            if not session:
                user_logger.info("Adding to live_detection_service")
                user_check_interval = self._get_user_check_interval(username)
                self.live_detection_service.add_user(username, check_interval=user_check_interval)

            # Get current live status from database before checking
            current_live_status = self._get_user_live_status(username)

            # Check live status
            live_status = await self.live_detection_service.check_user_live_status(username)
            self.stats['health_checks'] += 1

            # Determine if user went from live to offline (for after_live_check_interval)
            went_offline = current_live_status and live_status.value == 'offline'

            # Update next_check for this user IMMEDIATELY after checking
            await self._update_user_next_check(username, just_went_offline=went_offline)

            # Check if user already has an active recording
            existing_process = self.metadata_store.get_process_by_username(username)

            if live_status.value == 'live':
                self.stats['live_detections'] += 1
                self.supervisor_status.increment_stat('live_detections')
                user_logger.info("Status change: offline -> live")

                # Update database is_live status BEFORE starting recording
                await self._update_user_live_status(username, 1)

                if not existing_process:
                    # User is live but no recording - start one
                    user_logger.info("🔴 Starting recording - user is live")

                    # Send live notification
                    self._send_live_notification(username, 'live_start', f'🔴 {username} is now LIVE!')

                    # Get room_id from live detection service
                    session = self.live_detection_service.get_user_session(username)
                    room_id = session.room_id if session else None

                    result = await self.process_manager.start_live_recording(username, room_id)

                    if result.success:
                        if result.started_new:
                            self.stats['recording_starts'] += 1
                            self.supervisor_status.increment_stat('recording_starts')
                            self.logger.info(f"📹 Started recording for {username} (PID: {result.pid})")
                        else:
                            user_logger.info(f"Recording already running (PID: {result.pid})")
                    else:
                        error_text = str(result.error or "")
                        # Check if this is a race condition or a real error
                        if "race condition" in error_text.lower():
                            self.logger.info(f"⚡ {username}: {result.error}")
                            self.stats['race_conditions'] = self.stats.get('race_conditions', 0) + 1
                        elif "NO_STREAM_DATA_COOLDOWN" in error_text:
                            user_logger.debug(error_text)
                        elif "ROOM_ID_UNAVAILABLE" in error_text:
                            user_logger.info("RoomID unavailable; will retry on next live detection")
                            self.stats['room_id_unavailable'] = self.stats.get('room_id_unavailable', 0) + 1
                        else:
                            user_logger.error(f"❌ Failed to start recording: {result.error}")
                            self.stats['startup_failures'] = self.stats.get('startup_failures', 0) + 1
                else:
                    user_logger.debug("User is live and already has an active recording")

            else:
                # User is not live
                user_logger.info("Status change: unknown -> offline")

                # Update database is_live status to offline
                await self._update_user_live_status(username, 0)

                if existing_process and existing_process.health_status != 'stopped':
                    # User is not live but has active recording - stop it
                    user_logger.info("⚫ User is no longer live, stopping recording...")

                    # Send live end notification
                    self._send_live_notification(username, 'live_end', f'⚫ {username} has ended their live stream')

                    success = await self.process_manager.stop_live_recording(username)
                    if success:
                        user_logger.info("⏹️ Stopped recording")
                    else:
                        user_logger.error("❌ Failed to stop recording")

        except Exception as e:
            user_logger.error(f"Error checking live status: {e}") if 'user_logger' in locals() else self.logger.error(f"Error checking live status for {username}: {e}")
    """

    async def deactivate_user(self, username: str) -> None:
        """Deactivate a user via CLI: stop recording if live, move directory"""
        print(f"🔄 Deactivating user: {username}")
        print()

        try:
            # First, check if user exists in database
            conn = create_connection(self.database_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id, is_live, is_active FROM users WHERE username = ?", (username,))
            user = cursor.fetchone()

            if not user:
                print(f"❌ User {username} not found in database")
                return

            user_id, is_live, is_active = user

            if not is_active:
                print(f"ℹ️ User {username} is already inactive")

            # Check if user has active recording
            process = self.metadata_store.get_process_by_username(username)

            if process:
                print(f"📹 User has active recording (PID: {process.pid})")

                # Set next_check to future to prevent immediate re-check
                print("📅 Setting next_check to +1 day to prevent immediate re-check...")
                cursor.execute("""
                    UPDATE users
                    SET next_check = DATETIME('now', 'localtime', '+1 day')
                    WHERE username = ?
                """, (username,))
                conn.commit()

                # Stop the recording
                print("⏹️ Stopping recording...")
                success = await self.process_manager.stop_live_recording(username, graceful=True)

                if success:
                    print("✅ Recording stopped successfully")
                else:
                    print("⚠️ Recording may not have stopped cleanly")

                # Wait a moment for process to fully stop
                await asyncio.sleep(2)
            else:
                print("ℹ️ No active recording found")

            # Now deactivate the user (this will also move directory)
            print("🚚 Deactivating user and moving directory...")
            set_user_active_status(
                conn, username, False, path_config=self.config
            )

            conn.close()

            print(f"\n✅ User {username} has been deactivated successfully")
            print("   - Status set to inactive")
            print("   - Directory moved to inactive_users (if existed)")
            print("   - Recording stopped (if was active)")

        except Exception as e:
            print(f"❌ Error deactivating user: {e}")
            import traceback
            traceback.print_exc()

    async def mark_deleted_users(self) -> None:
        """Find users with tt_user_id or tt_secuid = -1 and mark them as deleted"""
        print("🔍 Finding users with invalid TikTok IDs (-1)...")
        print()

        try:
            conn = create_connection(self.database_path)
            cursor = conn.cursor()

            # Find users with tt_user_id = -1 or tt_secuid = -1 that are not already deleted
            cursor.execute("""
                SELECT id, username, is_active, tt_user_id, tt_secuid
                FROM users
                WHERE (tt_user_id = '-1' OR tt_secuid = '-1')
                AND is_active IN (0, 1)
                ORDER BY username
            """)

            users_to_mark = cursor.fetchall()

            if not users_to_mark:
                print("✅ No users found with invalid TikTok IDs")
                return

            print(f"Found {len(users_to_mark)} users with invalid TikTok IDs:")
            print()
            print(f"{'Username':<30} {'Status':<15} {'tt_user_id':<20} {'tt_secuid':<20}")
            print("-" * 85)

            for user in users_to_mark:
                user_id, username, is_active, tt_user_id, tt_secuid = user
                status = "Active" if is_active == 1 else "Inactive"
                print(f"{username:<30} {status:<15} {tt_user_id:<20} {tt_secuid:<20}")

            print()
            response = input("Do you want to mark these users as deleted? (yes/no): ").strip().lower()

            if response != 'yes':
                print("❌ Operation cancelled")
                return

            # Mark users as deleted
            print()
            print("Marking users as deleted...")

            for user in users_to_mark:
                user_id, username, is_active, tt_user_id, tt_secuid = user

                # Check if user has any active recordings
                process_info = self.metadata_store.get_process_by_username(username)
                if process_info and process_info.is_active:
                    print(f"⏹️ Stopping active recording for {username}...")
                    await self.process_manager.stop_recording(username, force=True)

                # Mark as deleted (this will also move directory)
                set_user_active_status(
                    conn, username, -1, path_config=self.config
                )

                print(f"✓ Marked {username} as deleted (directory moved)")

            conn.commit()
            conn.close()

            print()
            print(f"✅ Successfully marked {len(users_to_mark)} users as deleted")
            print("   These users will no longer be monitored or appear in active statistics")

        except Exception as e:
            print(f"❌ Error marking deleted users: {e}")
            import traceback
            traceback.print_exc()

    def test_live_notifications(self, username: str):
        """Test sending live notifications for a specific user"""
        print(f"Testing live notifications for: {username}")

        # Check if user has notifications enabled
        has_notifications = self._should_send_notification(username)
        print(f"Notifications enabled: {has_notifications}")

        if has_notifications:
            print("Sending test live_start notification...")
            self._send_live_notification(username, 'live_start', f'🔴 TEST: {username} is now LIVE!')

            print("Sending test live_end notification...")
            self._send_live_notification(username, 'live_end', f'⚫ TEST: {username} has ended their live stream')

            print("✅ Test notifications sent!")
        else:
            print(f"❌ User {username} does not have notifications enabled")

    def _get_user_live_status(self, username: str) -> bool:
        """Get current live status for a user from database"""
        try:
            db_path = self.config.get('database', {}).get('path', './db.sqlite')
            conn = create_connection(db_path)
            cursor = conn.cursor()

            cursor.execute("SELECT is_live FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            conn.close()

            return bool(row[0]) if row else False

        except Exception as e:
            self.logger.error(f"Error getting live status for {username}: {e}")
            return False

    async def _update_user_next_check(self, username: str, just_went_offline: bool = False) -> None:
        """Update next_check time for a user after checking"""
        try:
            db_path = self.config.get('database', {}).get('path', './db.sqlite')
            conn = create_connection(db_path)
            cursor = conn.cursor()

            # Determine which interval to use
            if just_went_offline:
                # User just went offline, use after_live_check_interval for quick recheck
                check_interval = self.config.get('intervals', {}).get('after_live_check_interval', 60)
                interval_type = "after_live"
            else:
                # Normal check, use user's regular check_interval
                cursor.execute("SELECT check_interval FROM users WHERE username = ?", (username,))
                row = cursor.fetchone()
                check_interval = row[0] if row else 300  # Default to 5 minutes if None
                interval_type = "normal"

            # Update next_check to now + check_interval
            cursor.execute("""
                UPDATE users
                SET next_check = DATETIME('now', 'localtime', ?)
                WHERE username = ?
            """, (f"+{int(check_interval)} seconds", username))

            conn.commit()
            # Create user logger for debug message
            user_logger = UserContextLogger(self.logger, username)
            user_logger.debug(f"Updated next_check: +{check_interval}s ({interval_type})")

            conn.close()

        except Exception as e:
            # Create user_logger for error reporting
            user_logger = UserContextLogger(self.logger, username)
            user_logger.error(f"Error updating next_check: {e}")

    async def _update_user_live_status(self, username: str, is_live: int) -> None:
        """Update is_live status for a user in the database

        Args:
            username: Username to update
            is_live: Status value (0=offline, 1=live, 2=starting)
        """
        try:
            user_logger = UserContextLogger(self.logger, username)

            db_path = self.config.get('database', {}).get('path', './db.sqlite')
            conn = create_connection(db_path)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE users
                SET is_live = ?
                WHERE username = ?
            """, (is_live, username))

            conn.commit()
            conn.close()

            status_map = {0: "offline", 1: "live", 2: "starting"}
            status_text = status_map.get(is_live, f"unknown({is_live})")
            user_logger.debug(f"Updated is_live status: {status_text}")

        except Exception as e:
            user_logger.error(f"Error updating is_live status: {e}") if 'user_logger' in locals() else self.logger.error(f"Error updating is_live status for {username}: {e}")

    async def _check_user_exists_in_db(self, username: str) -> bool:
        """Check if user exists in the database"""
        try:
            conn = get_db_connection(self.config['database']['path'])
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM users WHERE username = ?", (username,))
            count = cursor.fetchone()[0]

            conn.close()
            return count > 0

        except Exception as e:
            self.logger.error(f"Error checking if user {username} exists in DB: {e}")
            return False

    async def _get_user_info_from_db(self, username: str) -> dict:
        """Get user information from database"""
        try:
            conn = get_db_connection(self.config['database']['path'])
            cursor = conn.cursor()

            cursor.execute("""
                SELECT username, check_interval, is_live, next_check, is_active, total_lives
                FROM users WHERE username = ?
            """, (username,))

            row = cursor.fetchone()
            conn.close()

            if row:
                return {
                    'username': row[0],
                    'check_interval': row[1],
                    'is_live': row[2],
                    'next_check': row[3],
                    'is_active': row[4],
                    'total_lives': row[5]
                }
            else:
                return {}

        except Exception as e:
            self.logger.error(f"Error getting user info for {username}: {e}")
            return {}

    async def _log_statistics(self) -> None:
        """Log current statistics"""
        try:
            uptime = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0

            # Get user count from database
            conn = get_db_connection(self.config['database']['path'])
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
            users_monitored = cursor.fetchone()[0]
            conn.close()

            # Get component statistics
            active_processes = len(self.metadata_store.get_active_processes())
            self._synchronize_component_statistics()

            self.logger.info("📊 === Statistics ===")
            self.logger.info(f"  Users monitored: {users_monitored}")
            self.logger.info(f"  Active recordings: {active_processes}")
            self.logger.info(f"  Live detections: {self.stats['live_detections']}")
            self.logger.info(f"  Total recordings started: {self.stats['recording_starts']}")
            self.logger.info(f"  Recording stops: {self.stats['recording_stops']}")
            self.logger.info(f"  Recorder health checks: {self.stats['health_checks']}")
            self.logger.info(f"  Process restarts: {self.stats['restarts']}")
            race_conditions = self.stats.get('race_conditions', 0)
            if race_conditions > 0:
                self.logger.info(f"  Race conditions (normal): {race_conditions}")
            room_id_unavailable = self.stats.get('room_id_unavailable', 0)
            if room_id_unavailable > 0:
                self.logger.info(f"  RoomID unavailable: {room_id_unavailable}")
            self.logger.info(f"  Service uptime: {uptime:.1f}s")

            # Report inactive live users if any detected
            if len(self.inactive_live_users) > 0:
                self.logger.info(f"  Inactive users detected live: {len(self.inactive_live_users)}")

            # Report user changes if any
            if self.stats['new_users_added'] > 0 or self.stats['users_deactivated'] > 0:
                self.logger.info(f"  New users added: {self.stats['new_users_added']}")
                self.logger.info(f"  Users deactivated: {self.stats['users_deactivated']}")

            # Reset period-based counters
            self.inactive_live_users.clear()
            self.stats['new_users_added'] = 0
            self.stats['users_deactivated'] = 0

        except Exception as e:
            self.logger.error(f"Error logging statistics: {e}")

    # Method _refresh_live_detection_service removed - not needed with Selenium approach

    async def _shutdown_supervisor(self) -> None:
        """Shutdown supervisor components"""
        self.logger.info("🛑 Shutting down Selenium Supervisor...")

        try:
            # Record supervisor shutdown
            self.supervisor_status.stop_supervisor()

            # Stop health monitoring
            await self.health_monitor.stop_monitoring()

            # No HTTP detection service to stop - using selenium_live.db

            # Remove supervisor lock file
            self.remove_server_lock()

            # Cancel any remaining tasks in the current event loop
            loop = asyncio.get_event_loop()

            # Get all tasks (compatible with Python 3.7+)
            if hasattr(asyncio, 'all_tasks'):
                all_tasks = asyncio.all_tasks(loop)
            else:
                all_tasks = asyncio.Task.all_tasks(loop)

            pending_tasks = [task for task in all_tasks if not task.done() and task != asyncio.current_task()]

            if pending_tasks:
                self.logger.info(f"Cancelling {len(pending_tasks)} remaining tasks...")
                for task in pending_tasks:
                    task.cancel()

                # Wait for all tasks to complete with a timeout
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending_tasks, return_exceptions=True),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    self.logger.warning("Some tasks did not complete within timeout")

            # Shutdown the default executor to ensure all threads are cleaned up
            loop = asyncio.get_event_loop()
            if hasattr(loop, '_default_executor') and loop._default_executor:
                self.logger.debug("Shutting down default executor...")
                loop._default_executor.shutdown(wait=True)

            self.logger.info("✅ Supervisor shutdown complete")

        except Exception as e:
            self.logger.error(f"Error during supervisor shutdown: {e}")

    async def list_processes(self, detailed: bool = False) -> None:
        """List all active processes"""
        try:
            processes = await self.shutdown_handler.list_active_processes(detailed)

            if not processes:
                print("No active live recording processes found.")
                return

            print(f"Found {len(processes)} active live recording processes:")
            print()

            for process in processes:
                started_str = process['started_at'].strftime('%Y-%m-%d %H:%M:%S')
                db_status = process['health_status']
                real_status = process.get('real_status', db_status)
                status_type = process.get('status_type', 'running')
                restarts = process['restart_count']
                is_running = process.get('is_running', True)

                # Choose icon based on real status
                if status_type == 'dead':
                    status_icon = "⚰️"
                    display_status = f"{real_status} (DB: {db_status})"
                elif real_status == "healthy" and is_running:
                    status_icon = "✅"
                    display_status = real_status
                elif real_status == "unhealthy":
                    status_icon = "❌"
                    display_status = real_status
                elif real_status == "restarting":
                    status_icon = "🔄"
                    display_status = real_status
                else:
                    status_icon = "⚪"
                    display_status = real_status

                print(f"{status_icon} {process['username']}")
                print(f"    PID: {process['pid']}")
                print(f"    Started: {started_str}")
                print(f"    Status: {display_status}")
                if not is_running:
                    print("    ⚠️ Process not running in system")
                if restarts > 0:
                    print(f"    Restarts: {restarts}")

                if detailed and 'recording_file_path' in process:
                    if process.get('file_exists', False):
                        size_mb = process.get('file_size_current', 0) / (1024 * 1024)
                        print(f"    File: {Path(process['recording_file_path']).name} ({size_mb:.1f}MB)")
                    else:
                        print(f"    File: {process['recording_file_path']} (not found)")

                print()

        except Exception as e:
            print(f"Error listing processes: {e}")

    async def stop_all_processes(self, force: bool = False, orphans_only: bool = False) -> None:
        """Stop all tracked processes, or only reviewed orphan/duplicate processes."""
        try:
            if orphans_only:
                candidates = self.process_manager.build_orphan_cleanup_plan()
                if not candidates:
                    print("No orphaned or redundant recorder processes found.")
                    return

                print(f"Found {len(candidates)} orphaned/redundant recorder processes:\n")
                for candidate in candidates:
                    process = candidate.process
                    print(f"  - {process.username} (PID: {process.pid})")
                    print(f"    Reason: {candidate.reason}")
                    if process.output_file:
                        print(f"    File: {process.output_file}")
                    if candidate.kept_pid is not None:
                        print(f"    Keeping PID: {candidate.kept_pid}")

                print("\nStop only the processes listed above? [y/N]: ", end='', flush=True)
                if input().strip().lower() != 'y':
                    print("Cancelled. No processes were stopped.")
                    return

                results = await self.process_manager.stop_orphan_candidates(
                    candidates,
                    force=force,
                )
                stopped = sum(1 for success in results.values() if success)
                failed = len(results) - stopped
                print(f"\nStopped orphaned/redundant processes: {stopped}/{len(results)}")
                if failed:
                    print(f"Failed or skipped: {failed}")
                return

            result = await self.shutdown_handler.shutdown_all_processes(force=force, show_progress=True)

            if result.total_processes == 0:
                return

            # Verify shutdown
            all_stopped, remaining = await self.shutdown_handler.verify_shutdown_complete()

            if not all_stopped:
                print(f"\nWarning: {len(remaining)} processes may still be running:")
                for username in remaining:
                    print(f"  - {username}")
                print("\nYou may need to use system tools to terminate these processes manually.")

        except Exception as e:
            print(f"Error stopping processes: {e}")

    async def test_user(self, username: str) -> None:
        """Test specific user: check if live and start recording if needed"""
        print(f"Testing user: {username}")
        print()

        try:
            # Check if user already has an active recording
            existing_process = self.metadata_store.get_process_by_username(username)
            if existing_process:
                print(f"✅ User {username} already has an active recording:")
                print(f"    PID: {existing_process.pid}")
                print(f"    Status: {existing_process.health_status}")
                print(f"    Started: {existing_process.started_at}")
                if existing_process.recording_file_path:
                    try:
                        file_path = Path(existing_process.recording_file_path)
                        if file_path.exists():
                            size_mb = file_path.stat().st_size / (1024 * 1024)
                            print(f"    File: {size_mb:.1f}MB")
                        else:
                            print("    File: Not found")
                    except:
                        pass
                return

            # First check if user exists in database
            user_exists = await self._check_user_exists_in_db(username)
            if not user_exists:
                print(f"❌ User '{username}' not found in database")
                print("    This user is not being monitored by the system")
                print("    Add the user to the database to start monitoring")
                return

            print(f"Checking if {username} is live...")

            # Simply check the user directly
            await self._check_and_start_recording_if_needed(username)

            print(f"✅ Check completed for {username}")
            return

            if False:  # Old code disabled
                print(f"🔴 {username} is LIVE! Starting recording...")

                # Get room_id from live detection service
                session = self.live_detection_service.get_user_session(username)
                room_id = session.room_id if session else None

                if room_id:
                    print(f"    Room ID: {room_id}")

                # Start recording
                result = await self.process_manager.start_live_recording(username, room_id)

                if result.success:
                    print("✅ Recording started successfully!")
                    print(f"    PID: {result.pid}")
                    print(f"    Database ID: {result.process_id}")

                    # Wait a moment to let it stabilize
                    await asyncio.sleep(3)

                    # Check if process is still running
                    if self.process_manager._is_process_running(result.pid):
                        print("    Process is running and detached ✅")

                        # Show recording file info
                        process_info = self.metadata_store.get_process_by_id(result.process_id)
                        if process_info and process_info.recording_file_path:
                            print(f"    Recording file: {process_info.recording_file_path}")
                    else:
                        print("    ❌ Process terminated unexpectedly")

                else:
                    print(f"❌ Failed to start recording: {result.error}")

            else:
                # Get user info to show additional context
                user_info = await self._get_user_info_from_db(username)
                print(f"⚫ {username} exists in database but is currently OFFLINE (not in Selenium live list)")
                if user_info:
                    print(f"    Check interval: {user_info.get('check_interval', 'Unknown')}s")
                    print(f"    Active monitoring: {'Yes' if user_info.get('is_active') else 'No'}")
                    total_lives = user_info.get('total_lives', 0)
                    print(f"    Total lives recorded: {total_lives}")
                    next_check = user_info.get('next_check')
                    if next_check:
                        print(f"    Next check: {next_check}")
                    else:
                        print("    Next check: Not scheduled")
                print("    No recording needed - user is not live")

            # No cleanup needed for Selenium approach

        except Exception as e:
            print(f"Error testing user {username}: {e}")

    async def restart_user(self, username: str) -> bool:
        """Restart one active recording process for a specific user."""
        print(f"Restarting user: {username}")
        print()

        try:
            if normalize_tiktok_username(username, strip_at=False) != username:
                print(f"❌ Invalid TikTok username: {username}")
                return False
            process_info = self.metadata_store.get_process_by_username(username)
            if not process_info:
                print(f"❌ No active recording process found for {username}")
                print("    Use --test-user only if you want to start a missing recording.")
                return False

            if not self.metadata_store.is_user_live(username):
                print(f"❌ {username} is not marked as live in the database")
                print("    Refusing restart to avoid stopping a process and failing to register the replacement.")
                return False

            print("Active recording found:")
            print(f"    PID: {process_info.pid}")
            print(f"    Status: {process_info.health_status}")
            started_at = getattr(process_info, 'started_at', None)
            if started_at:
                print(f"    Started: {started_at}")
            room_id = getattr(process_info, 'room_id', None)
            if room_id:
                print(f"    Room ID: {room_id}")
            print()
            print("🔄 Restarting via process manager...")

            success = await self.process_manager.restart_live_recording(
                process_info,
                ["manual CLI restart"]
            )

            if success:
                restarted = self.metadata_store.get_process_by_username(username)
                print("✅ Restart successful")
                if restarted:
                    print(f"    New PID: {restarted.pid}")
                    recording_file_path = getattr(restarted, 'recording_file_path', None)
                    if recording_file_path:
                        print(f"    File: {recording_file_path}")
                return True

            print("❌ Restart failed")
            return False

        except Exception as e:
            print(f"❌ Error restarting user {username}: {e}")
            return False

    async def check_user_live_only(self, username: str) -> None:
        """Check if user is live without starting recording"""
        print(f"Checking live status for user: {username}")
        print()

        try:
            # First check if user exists in database
            user_exists = await self._check_user_exists_in_db(username)
            if not user_exists:
                print(f"❌ User '{username}' not found in database")
                print("    This user is not being monitored by the system")
                print("    Add the user to the database to start monitoring")
                return

            print(f"Checking if {username} is live in Selenium database...")

            # Check if user is live according to Selenium
            selenium_live_users = self._get_selenium_live_users()
            is_live = username in selenium_live_users

            if is_live:
                print(f"🔴 {username} is LIVE (detected by Selenium)!")
                print("    Room ID: Not available from Selenium")

                # Check if already has active recording
                existing_process = self.metadata_store.get_process_by_username(username)
                if existing_process:
                    print(f"    Already recording: PID {existing_process.pid}")

                    # Show session duration
                    if existing_process.started_at:
                        duration = datetime.now() - existing_process.started_at
                        hours = int(duration.total_seconds() // 3600)
                        minutes = int((duration.total_seconds() % 3600) // 60)
                        seconds = int(duration.total_seconds() % 60)
                        print(f"    Session duration: {hours:02d}:{minutes:02d}:{seconds:02d}")
                        print(f"    Started at: {existing_process.started_at.strftime('%Y-%m-%d %H:%M:%S')}")

                    # Show recording file info
                    if existing_process.recording_file_path:
                        print(f"    Recording file: {existing_process.recording_file_path}")

                        # Check file stats
                        try:
                            from pathlib import Path

                            file_path = Path(existing_process.recording_file_path)
                            if file_path.exists():
                                file_stats = file_path.stat()
                                file_size_mb = file_stats.st_size / (1024 * 1024)

                                # Last modification time
                                last_modified = datetime.fromtimestamp(file_stats.st_mtime)
                                time_since_modified = datetime.now() - last_modified

                                print(f"    File size: {file_size_mb:.1f} MB")
                                print(f"    Last modified: {last_modified.strftime('%Y-%m-%d %H:%M:%S')}")

                                if time_since_modified.total_seconds() < 300:  # Less than 5 minutes
                                    print(f"    File status: ✅ Recently updated ({int(time_since_modified.total_seconds())}s ago)")
                                elif time_since_modified.total_seconds() < 1800:  # Less than 30 minutes
                                    print(f"    File status: ⚠️ Stale ({int(time_since_modified.total_seconds()/60)}m ago)")
                                else:
                                    print(f"    File status: ❌ Very stale ({int(time_since_modified.total_seconds()/60)}m ago)")
                            else:
                                print("    File status: ❌ File not found")

                        except Exception as e:
                            print(f"    File status: ❌ Error checking file: {e}")

                else:
                    print("    No active recording found")

            else:
                # Get user info to show additional context
                user_info = await self._get_user_info_from_db(username)
                print(f"⚫ {username} is currently OFFLINE (not in Selenium live list)")
                if user_info:
                    print(f"    Check interval: {user_info.get('check_interval', 'Unknown')}s")
                    print(f"    Active monitoring: {'Yes' if user_info.get('is_active') else 'No'}")
                    total_lives = user_info.get('total_lives', 0)
                    print(f"    Total lives recorded: {total_lives}")
                    next_check = user_info.get('next_check')
                    if next_check:
                        print(f"    Next check: {next_check}")
                    else:
                        print("    Next check: Not scheduled")

            # No cleanup needed for Selenium approach

        except Exception as e:
            print(f"Error checking user {username}: {e}")

    async def health_check_only(self) -> None:
        """Perform comprehensive health check including file modification times"""
        print("Performing comprehensive health check on all active processes...")
        print()

        try:
            # Get active processes with real status checking
            processes = await self.shutdown_handler.list_active_processes(detailed=True)

            if not processes:
                print("No active processes found to check.")
                return

            max_stale_time = self.config['persistent_live_system'].get('max_file_stale_time', 1800)
            current_time = time.time()
            stale_processes = []

            healthy_count = 0

            print(f"Health check results ({len(processes)} processes):")
            print(f"Max file stale time: {max_stale_time//60} minutes")
            print()

            for process in processes:
                username = process['username']
                pid = process['pid']
                is_running = process.get('is_running', False)
                status_type = process.get('status_type', 'unknown')

                # Determine overall health
                is_healthy = is_running and status_type == 'running'
                issues = []

                if not is_running:
                    issues.append("Process not running")

                # Check file status and modification time
                file_info = {}
                if process.get('recording_file_path'):
                    file_path = Path(process['recording_file_path'])
                    try:
                        if file_path.exists():
                            stat = file_path.stat()
                            file_size = stat.st_size
                            file_mtime = stat.st_mtime
                            seconds_since_modified = int(current_time - file_mtime)

                            file_info = {
                                'exists': True,
                                'size_mb': file_size / (1024 * 1024),
                                'size_bytes': file_size,
                                'seconds_since_modified': seconds_since_modified,
                                'last_modified': datetime.fromtimestamp(file_mtime).strftime('%H:%M:%S')
                            }

                            # Check for zero-byte files
                            if file_size == 0:
                                issues.append("Zero-byte file")
                                is_healthy = False

                            # Check for stale files (not modified recently)
                            elif seconds_since_modified > max_stale_time and is_running:
                                issues.append(f"File not modified for {seconds_since_modified//60} minutes (max: {max_stale_time//60} min)")
                                is_healthy = False
                                stale_processes.append(process)

                        else:
                            file_info = {'exists': False}
                            issues.append("Recording file not found")
                            is_healthy = False

                    except Exception as e:
                        file_info = {'error': str(e)}
                        issues.append(f"File access error: {e}")
                        is_healthy = False

                if is_healthy:
                    healthy_count += 1

                # Display results
                status_icon = "✅" if is_healthy else "❌"
                print(f"{status_icon} {username}")
                print(f"    PID: {pid}")
                print(f"    Process: {'Running' if is_running else 'Not running'}")

                if file_info.get('exists'):
                    print(f"    File: {file_info['size_mb']:.1f}MB")
                    print(f"    Last modified: {file_info['last_modified']} ({file_info['seconds_since_modified']}s ago)")

                    # Color code based on modification time
                    if file_info['seconds_since_modified'] < 300:  # < 5 minutes
                        mod_status = "🟢 Recent"
                    elif file_info['seconds_since_modified'] < max_stale_time:  # < max stale time
                        mod_status = "🟡 Normal"
                    else:  # > max stale time
                        mod_status = "🔴 Stale"
                    print(f"    File status: {mod_status}")

                elif file_info.get('exists') is False:
                    print("    File: Not found")
                elif file_info.get('error'):
                    print(f"    File: Error - {file_info['error']}")

                if issues:
                    for issue in issues:
                        print(f"    ⚠️ Issue: {issue}")

                print()

            # Summary
            print("📊 Summary:")
            print(f"    Healthy processes: {healthy_count}/{len(processes)}")
            if stale_processes:
                print(f"    Stale processes (need restart): {len(stale_processes)}")
                print(f"    Stale processes: {', '.join(p['username'] for p in stale_processes)}")

                # Ask if user wants to restart stale processes
                print()
                print("🔄 Stale processes detected. These recordings may be frozen.")
                print("    Use --restart-stale-processes to automatically restart them.")
            else:
                print("    No stale processes detected")

        except Exception as e:
            print(f"Error performing health check: {e}")

    async def cleanup_dead_processes(self) -> None:
        """Manual cleanup of dead processes"""
        print("🧹 Cleaning up dead processes...")
        print()

        try:
            # Get cleanup statistics
            stats = await self.shutdown_handler.cleanup_dead_processes_command()

            if 'error' in stats:
                print(f"❌ Error during cleanup: {stats['error']}")
                return

            total_checked = stats['total_checked']
            dead_found = stats['dead_found']
            cleaned_up = stats['cleaned_up']

            print("📊 Cleanup Results:")
            print(f"    Total processes checked: {total_checked}")
            print(f"    Dead processes found: {dead_found}")
            print(f"    Successfully cleaned up: {cleaned_up}")

            if dead_found == 0:
                print("✅ No dead processes found - database is clean!")
            elif cleaned_up == dead_found:
                print(f"✅ Successfully cleaned up all {cleaned_up} dead processes")
            else:
                print(f"⚠️ Some processes could not be cleaned up ({cleaned_up}/{dead_found})")

        except Exception as e:
            print(f"❌ Error during cleanup: {e}")

    async def restart_stale_processes(self) -> None:
        """Find and restart processes with stale recording files"""
        print("🔄 Finding and restarting stale recording processes...")
        print()

        try:
            # Get active processes with detailed info
            processes = await self.shutdown_handler.list_active_processes(detailed=True)

            if not processes:
                print("No active processes found to check.")
                return

            max_stale_time = self.config['persistent_live_system'].get('max_file_stale_time', 1800)
            current_time = time.time()
            stale_processes = []

            # Find stale processes
            for process in processes:
                if not process.get('is_running', False):
                    continue  # Skip non-running processes

                if not process.get('recording_file_path'):
                    continue  # Skip processes without recording file

                try:
                    file_path = Path(process['recording_file_path'])
                    if file_path.exists():
                        stat = file_path.stat()
                        file_mtime = stat.st_mtime
                        seconds_since_modified = int(current_time - file_mtime)

                        if seconds_since_modified > max_stale_time:
                            stale_processes.append({
                                'process': process,
                                'seconds_stale': seconds_since_modified,
                                'minutes_stale': seconds_since_modified // 60
                            })
                except Exception as e:
                    print(f"⚠️ Error checking file for {process['username']}: {e}")

            if not stale_processes:
                print("✅ No stale processes found - all recordings are actively writing!")
                return

            print(f"Found {len(stale_processes)} stale processes:")
            for stale in stale_processes:
                process = stale['process']
                minutes_stale = stale['minutes_stale']
                print(f"  📁 {process['username']} - file not modified for {minutes_stale} minutes")
            print()

            # Restart each stale process
            restarted_count = 0
            for stale in stale_processes:
                process = stale['process']
                username = process['username']
                pid = process['pid']

                try:
                    print(f"🔄 Restarting {username} (PID: {pid})...")

                    # Stop the current process
                    success = await self.process_manager.stop_live_recording(username, graceful=False)
                    if success:
                        print("  ✅ Stopped stale process")

                        # Wait a moment
                        await asyncio.sleep(2)

                        # Start new recording
                        room_id = process.get('room_id')
                        result = await self.process_manager.start_live_recording(username, room_id, force_restart=True)

                        if result.success:
                            print(f"  ✅ Started new recording (PID: {result.pid})")
                            restarted_count += 1
                        else:
                            print(f"  ❌ Failed to start new recording: {result.error}")
                    else:
                        print("  ❌ Failed to stop stale process")

                except Exception as e:
                    print(f"  ❌ Error restarting {username}: {e}")

                print()

            # Summary
            print("📊 Restart Results:")
            print(f"    Stale processes found: {len(stale_processes)}")
            print(f"    Successfully restarted: {restarted_count}")

            if restarted_count == len(stale_processes):
                print("✅ All stale processes successfully restarted!")
            elif restarted_count > 0:
                print(f"⚠️ {restarted_count}/{len(stale_processes)} processes restarted successfully")
            else:
                print("❌ Failed to restart any stale processes")

        except Exception as e:
            print(f"❌ Error during stale process restart: {e}")

    async def check_database_sync(self) -> None:
        """Check database synchronization between users and live_processes tables"""
        print("🔍 Checking database synchronization...")
        print()

        try:
            # Get users marked as live
            conn = get_db_connection(self.database_path)
            live_users_query = """
                SELECT u.id, u.username, u.is_live, u.total_lives,
                       l.id as live_session_id, l.started_at, l.ended_at
                FROM users u
                LEFT JOIN lives l ON u.id = l.user_id AND l.ended_at IS NULL
                WHERE u.is_live = 1
                ORDER BY u.username
            """
            live_users = conn.execute(live_users_query).fetchall()

            # Get active recording processes
            active_processes = self.metadata_store.get_active_processes()
            process_usernames = {p.username for p in active_processes}

            print("📊 Synchronization Status:")
            print(f"    Users marked as live: {len(live_users)}")
            print(f"    Active recording processes: {len(active_processes)}")
            print()

            # Check for discrepancies
            discrepancies = []
            orphaned_live_users = []

            # Check users marked as live without recording processes
            for user_row in live_users:
                user_id, username, is_live, total_lives, live_session_id, started_at, ended_at = user_row

                if username not in process_usernames:
                    # Found user marked as live but no recording process
                    discrepancies.append({
                        'type': 'orphaned_live_user',
                        'username': username,
                        'user_id': user_id,
                        'live_session_id': live_session_id,
                        'started_at': started_at
                    })
                    orphaned_live_users.append(username)

            # Check recording processes without live users
            orphaned_processes = []
            live_usernames = {row[1] for row in live_users}  # username is index 1

            for process in active_processes:
                if process.username not in live_usernames:
                    # Found recording process but user not marked as live
                    discrepancies.append({
                        'type': 'orphaned_process',
                        'username': process.username,
                        'pid': process.pid,
                        'started_at': process.started_at
                    })
                    orphaned_processes.append(process.username)

            # Display results
            if not discrepancies:
                print("✅ Database synchronization is perfect!")
                print("   All users marked as live have active recording processes.")
                print("   All recording processes correspond to users marked as live.")
                return

            print("⚠️ Synchronization discrepancies found:")
            print()

            # Show orphaned live users
            if orphaned_live_users:
                print(f"🔸 Users marked as live without recording processes ({len(orphaned_live_users)}):")
                for disc in discrepancies:
                    if disc['type'] == 'orphaned_live_user':
                        started_str = disc['started_at'] if disc['started_at'] else 'Unknown'
                        print(f"    • {disc['username']} (live since: {started_str})")

                        # Check if user is actually live on TikTok
                        print("      Checking TikTok live status...")
                        try:
                            is_actually_live = await self._check_user_live_status_quick(disc['username'])
                            if is_actually_live:
                                print("      ✅ User is actually live on TikTok - missing recording process")
                            else:
                                print("      ❌ User is NOT live on TikTok - orphaned database entry")
                        except Exception as e:
                            print(f"      ⚠️ Could not check live status: {e}")
                print()

            # Show orphaned processes
            if orphaned_processes:
                print(f"🔸 Recording processes without live user status ({len(orphaned_processes)}):")
                for disc in discrepancies:
                    if disc['type'] == 'orphaned_process':
                        print(f"    • {disc['username']} (PID: {disc['pid']}, started: {disc['started_at']})")
                print()

            # Offer suggestions
            print("🔧 Suggested Actions:")
            if orphaned_live_users:
                print("    For orphaned live users:")
                print("    1. Run --test-user <username> to start missing recordings")
                print("    2. Or manually clean database if user is no longer live")

            if orphaned_processes:
                print("    For orphaned processes:")
                print("    1. Check if recordings are working correctly")
                print("    2. Consider stopping orphaned processes if needed")

            conn.close()

        except Exception as e:
            print(f"❌ Error during synchronization check: {e}")

    async def _check_user_live_status_quick(self, username: str) -> bool:
        """Quick check if user is actually live according to Selenium"""
        try:
            # Check if user is in selenium live users list
            selenium_live_users = self._get_selenium_live_users()
            return username in selenium_live_users
        except Exception:
            return False

    async def _async_check_live_for_health_monitor(self, username: str) -> Optional[bool]:
        """Async callback for health monitor to check if user is live"""
        try:
            # Check if user is in selenium live users list
            selenium_live_users = self._get_selenium_live_users(raise_on_error=True)
            return username in selenium_live_users
        except Exception as e:
            self.logger.error(f"Error checking live status for health monitor: {e}")
            return None

    async def _async_check_identity_for_health_monitor(self, username: str) -> Dict[str, Any]:
        """Fetch current API identity with a global one-request-per-second limit."""
        async with self._identity_request_lock:
            elapsed = time.monotonic() - self._last_identity_request_at
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)
            self._last_identity_request_at = time.monotonic()

            try:
                def fetch_identity():
                    cookies = (
                        None
                        if self.no_cookies
                        else read_cookies(self.identity_cookie_file_path)
                    )
                    return TikTokAPI(proxy=None, cookies=cookies).get_live_identity(username)

                identity = await asyncio.to_thread(
                    fetch_identity,
                )
            except Exception as error:
                UserContextLogger(self.logger, username).error(
                    f"Could not fetch current live identity: {error}"
                )
                return {'state': 'unknown'}

            room_id = identity.get('room_id')
            status = identity.get('status')
            identity_state = str(identity.get('state', '')).lower()
            if identity_state == 'offline':
                return {'state': 'offline'}
            if (
                identity_state == 'live'
                or (not identity_state and room_id and status in (2, '2'))
            ):
                return {
                    'state': 'live',
                    'stream_id': identity.get('stream_id'),
                    'start_time': identity.get('start_time'),
                    'room_id': room_id,
                }
            if not identity_state and status is not None and status not in (2, '2'):
                return {'state': 'offline'}
            return {'state': 'unknown'}

    async def cleanup_offline_users(self) -> None:
        """Find and cleanup processes for users who are no longer live"""
        print("🧹 Finding and cleaning up processes for offline users...")
        print()

        try:
            # Get all active processes
            processes = await self.shutdown_handler.list_active_processes(detailed=True)

            if not processes:
                print("No active processes found.")
                return

            print(f"Found {len(processes)} active processes to check...")
            offline_processes = []

            # Check each user's live status
            for process in processes:
                username = process['username']
                print(f"Checking {username}...", end='', flush=True)

                is_live = await self._check_user_live_status_quick(username)

                if not is_live:
                    print(" OFFLINE")
                    offline_processes.append(process)
                else:
                    print(" LIVE")

            if not offline_processes:
                print("\n✅ All active processes belong to live users!")
                return

            print(f"\n⚠️ Found {len(offline_processes)} processes for offline users:")
            for process in offline_processes:
                print(f"  - {process['username']} (PID: {process['pid']})")

            # Ask for confirmation
            print("\nDo you want to stop these processes? [y/N]: ", end='', flush=True)
            response = input().strip().lower()

            if response != 'y':
                print("Cancelled.")
                return

            # Stop each offline process
            print("\nStopping processes...")
            stopped_count = 0

            for process in offline_processes:
                try:
                    # Update database to reflect user is offline
                    self.metadata_store.update_health_status(
                        process['id'],
                        'stopped_user_offline',
                        process.get('file_size_db', 0)
                    )
                    self.metadata_store.mark_process_stopped(process['id'])
                    self.metadata_store.reset_user_live_status(process['username'])

                    # Try to kill the process if it's still running
                    if process.get('is_running', False):
                        try:
                            os.kill(process['pid'], signal.SIGTERM)
                            print(f"  ✅ Stopped {process['username']} (PID: {process['pid']})")
                        except ProcessLookupError:
                            print(f"  ⚪ Process {process['username']} already stopped")
                    else:
                        print(f"  ⚪ Cleaned up database entry for {process['username']}")

                    stopped_count += 1

                except Exception as e:
                    print(f"  ❌ Failed to stop {process['username']}: {e}")

            print("\n📊 Summary:")
            print(f"    Total offline processes found: {len(offline_processes)}")
            print(f"    Successfully stopped/cleaned: {stopped_count}")

            if stopped_count == len(offline_processes):
                print("✅ All offline user processes have been cleaned up!")
            else:
                print("⚠️ Some processes could not be cleaned up")

        except Exception as e:
            print(f"❌ Error during offline user cleanup: {e}")


async def main():
    """Main entry point"""

    # If no arguments provided, show full help (like -h)
    if len(sys.argv) == 1:
        sys.argv.append('-h')

    parser = argparse.ArgumentParser(
        description='Selenium-based TikTok Live Supervisor - Uses selenium_live.db for live detection',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
🖥️  SERVER MODE (Continuous daemon):
  --server                🖥️ Run persistent supervisor in SERVER mode (continuous monitoring)

📋 READ-ONLY CLI OPERATIONS (Info only, no state changes):
  --list-processes        📋 List all active processes and exit
  --health-check-only     📋 Perform health check only and exit
  --check-sync            📋 Check database synchronization status and exit
  --check-user-live       📋 Check if user is live (read-only) and exit
  --test-notifications    📋 Test notifications (no recording) and exit

⚠️  STATE-MODIFYING CLI OPERATIONS (Changes database/processes):
  --stop-all-lives        🔴 STOP all active recordings and exit
  --orphans-only          🔴 With --stop-all-lives, stop only listed orphans/duplicates
  --test-user             🔴 START recording if user is live and exit
  --restart-user          🔴 RESTART one active recording process and exit
  --cleanup-dead-processes 🔴 REMOVE dead processes from database and exit
  --restart-stale-processes 🔴 RESTART stale recording processes and exit
  --cleanup-offline-users 🔴 REMOVE processes for offline users and exit

🔧 CONFIGURATION OPTIONS:
  --config, -c            Path to configuration file (default: config.yaml)
  --debug, -d             Enable debug logging
  --force                 Skip graceful shutdown when stopping processes
  --detailed              Show detailed information when listing processes

USAGE EXAMPLES:
  uv run python selenium_supervisor.py --server
  uv run python selenium_supervisor.py --list-processes
  uv run python selenium_supervisor.py --test-user username123
  uv run python selenium_supervisor.py --stop-all-lives --orphans-only
'''
    )
    parser.add_argument(
        '--config', '-c',
        default='config.yaml',
        help='Path to configuration file (default: config.yaml)'
    )
    parser.add_argument(
        '--debug', '-d',
        action='store_true',
        help='Enable debug logging'
    )
    parser.add_argument(
        '--stop-all-lives',
        action='store_true',
        help='Stop all active live recordings and exit'
    )
    parser.add_argument(
        '--orphans-only',
        action='store_true',
        help='With --stop-all-lives, show and stop only orphaned/redundant recorder processes'
    )
    parser.add_argument(
        '--list-processes',
        action='store_true',
        help='List all active processes and exit'
    )
    parser.add_argument(
        '--health-check-only',
        action='store_true',
        help='Perform health check only and exit'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Use force when stopping processes (skip graceful shutdown)'
    )
    parser.add_argument(
        '--detailed',
        action='store_true',
        help='Show detailed information when listing processes'
    )
    parser.add_argument(
        '--test-user',
        metavar='USERNAME',
        help='Test specific user: check if live and start recording if needed, then exit'
    )
    parser.add_argument(
        '--restart-user',
        metavar='USERNAME',
        help='Restart active recording for specific user, then exit'
    )
    parser.add_argument(
        '--cleanup-dead-processes',
        action='store_true',
        help='Find and cleanup dead processes in database, then exit'
    )
    parser.add_argument(
        '--restart-stale-processes',
        action='store_true',
        help='Find and restart processes with stale recording files, then exit'
    )
    parser.add_argument(
        '--check-sync',
        action='store_true',
        help='Check database synchronization between users and live_processes tables'
    )
    parser.add_argument(
        '--test-notifications',
        metavar='USERNAME',
        help='Test live notifications for specific user, then exit'
    )
    parser.add_argument(
        '--check-user-live',
        metavar='USERNAME',
        help='Check if user is live (without starting recording), then exit'
    )
    parser.add_argument(
        '--cleanup-offline-users',
        action='store_true',
        help='Find and cleanup processes for users who are no longer live, then exit'
    )
    parser.add_argument(
        '--deactivate-user',
        metavar='USERNAME',
        help='Deactivate a user: stop recording if live, move directory to inactive_users, then exit'
    )
    parser.add_argument(
        '--server',
        action='store_true',
        help='🖥️ Run in SERVER mode (continuous monitoring daemon)'
    )
    parser.add_argument(
        '--no-cookies',
        action='store_true',
        help='Start supervisor without using cookies file (no authentication)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Run in dry-run mode - detect live users but do not start recordings'
    )
    parser.add_argument(
        '--mark-deleted-users',
        action='store_true',
        help='Find users with tt_user_id or tt_secuid = -1 and mark them as deleted (is_active = -1)'
    )

    args = parser.parse_args()

    if args.orphans_only and not args.stop_all_lives:
        parser.error('--orphans-only requires --stop-all-lives')

    # Check if this is a CLI operation (not server mode)
    cli_operations = [
        args.stop_all_lives,
        args.list_processes,
        args.health_check_only,
        args.test_user,
        args.restart_user,
        args.cleanup_dead_processes,
        args.restart_stale_processes,
        args.check_sync,
        args.test_notifications,
        args.check_user_live,
        args.cleanup_offline_users,
        args.deactivate_user,
        args.mark_deleted_users
    ]
    is_cli_operation = any(cli_operations)

    # Validate argument combinations
    if args.server and is_cli_operation:
        print("❌ Error: --server mode cannot be combined with CLI operations")
        print("   Use either --server for daemon mode OR specific CLI commands")
        sys.exit(1)

    if not args.server and not is_cli_operation:
        print("❌ Error: Missing operation mode")
        print("   Use --server for continuous monitoring OR specify a CLI operation")
        print("   Run with -h for help")
        sys.exit(1)

    # Create supervisor instance - don't rotate logs for CLI operations
    supervisor = SeleniumSupervisor(
        config_path=args.config,
        debug=args.debug,
        rotate_logs=not is_cli_operation,
        no_cookies=args.no_cookies,
        dry_run=args.dry_run
    )

    try:
        if args.stop_all_lives:
            # Stop all processes and exit
            await supervisor.stop_all_processes(
                force=args.force,
                orphans_only=args.orphans_only,
            )

        elif args.list_processes:
            # List processes and exit
            await supervisor.list_processes(detailed=args.detailed)

        elif args.health_check_only:
            # Health check only and exit
            await supervisor.health_check_only()

        elif args.test_user:
            # Test specific user and exit
            await supervisor.test_user(args.test_user)

        elif args.restart_user:
            # Restart specific user and exit
            success = await supervisor.restart_user(args.restart_user)
            if not success:
                sys.exit(1)

        elif args.cleanup_dead_processes:
            # Cleanup dead processes and exit
            await supervisor.cleanup_dead_processes()

        elif args.restart_stale_processes:
            # Restart stale processes and exit
            await supervisor.restart_stale_processes()

        elif args.check_sync:
            # Check database synchronization and exit
            await supervisor.check_database_sync()

        elif args.test_notifications:
            # Test notifications for specific user and exit
            supervisor.test_live_notifications(args.test_notifications)

        elif args.check_user_live:
            # Check if user is live (without recording) and exit
            await supervisor.check_user_live_only(args.check_user_live)

        elif args.cleanup_offline_users:
            # Cleanup processes for offline users and exit
            await supervisor.cleanup_offline_users()

        elif args.deactivate_user:
            # Deactivate specific user and exit
            await supervisor.deactivate_user(args.deactivate_user)

        elif args.mark_deleted_users:
            # Mark users with invalid TikTok IDs as deleted and exit
            await supervisor.mark_deleted_users()

        elif args.server:
            # SERVER mode - continuous monitoring
            print("🖥️ Starting TikTok Live Supervisor in SERVER mode...")

            # Check for existing supervisor lock ONLY in --server mode
            if supervisor.check_server_lock():
                sys.exit(1)

            # Create lock file for this instance
            if not supervisor.create_server_lock():
                print("❌ Error: Failed to create supervisor lock file")
                sys.exit(1)

            await supervisor.start_supervisor()

        else:
            # This should never happen due to validation above
            print("❌ No valid operation specified")
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nShutdown requested by user")
    except asyncio.CancelledError:
        # This is expected when shutting down - don't print error
        pass
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Already handled in main()
        pass
    finally:
        # Force exit if program is still hanging after shutdown
        import threading
        import time

        # Give a moment for logs to flush
        time.sleep(0.5)

        # Check if there are any non-daemon threads still running
        active_threads = [t for t in threading.enumerate() if t.is_alive() and not t.daemon]
        if len(active_threads) > 1:  # More than just the main thread
            print(f"Warning: {len(active_threads)-1} threads still active, forcing exit...")
            import os
            os._exit(0)
