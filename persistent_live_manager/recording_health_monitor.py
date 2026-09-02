"""
Recording Health Monitor

Monitors the health of persistent live recording processes and files.
Performs automatic restart of unhealthy processes and handles file validation.
"""

import asyncio
import logging
import os
import psutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime
from pathlib import Path
import time

# Add project root to path for imports
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import UserContextLogger for username-prefixed logging
from utils.user_context_logger import UserContextLogger
from utils.config_paths import resolve_path
from recorder.utils.stream_parts import existing_stream_parts, stream_part_path

from .process_metadata_store import ProcessMetadataStore, ProcessInfo
from .process_inventory import RecorderProcess


class HealthCheckResult:
    """Result of a health check operation"""

    def __init__(self, process_info: ProcessInfo, is_healthy: bool, issues: List[str], is_stale: bool = False):
        self.process_info = process_info
        self.is_healthy = is_healthy
        self.issues = issues
        self.is_stale = is_stale  # True if this is a stale file issue
        self.checked_at = datetime.now()


@dataclass
class FileProgressState:
    """In-memory progress state for one exact recorder output."""

    last_size: int
    last_mtime: float
    no_growth_since: float
    last_identity_check_at: Optional[float] = None
    identity_unknown_warned: bool = False


class RecordingHealthMonitor:
    """
    Monitors health of persistent live recording processes

    Performs regular health checks on active processes including:
    - Process existence and responsiveness
    - Recording file validation (size, growth)
    - Automatic restart of failed/unhealthy processes
    """

    # Grace period for new sessions before Health Monitor can clean orphaned live status
    GRACE_PERIOD_SECONDS = 120  # 2 minutes

    def __init__(self, metadata_store: ProcessMetadataStore, config: Dict):
        self.metadata_store = metadata_store
        self.config = config
        self.logger = logging.getLogger('HLTH')

        # Configuration parameters
        self.health_checks_enabled = config.get('health_checks_enabled', True)
        self.file_health_checks_enabled = config.get('file_health_checks_enabled', True)
        self.health_check_interval = config.get('health_check_interval', 60)
        self.file_check_interval = config.get('file_check_interval', 60)
        self.max_restart_attempts = config.get('max_restart_attempts', 3)
        self.process_restart_delay = config.get('process_restart_delay', 10)
        self.no_growth_identity_check_after = config.get(
            'no_growth_identity_check_after', 300
        )
        self.stale_identity_check_interval = config.get(
            'stale_identity_check_interval', 300
        )
        self.same_session_hard_timeout = config.get(
            'same_session_hard_timeout', 21600
        )
        self.untracked_startup_grace_seconds = config.get(
            'untracked_startup_grace_seconds', 10
        )
        self.segment_on_reconnect = config.get('segment_on_reconnect', False)
        self.recorder_log_path = Path(
            config.get('recorder_log_path', '/tmp/tiktok_live_logs')
        )

        # Internal state
        self.is_monitoring = False
        self.monitoring_task: Optional[asyncio.Task] = None
        self.last_file_sizes: Dict[int, int] = {}  # process_id -> file_size
        self.restart_callbacks: List[callable] = []
        self.stop_callbacks: List[callable] = []
        self.untracked_stop_callbacks: List[callable] = []
        self.replacement_callbacks: List[callable] = []
        self.system_process_provider: Optional[callable] = None
        self.last_restart_times: Dict[str, datetime] = {}  # username -> last_restart_time
        self.file_progress_states: Dict[Tuple[int, int, str], FileProgressState] = {}
        self.identity_check_callback: Optional[callable] = None

        # Statistics
        self.stats = {
            'health_checks_performed': 0,
            'processes_restarted': 0,
            'unhealthy_processes_found': 0,
            'file_size_issues_detected': 0,
            'dead_processes_found': 0,
            'untracked_processes_found': 0,
            'duplicate_recorders_found': 0,
            'zombie_cleanups_performed': 0
        }

    def add_restart_callback(self, callback: callable) -> None:
        """Add callback function to be called when a process is restarted"""
        self.restart_callbacks.append(callback)

    def add_stop_callback(self, callback: callable) -> None:
        """Add callback used to stop an exact unhealthy process."""
        self.stop_callbacks.append(callback)

    def add_untracked_stop_callback(self, callback: callable) -> None:
        """Add callback used to stop an exact recorder missing from active metadata."""
        self.untracked_stop_callbacks.append(callback)

    def add_replacement_callback(self, callback: callable) -> None:
        """Add callback used to replace one exact recorder process."""
        self.replacement_callbacks.append(callback)

    def set_identity_check_callback(self, callback: callable) -> None:
        """Set callback returning current TikTok session identity for a user."""
        self.identity_check_callback = callback

    def set_system_process_provider(self, provider: callable) -> None:
        """Use the OS recorder inventory as the source of running processes."""
        self.system_process_provider = provider

    async def _request_process_stop(
        self,
        process_info: ProcessInfo,
        final_status: str,
        reset_live_status: bool,
    ) -> bool:
        if not self.stop_callbacks:
            UserContextLogger(self.logger, process_info.username).error(
                "No stop callback registered; keeping process active"
            )
            return False

        success = False
        for callback in self.stop_callbacks:
            try:
                callback_success = await callback(
                    process_info,
                    final_status,
                    reset_live_status,
                )
                success = callback_success or success
            except Exception as e:
                self.logger.error(f"Error in stop callback: {e}")
        return success

    async def start_monitoring(self) -> None:
        """Start the health monitoring loop"""
        if self.is_monitoring:
            self.logger.warning("Health monitoring is already running")
            return

        self.is_monitoring = True
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())

        if self.health_checks_enabled:
            self.logger.info(
                "Started health monitoring with file checks ENABLED "
                f"(interval: {self.health_check_interval}s, "
                f"identity_after: {self.no_growth_identity_check_after}s, "
                f"hard_timeout: {self.same_session_hard_timeout}s)"
            )
        else:
            self.logger.info("Started health monitoring with file checks DISABLED (only checking if processes are alive)")

    async def stop_monitoring(self) -> None:
        """Stop the health monitoring loop"""
        if not self.is_monitoring:
            return

        self.is_monitoring = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass

        self.logger.info("Stopped health monitoring")

    async def _monitoring_loop(self) -> None:
        """Main monitoring loop"""
        self.logger.debug("Health monitoring loop started")
        last_zombie_cleanup = 0

        try:
            while self.is_monitoring:
                start_time = time.time()

                # Perform health checks on all active processes
                await self.check_all_processes()

                # Periodic zombie cleanup (every 5 minutes)
                if start_time - last_zombie_cleanup > 300:  # 5 minutes
                    await self._cleanup_zombie_processes()
                    last_zombie_cleanup = start_time

                # Calculate sleep time (minimum 5 seconds)
                elapsed = time.time() - start_time
                sleep_time = max(5, self.health_check_interval - elapsed)

                if elapsed > 60:
                    self.logger.warning(f"Health check cycle took {elapsed:.2f} seconds")

                await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            self.logger.debug("Health monitoring loop cancelled")
            raise
        except Exception as e:
            self.logger.error(f"Error in health monitoring loop: {e}")
            raise

    async def check_all_processes(self) -> List[HealthCheckResult]:
        """
        Check health of all active processes

        Returns:
            List of health check results
        """
        system_processes = (
            self.system_process_provider()
            if self.system_process_provider is not None
            else []
        )
        active_processes = self.metadata_store.get_active_processes()
        results = []
        identity_cache: Dict[str, Dict[str, Any]] = {}
        active_identities = {
            (process.pid, process.username)
            for process in active_processes
        }

        self._warn_about_duplicate_system_processes(system_processes)

        for process in system_processes:
            if (process.pid, process.username) not in active_identities:
                await self._handle_untracked_system_process(process)

        if not active_processes:
            self.logger.debug(
                f"No active database processes to check; found {len(system_processes)} system recorders"
            )
        else:
            self.logger.debug(f"Checking health of {len(active_processes)} active processes")

        for process_info in active_processes:
            try:
                result = await self.check_process_health(process_info)
                results.append(result)

                # Handle unhealthy processes
                if not result.is_healthy:
                    await self._handle_unhealthy_process(result)
                else:
                    await self._handle_file_progress(process_info, identity_cache)

                self.stats['health_checks_performed'] += 1

            except Exception as e:
                user_logger = UserContextLogger(self.logger, process_info.username)
                user_logger.error(f"Error checking health of process (PID={process_info.pid}): {e}")

        # Perform database synchronization check
        await self._sync_database_status(system_processes)

        return results

    def _warn_about_duplicate_system_processes(
        self,
        system_processes: List[RecorderProcess],
    ) -> None:
        """Warn when one username owns more than one real recorder PID."""
        processes_by_username: Dict[str, List[RecorderProcess]] = {}
        for process in system_processes:
            processes_by_username.setdefault(process.username, []).append(process)

        for username, processes in processes_by_username.items():
            if len(processes) <= 1:
                continue

            processes = sorted(processes, key=lambda process: process.pid)
            details = ", ".join(
                f"PID={process.pid}, file={process.output_file or 'unknown'}"
                for process in processes
            )
            UserContextLogger(self.logger, username).warning(
                f"Duplicate recorders detected ({len(processes)} processes): {details}"
            )
            self.stats['duplicate_recorders_found'] += len(processes) - 1

    async def _handle_untracked_system_process(self, process: RecorderProcess) -> None:
        """Keep an OS recorder visible even after its active database row disappears."""
        process_age = max(0.0, time.time() - process.create_time)
        if process_age < self.untracked_startup_grace_seconds:
            UserContextLogger(self.logger, process.username).debug(
                f"Recorder PID={process.pid} is still inside startup grace period; "
                "waiting for metadata registration"
            )
            return

        self.stats['untracked_processes_found'] += 1
        user_logger = UserContextLogger(self.logger, process.username)
        user_logger.warning(
            f"Untracked recorder is still running: PID={process.pid}, "
            f"file={process.output_file or 'unknown'}"
        )

        live_check_callback = getattr(self, 'live_check_callback', None)
        if live_check_callback is None:
            user_logger.warning("Cannot verify live status; keeping untracked recorder")
            return

        try:
            is_live = await live_check_callback(process.username)
        except Exception as e:
            user_logger.error(f"Could not verify live status: {e}; keeping recorder")
            return

        if is_live is not False:
            state = "live" if is_live is True else "unknown"
            user_logger.info(f"Selenium status is {state}; keeping untracked recorder")
            return

        if not self.untracked_stop_callbacks:
            user_logger.error("User is offline but no untracked-process stop callback is registered")
            return

        for callback in self.untracked_stop_callbacks:
            try:
                if await callback(process):
                    user_logger.info(f"Stopped offline untracked recorder PID={process.pid}")
                    return
            except Exception as e:
                user_logger.error(f"Error stopping untracked recorder PID={process.pid}: {e}")

        user_logger.error(f"Failed to stop offline untracked recorder PID={process.pid}")

    async def check_process_health(self, process_info: ProcessInfo) -> HealthCheckResult:
        """
        Check health of a single process

        Args:
            process_info: Process information to check

        Returns:
            HealthCheckResult with health status and any issues found
        """
        issues = []

        # If health checks are disabled, skip all health monitoring completely
        if not self.health_checks_enabled:
            # Return healthy result without any database updates or logging
            return HealthCheckResult(process_info, True, [])

        # Normal health check flow when enabled
        # 1. Always check if process is still running (this is essential)
        process_running = self.is_process_running(process_info.pid)
        if not process_running:
            if self._recorder_log_contains(process_info, "[NO_STREAM_DATA]"):
                issues.append("Recorder exited without producing stream data")
            else:
                issues.append(f"Process PID {process_info.pid} is not running")
            self.stats['dead_processes_found'] += 1

        # 2. Check recording file health only if file health checks are enabled
        if (
            process_running
            and self.file_health_checks_enabled
            and process_info.recording_file_path
        ):
            file_issues = await self._check_file_health(process_info)
            issues.extend(file_issues)

        # 3. Check restart count limits
        if process_info.restart_count >= self.max_restart_attempts:
            issues.append(f"Process has exceeded max restart attempts ({process_info.restart_count}/{self.max_restart_attempts})")

        is_healthy = len(issues) == 0

        if not is_healthy:
            self.stats['unhealthy_processes_found'] += 1
            user_logger = UserContextLogger(self.logger, process_info.username)

            # Check if process is already marked as inactive in database
            if not process_info.is_active:
                # Process is already marked as inactive - this is normal cleanup, log as DEBUG
                user_logger.debug(f"Process (PID={process_info.pid}) cleanup detected: {', '.join(issues)}")
            elif len(issues) == 1 and "is not running" in issues[0]:
                # Process just died but still marked as active - log as DEBUG (normal completion)
                user_logger.debug(f"Process (PID={process_info.pid}) completed normally: {', '.join(issues)}")
            else:
                # Real health issues - log as WARNING
                user_logger.warning(f"Process (PID={process_info.pid}) is unhealthy: {', '.join(issues)}")

        # Update health status in database
        status = 'healthy' if is_healthy else 'unhealthy'
        file_size = 0
        if process_info.recording_file_path:
            progress = self._recording_file_progress(process_info.recording_file_path)
            if progress is not None:
                file_size = progress[0]

        self.metadata_store.update_health_status(process_info.id, status, file_size)

        return HealthCheckResult(process_info, is_healthy, issues)

    def _recorder_log_contains(
        self,
        process_info: ProcessInfo,
        marker: str,
    ) -> bool:
        """Check the exact recorder's stderr logs for a lifecycle marker."""
        expected_prefix = f"live_{process_info.username}_{process_info.pid}_"
        try:
            candidates = self.recorder_log_path.glob(
                f"live_*_{process_info.pid}_*.err"
            )
            for log_path in candidates:
                if not log_path.name.startswith(expected_prefix):
                    continue
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as log_file:
                        if any(marker in line for line in log_file):
                            return True
                except OSError:
                    continue
        except OSError:
            return False
        return False

    def is_process_running(self, pid: int) -> bool:
        """
        Check if a process with given PID is running and not a zombie

        Args:
            pid: Process ID to check

        Returns:
            True if process is running and healthy, False if dead or zombie
        """
        try:
            # Use psutil for cross-platform process checking
            if not psutil.pid_exists(pid):
                return False

            # Check if it's a zombie process
            try:
                process = psutil.Process(pid)
                if process.status() == psutil.STATUS_ZOMBIE:
                    self.logger.debug(f"Process {pid} is a zombie, treating as dead")
                    return False
            except psutil.NoSuchProcess:
                return False
            except psutil.AccessDenied:
                # If we can't access process info, check via /proc
                if self._is_zombie_process(pid):
                    self.logger.debug(f"Process {pid} is a zombie (via /proc), treating as dead")
                    return False

            return True
        except Exception as e:
            self.logger.error(f"Error checking if PID {pid} exists: {e}")
            return False

    def _is_zombie_process(self, pid: int) -> bool:
        """
        Check if a process is a zombie using /proc filesystem

        Args:
            pid: Process ID to check

        Returns:
            True if process is a zombie, False otherwise
        """
        try:
            with open(f"/proc/{pid}/stat", "r") as f:
                stat_data = f.read().strip().split()
                # Third field is the state: Z means zombie
                if len(stat_data) > 2 and stat_data[2] == 'Z':
                    return True
        except (FileNotFoundError, PermissionError, IndexError):
            # If we can't read proc stat, assume not zombie
            pass
        return False

    async def _check_file_health(self, process_info: ProcessInfo) -> List[str]:
        """
        Check health of the recording file

        Args:
            process_info: Process information containing file path

        Returns:
            List of issues found with the file
        """
        issues = []
        file_path = process_info.recording_file_path

        if not file_path:
            return issues

        try:
            progress = self._recording_file_progress(file_path)
            if progress is None:
                # Check if process is still running - if not, don't treat missing file as an error
                if not self.is_process_running(process_info.pid):
                    user_logger = UserContextLogger(self.logger, process_info.username)
                    user_logger.debug(f"Recording file missing but process (PID={process_info.pid}) is dead - this is expected")
                    issues.append(f"Process PID {process_info.pid} is not running (dead process)")
                    return issues
                else:
                    process_age = datetime.now() - process_info.started_at
                    if process_age.total_seconds() > self.no_growth_identity_check_after:
                        expected_path = (
                            stream_part_path(file_path, 1)
                            if self.segment_on_reconnect
                            else file_path
                        )
                        issues.append(
                            "Recorder produced no stream data within "
                            f"{int(self.no_growth_identity_check_after)} seconds: "
                            f"{expected_path}"
                        )
                    return issues

            # Get current file size
            current_size = progress[0]
            process_id = process_info.id

            # Check file growth (compare with previous size)
            if process_id in self.last_file_sizes:
                previous_size = self.last_file_sizes[process_id]

                # If file size decreased significantly, this might indicate a problem
                if current_size < previous_size * 0.9:  # Allow for some fluctuation
                    issues.append(f"File size decreased significantly: {previous_size} -> {current_size} bytes")
                    self.stats['file_size_issues_detected'] += 1

            # Update last known file size
            self.last_file_sizes[process_id] = current_size

        except Exception as e:
            issues.append(f"Error checking file health: {e}")
            self.logger.error(f"Error checking file health for {file_path}: {e}")

        return issues

    def _recording_file_progress(self, file_path: str) -> Optional[Tuple[int, float]]:
        """Return combined size and latest mtime for the exact reconnect parts."""
        paths = (
            existing_stream_parts(file_path)
            if self.segment_on_reconnect
            else (file_path,)
        )
        total_size = 0
        latest_mtime = 0.0
        found = False
        for path in paths:
            try:
                stat = os.stat(path)
            except OSError:
                continue
            found = True
            total_size += stat.st_size
            latest_mtime = max(latest_mtime, stat.st_mtime)
        if not found:
            return None
        return total_size, latest_mtime

    @staticmethod
    def _normalize_started_at(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return int(value.timestamp())
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
        try:
            return int(datetime.fromisoformat(str(value)).timestamp())
        except (TypeError, ValueError):
            return None

    @classmethod
    def _identity_tuple(cls, identity: Optional[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
        if not identity:
            return None
        stream_id = identity.get('tiktok_stream_id', identity.get('stream_id'))
        started_at = identity.get('tiktok_started_at', identity.get('start_time'))
        normalized_started_at = cls._normalize_started_at(started_at)
        if stream_id in (None, "") or normalized_started_at is None:
            return None
        return str(stream_id), normalized_started_at

    def _has_other_system_recorder(self, process_info: ProcessInfo) -> bool:
        if self.system_process_provider is None:
            return False
        return any(
            process.username == process_info.username and process.pid != process_info.pid
            for process in self.system_process_provider()
        )

    def _has_process_with_identity(
        self,
        process_info: ProcessInfo,
        identity: Dict[str, Any],
    ) -> bool:
        expected = self._identity_tuple(identity)
        if expected is None:
            return False
        for candidate in self.metadata_store.get_active_processes():
            if candidate.id == process_info.id or candidate.username != process_info.username:
                continue
            if not self.is_process_running(candidate.pid):
                continue
            stored = self.metadata_store.get_process_live_identity(candidate.id)
            if self._identity_tuple(stored) == expected:
                return True
        return False

    async def _request_replacement(
        self,
        process_info: ProcessInfo,
        reason: str,
        final_status: str,
        start_new: bool,
    ) -> bool:
        if not self.replacement_callbacks:
            UserContextLogger(self.logger, process_info.username).error(
                "No exact replacement callback registered; keeping process"
            )
            return False

        success = False
        for callback in self.replacement_callbacks:
            try:
                callback_success = await callback(
                    process_info,
                    reason,
                    final_status,
                    start_new,
                )
                success = callback_success or success
            except Exception as error:
                self.logger.error(f"Error in exact replacement callback: {error}")
        return success

    async def _get_current_identity(
        self,
        username: str,
        identity_cache: Dict[str, Dict[str, Any]],
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        if use_cache and username in identity_cache:
            return identity_cache[username]
        if self.identity_check_callback is None:
            result = {'state': 'unknown'}
        else:
            try:
                result = await self.identity_check_callback(username)
            except Exception as error:
                UserContextLogger(self.logger, username).error(
                    f"Identity check failed: {error}"
                )
                result = {'state': 'unknown'}
        if not isinstance(result, dict):
            result = {'state': 'unknown'}
        identity_cache[username] = result
        return result

    async def _handle_file_progress(
        self,
        process_info: ProcessInfo,
        identity_cache: Dict[str, Dict[str, Any]],
    ) -> None:
        file_path = process_info.recording_file_path
        if not file_path:
            return
        if not self.is_process_running(process_info.pid):
            return

        progress = self._recording_file_progress(file_path)
        if progress is None:
            return
        current_size, current_mtime = progress

        now = time.time()
        key = (process_info.id, process_info.pid, file_path)
        state = self.file_progress_states.get(key)
        if state is None:
            no_growth_since = max(process_info.started_at.timestamp(), current_mtime)
            state = FileProgressState(current_size, current_mtime, no_growth_since)
            self.file_progress_states[key] = state
        elif current_size > state.last_size or current_mtime > state.last_mtime:
            state.last_size = current_size
            state.last_mtime = current_mtime
            state.no_growth_since = now
            state.last_identity_check_at = None
            state.identity_unknown_warned = False
            return
        else:
            state.last_size = current_size
            state.last_mtime = current_mtime

        no_growth_seconds = max(0, now - state.no_growth_since)
        user_logger = UserContextLogger(self.logger, process_info.username)

        if no_growth_seconds >= self.same_session_hard_timeout:
            start_new = not self._has_other_system_recorder(process_info)
            user_logger.warning(
                f"[SAME_SESSION_TIMEOUT] PID={process_info.pid} file={file_path} "
                f"no_growth={int(no_growth_seconds)}s"
            )
            await self._request_replacement(
                process_info,
                f"no file growth for {int(no_growth_seconds)} seconds",
                'stopped_same_session_timeout',
                start_new,
            )
            return

        if no_growth_seconds < self.no_growth_identity_check_after:
            return
        if (
            state.last_identity_check_at is not None
            and now - state.last_identity_check_at < self.stale_identity_check_interval
        ):
            return

        state.last_identity_check_at = now
        user_logger.info(
            f"[NO_GROWTH] PID={process_info.pid} file={file_path} "
            f"size={current_size} no_growth={int(no_growth_seconds)}s"
        )

        current = await self._get_current_identity(process_info.username, identity_cache)
        current_state = str(current.get('state', 'unknown')).lower()

        def log_unknown(message: str) -> None:
            if state.identity_unknown_warned:
                user_logger.debug(message)
            else:
                user_logger.warning(message)
                state.identity_unknown_warned = True

        if current_state == 'offline':
            user_logger.info(
                f"[IDENTITY_CHECK] PID={process_info.pid} decision=OFFLINE"
            )
            await self._request_process_stop(
                process_info,
                'stopped_user_offline',
                reset_live_status=True,
            )
            return
        if current_state != 'live':
            log_unknown(
                f"[IDENTITY_CHECK] PID={process_info.pid} decision=UNKNOWN; keeping process"
            )
            return

        stored = self.metadata_store.get_process_live_identity(process_info.id)
        stored_tuple = self._identity_tuple(stored)
        current_tuple = self._identity_tuple(current)
        if stored_tuple is None or current_tuple is None:
            log_unknown(
                f"[IDENTITY_CHECK] PID={process_info.pid} decision=UNKNOWN "
                "because complete linked identity is unavailable; keeping process"
            )
            return
        if stored_tuple == current_tuple:
            state.identity_unknown_warned = False
            user_logger.info(
                f"[IDENTITY_CHECK] PID={process_info.pid} decision=SAME "
                f"stream_id={current_tuple[0]} started_at={current_tuple[1]}"
            )
            return

        confirmation = await self._get_current_identity(
            process_info.username,
            identity_cache,
            use_cache=False,
        )
        if (
            str(confirmation.get('state', 'unknown')).lower() != 'live'
            or self._identity_tuple(confirmation) != current_tuple
        ):
            log_unknown(
                f"[IDENTITY_CHECK] PID={process_info.pid} decision=UNKNOWN "
                "because new identity was not confirmed; keeping process"
            )
            return

        already_recording_new_session = self._has_process_with_identity(
            process_info,
            confirmation,
        )
        user_logger.info(
            f"[NEW_SESSION] PID={process_info.pid} old={stored_tuple} "
            f"new={current_tuple} replacement_exists={already_recording_new_session}"
        )
        await self._request_replacement(
            process_info,
            "TikTok live identity changed",
            'stopped_replaced_session',
            not already_recording_new_session,
        )

    async def _handle_unhealthy_process(self, health_result: HealthCheckResult) -> None:
        """
        Handle an unhealthy process by attempting restart

        Args:
            health_result: Health check result for the unhealthy process
        """
        process_info = health_result.process_info

        # Check if we should attempt restart
        if process_info.restart_count >= self.max_restart_attempts:
            user_logger = UserContextLogger(self.logger, process_info.username)
            user_logger.error("Process has exceeded max restart attempts, marking as stopped")
            await self._request_process_stop(
                process_info,
                'stopped_max_retries',
                reset_live_status=True,
            )
            return

        exact_failure = None
        if any("Recorder exited without producing stream data" in issue for issue in health_result.issues):
            exact_failure = (
                "recorder produced no stream data",
                'stopped_no_stream_data',
            )
        elif any("is not running" in issue or "dead process" in issue for issue in health_result.issues):
            exact_failure = ("recorder process is dead", 'stopped_dead')
        elif any("Recorder produced no stream data" in issue for issue in health_result.issues):
            exact_failure = (
                "recorder produced no stream data",
                'stopped_missing_file',
            )
        elif any("File size decreased significantly" in issue for issue in health_result.issues):
            exact_failure = ("recording file size decreased", 'stopped_file_truncated')

        if exact_failure is not None:
            reason, final_status = exact_failure
            user_logger = UserContextLogger(self.logger, process_info.username)
            is_live = None
            if getattr(self, 'live_check_callback', None) is not None:
                try:
                    is_live = await self.live_check_callback(process_info.username)
                except Exception as error:
                    user_logger.error(
                        f"Could not verify live status for exact replacement: {error}"
                    )

            if is_live is False:
                user_logger.info(f"{reason}; user is offline, stopping exact process")
                await self._request_process_stop(
                    process_info,
                    'stopped_user_offline',
                    reset_live_status=True,
                )
                return

            start_new = not self._has_other_system_recorder(process_info)
            user_logger.info(
                f"{reason}; replacing exact PID={process_info.pid}, start_new={start_new}"
            )
            await self._request_replacement(
                process_info,
                reason,
                final_status,
                start_new,
            )
            return

        # FIRST: Always check database is_live status before restart
        user_logger = UserContextLogger(self.logger, process_info.username)
        is_user_live_in_db = self.metadata_store.is_user_live(process_info.username)

        if not is_user_live_in_db:
            user_logger.info("User is not live in database, stopping process instead of restart")
            await self._request_process_stop(
                process_info,
                'stopped_user_offline',
                reset_live_status=False,
            )
            return

        # SECOND: If user is live in DB, optionally do external live check via callback
        if hasattr(self, 'live_check_callback') and self.live_check_callback:
            try:
                is_live = await self.live_check_callback(process_info.username)
                if is_live is False:
                    user_logger.info("User is no longer live (external check), stopping process")
                    await self._request_process_stop(
                        process_info,
                        'stopped_user_offline',
                        reset_live_status=True,
                    )
                    return
            except Exception as e:
                user_logger.error(f"Error checking live status: {e}")
                # Continue with restart attempt if we can't verify live status
        try:
            user_logger = UserContextLogger(self.logger, process_info.username)

            # Check restart cooldown (minimum 2 minutes between restarts for same user)
            username = process_info.username
            now = datetime.now()
            if username in self.last_restart_times:
                time_since_last_restart = now - self.last_restart_times[username]
                if time_since_last_restart.total_seconds() < 120:  # 2 minutes cooldown
                    seconds_left = 120 - time_since_last_restart.total_seconds()
                    user_logger.info(f"Restart cooldown active - {seconds_left:.0f} seconds remaining")
                    return

            user_logger.info(f"Attempting to restart unhealthy process (PID={process_info.pid})")
            self.last_restart_times[username] = now

            # Mark as restarting
            # Get current file size before marking as restarting
            file_size = 0
            if process_info.recording_file_path:
                progress = self._recording_file_progress(process_info.recording_file_path)
                if progress is not None:
                    file_size = progress[0]
            self.metadata_store.update_health_status(process_info.id, 'restarting', file_size)

            # Call restart callbacks (these should handle the actual restart)
            for callback in self.restart_callbacks:
                try:
                    await callback(process_info, health_result.issues)
                except Exception as e:
                    self.logger.error(f"Error in restart callback: {e}")

            # Increment restart count
            new_count = self.metadata_store.increment_restart_count(process_info.id)
            self.stats['processes_restarted'] += 1

            user_logger = UserContextLogger(self.logger, process_info.username)
            user_logger.info(f"Restart initiated (restart count: {new_count})")

            # Add exponential backoff delay before next health check
            backoff_delay = self._calculate_backoff_delay(new_count)
            user_logger.debug(f"Waiting {backoff_delay}s before next health check")
            await asyncio.sleep(backoff_delay)

        except Exception as e:
            user_logger = UserContextLogger(self.logger, process_info.username)
            user_logger.error(f"Failed to handle unhealthy process: {e}")
            # Mark as stopped if restart handling fails
            # Get final file size before marking as stopped
            file_size = 0
            if process_info.recording_file_path:
                progress = self._recording_file_progress(process_info.recording_file_path)
                if progress is not None:
                    file_size = progress[0]
            self.metadata_store.update_health_status(process_info.id, 'stopped', file_size)

    async def force_check_process(self, username: str) -> Optional[HealthCheckResult]:
        """
        Force an immediate health check for a specific user

        Args:
            username: Username to check

        Returns:
            HealthCheckResult if process found, None otherwise
        """
        process_info = self.metadata_store.get_process_by_username(username)
        if not process_info:
            user_logger = UserContextLogger(self.logger, username)
            user_logger.warning("No active process found")
            return None

        result = await self.check_process_health(process_info)

        if not result.is_healthy:
            await self._handle_unhealthy_process(result)

        return result

    def get_statistics(self) -> Dict[str, any]:
        """
        Get health monitoring statistics

        Returns:
            Dictionary with monitoring statistics
        """
        stats = self.stats.copy()
        stats['is_monitoring'] = self.is_monitoring
        stats['tracked_file_sizes'] = len(self.last_file_sizes)

        return stats

    def set_live_check_callback(self, callback: callable) -> None:
        """
        Set callback function to check if user is live

        Args:
            callback: Async function that takes username and returns bool
        """
        self.live_check_callback = callback

    def _get_file_size(self, file_path: Optional[str]) -> int:
        """
        Get file size safely

        Args:
            file_path: Path to file

        Returns:
            File size in bytes or 0 if error
        """
        if not file_path:
            return 0
        try:
            if os.path.exists(file_path):
                return os.path.getsize(file_path)
        except OSError:
            pass
        return 0

    def _calculate_backoff_delay(self, restart_count: int) -> int:
        """
        Calculate exponential backoff delay based on restart count

        Args:
            restart_count: Number of restart attempts

        Returns:
            Delay in seconds
        """
        # Base delay with exponential increase: 10s, 30s, 60s
        base_delay = self.process_restart_delay
        if restart_count <= 1:
            return base_delay
        elif restart_count == 2:
            return base_delay * 3
        else:
            return base_delay * 6

    def cleanup_tracking_data(self, active_process_ids: Set[int]) -> None:
        """
        Clean up tracking data for processes that no longer exist

        Args:
            active_process_ids: Set of currently active process IDs
        """
        # Clean up last file sizes
        to_remove = []
        for process_id in self.last_file_sizes:
            if process_id not in active_process_ids:
                to_remove.append(process_id)

        for process_id in to_remove:
            del self.last_file_sizes[process_id]
            self.logger.debug(f"Cleaned up file size tracking for process {process_id}")

        stale_progress_keys = [
            key for key in self.file_progress_states if key[0] not in active_process_ids
        ]
        for key in stale_progress_keys:
            del self.file_progress_states[key]

    async def _cleanup_zombie_processes(self) -> None:
        """
        Periodically scan for and clean up zombie processes from our process tracking
        """
        try:
            import subprocess

            # Get all zombie processes in the system
            result = subprocess.run(
                ['ps', 'aux'],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                self.logger.debug("Could not run ps command for zombie cleanup")
                return

            zombie_pids = []
            for line in result.stdout.split('\n'):
                if ' Z ' in line and 'python3' in line:  # Zombie Python processes
                    parts = line.split()
                    if len(parts) > 1:
                        try:
                            pid = int(parts[1])
                            zombie_pids.append(pid)
                        except ValueError:
                            continue

            if not zombie_pids:
                self.logger.debug("No zombie processes found during periodic cleanup")
                return

            self.logger.info(f"Found {len(zombie_pids)} zombie Python processes, attempting cleanup")

            # Try to reap zombie children
            reaped_count = 0
            try:
                while True:
                    try:
                        pid, status = os.waitpid(-1, os.WNOHANG)
                        if pid == 0:  # No more children to reap
                            break
                        if pid in zombie_pids:
                            self.logger.info(f"Reaped zombie process PID={pid}, exit_status={status}")
                            reaped_count += 1
                        else:
                            self.logger.debug(f"Reaped child process PID={pid}, exit_status={status}")
                    except ChildProcessError:
                        # No more child processes
                        break
            except Exception as e:
                self.logger.debug(f"Error during zombie reaping: {e}")

            if reaped_count > 0:
                self.logger.info(f"Successfully reaped {reaped_count} zombie processes")

            # Update statistics
            self.stats['zombie_cleanups_performed'] = self.stats.get('zombie_cleanups_performed', 0) + 1

        except Exception as e:
            self.logger.error(f"Error during periodic zombie cleanup: {e}")

    async def validate_recording_file(self, file_path: str, expected_min_size: int = 1024) -> Dict[str, any]:
        """
        Validate a recording file independently

        Args:
            file_path: Path to the recording file
            expected_min_size: Expected minimum file size in bytes

        Returns:
            Dictionary with validation results
        """
        result = {
            'exists': False,
            'size_bytes': 0,
            'is_healthy': False,
            'issues': []
        }

        try:
            if not os.path.exists(file_path):
                result['issues'].append("File does not exist")
                return result

            result['exists'] = True
            result['size_bytes'] = os.path.getsize(file_path)

            if result['size_bytes'] == 0:
                result['issues'].append("File is 0 bytes")
            elif result['size_bytes'] < expected_min_size:
                result['issues'].append(f"File size {result['size_bytes']} is below expected minimum {expected_min_size}")

            # Check file modification time
            mtime = os.path.getmtime(file_path)
            age_seconds = time.time() - mtime

            if age_seconds > 300:  # File hasn't been modified in 5 minutes
                result['issues'].append(f"File hasn't been modified in {age_seconds:.0f} seconds")

            result['is_healthy'] = len(result['issues']) == 0

        except Exception as e:
            result['issues'].append(f"Error validating file: {e}")

        return result

    async def _sync_database_status(
        self,
        system_processes: Optional[List[RecorderProcess]] = None,
    ) -> None:
        """
        Synchronize database status between users and live_processes tables

        This method ensures that:
        1. Users with active recording processes are marked as live
        2. Users without active recording processes are marked as not live (if appropriate)
        """
        try:
            # Get all active processes
            active_processes = self.metadata_store.get_active_processes()
            active_usernames = {p.username for p in active_processes}
            active_usernames.update(
                process.username for process in (system_processes or [])
            )

            # Import sqlite3 for direct database access
            import sqlite3

            # Get database path from metadata store
            db_path = resolve_path(self.metadata_store.db_path)
            conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)

            # Get users currently marked as live (is_live = 1) or starting (is_live = 2)
            live_users_query = "SELECT username FROM users WHERE is_live IN (1, 2)"
            live_usernames = {row[0] for row in conn.execute(live_users_query).fetchall()}

            sync_actions = []

            # Check for users with active processes but not marked as live
            missing_live_status = active_usernames - live_usernames
            for username in missing_live_status:
                try:
                    conn.execute("UPDATE users SET is_live = 1 WHERE username = ?", (username,))
                    sync_actions.append(f"Marked {username} as live (has active recording)")
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.info("Sync: Marked as live (has active recording)")
                except Exception as e:
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.error(f"Error marking as live: {e}")

            # Check for users with active processes but marked as starting (is_live=2) - promote to live (is_live=1)
            starting_users_query = "SELECT username FROM users WHERE is_live = 2"
            starting_usernames = {row[0] for row in conn.execute(starting_users_query).fetchall()}
            starting_with_processes = starting_usernames & active_usernames
            for username in starting_with_processes:
                try:
                    conn.execute("UPDATE users SET is_live = 1 WHERE username = ?", (username,))
                    sync_actions.append(f"Promoted {username} from starting to live (has active recording)")
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.info("Sync: Promoted from starting to live (has active recording)")
                except Exception as e:
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.error(f"Error promoting to live: {e}")

            # Check for failed processes with WAF errors that should be removed from database
            self._cleanup_failed_waf_processes(active_processes, sync_actions)

            # Check for users marked as live but no active processes
            # NOTE: We need to be careful here - user might have just gone offline
            # So we only clean up if there's no recent live session or if it's very old
            orphaned_live_users = live_usernames - active_usernames

            for username in orphaned_live_users:
                try:
                    # Check if there's a recent live session that ended
                    recent_session_query = """
                        SELECT l.ended_at, l.started_at
                        FROM lives l
                        JOIN users u ON l.user_id = u.id
                        WHERE u.username = ?
                        ORDER BY l.started_at DESC
                        LIMIT 1
                    """
                    session_result = conn.execute(recent_session_query, (username,)).fetchone()

                    should_cleanup = False

                    if session_result:
                        ended_at, started_at = session_result
                        if ended_at is None:
                            # Active session exists but no recording process - check grace period

                            # Parse started_at timestamp (assuming it's a Unix timestamp or ISO string)
                            if isinstance(started_at, (int, float)):
                                session_start_time = started_at
                            else:
                                # Try parsing as ISO string, fallback to Unix timestamp
                                try:
                                    session_start_time = datetime.fromisoformat(str(started_at).replace('Z', '+00:00')).timestamp()
                                except:
                                    session_start_time = float(started_at) if started_at else 0

                            current_time = time.time()
                            session_age = current_time - session_start_time

                            # Grace period for process to start and register
                            if session_age > self.GRACE_PERIOD_SECONDS:
                                # Session is old enough, safe to cleanup
                                should_cleanup = True
                                sync_actions.append(f"Cleaned orphaned live status for {username} (active session but no recording, age: {session_age:.1f}s)")
                            else:
                                # Session is too new, give process time to register
                                self.logger.debug(f"[{username}] Skipping cleanup of recent session (age: {session_age:.1f}s < {self.GRACE_PERIOD_SECONDS}s)")
                        else:
                            # Session has ended - user should not be marked as live
                            should_cleanup = True
                            sync_actions.append(f"Cleaned orphaned live status for {username} (session ended)")
                    else:
                        # No live sessions at all - definitely should not be marked as live
                        should_cleanup = True
                        sync_actions.append(f"Cleaned orphaned live status for {username} (no live sessions)")

                    if should_cleanup:
                        ended_at_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        conn.execute("""
                            UPDATE lives
                            SET ended_at = ?
                            WHERE user_id = (SELECT id FROM users WHERE username = ?)
                              AND ended_at IS NULL
                        """, (ended_at_str, username))
                        conn.execute("UPDATE users SET is_live = 0 WHERE username = ?", (username,))
                        user_logger = UserContextLogger(self.logger, username)
                        user_logger.info("Sync: Cleaned orphaned live status")

                except Exception as e:
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.error(f"Error cleaning live status: {e}")

            # Commit all changes
            conn.commit()
            conn.close()

            # Log summary of sync actions
            if sync_actions:
                self.logger.info(f"Database sync completed: {len(sync_actions)} actions taken")
                for action in sync_actions:
                    self.logger.debug(f"Sync action: {action}")
            else:
                self.logger.debug("Database sync completed: no actions needed")

        except Exception as e:
            self.logger.error(f"Error during database synchronization: {e}")

    def _cleanup_failed_waf_processes(self, active_processes: List[ProcessInfo], sync_actions: List[str]) -> None:
        """
        Clean up processes that are registered in database but have WAF errors and are not actually recording.

        Args:
            active_processes: List of active processes from database
            sync_actions: List to append cleanup actions to
        """
        try:
            for process in active_processes:
                user_logger = UserContextLogger(self.logger, process.username)

                # Check if process is actually alive
                if not self._is_process_alive(process.pid):
                    # Process is dead, check for WAF error in its logs
                    matching_logs = list(
                        self.recorder_log_path.glob(
                            f"live_{process.username}_{process.pid}_*.err"
                        )
                    )
                    log_file = max(
                        matching_logs,
                        key=lambda path: path.stat().st_mtime,
                        default=None,
                    )

                    waf_detected = False
                    if log_file is not None and log_file.exists():
                        try:
                            with open(log_file, 'r') as f:
                                stderr_content = f.read()
                            if stderr_content and ("WAF" in stderr_content or "blocked" in stderr_content.lower()):
                                waf_detected = True
                                user_logger.warning(f"Dead process {process.pid} had WAF error: {stderr_content[:100]}")
                        except Exception as e:
                            user_logger.debug(f"Could not read stderr log: {e}")

                    if waf_detected:
                        # Remove the failed process from database
                        if self.metadata_store.remove_process(process.id, process.username):
                            sync_actions.append(f"Removed failed WAF process for {process.username} (PID={process.pid})")
                            user_logger.info(f"Cleaned up failed WAF process: PID={process.pid}, DB_ID={process.id}")

        except Exception as e:
            self.logger.error(f"Error during WAF process cleanup: {e}")

    def _is_process_alive(self, pid: int) -> bool:
        """
        Check if a process is still alive.

        Args:
            pid: Process ID to check

        Returns:
            True if process is alive, False otherwise
        """
        try:
            import os
            # Send signal 0 to check if process exists
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
