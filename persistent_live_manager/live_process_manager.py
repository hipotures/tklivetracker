"""
Live Process Manager

Manages the lifecycle of persistent live recording processes.
Handles starting, stopping, and restarting live recording processes in detached mode.
"""

import asyncio
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import UserContextLogger for username-prefixed logging
from utils.user_context_logger import UserContextLogger
from utils.username import normalize_tiktok_username
from recorder.utils.recording_metadata import validate_metadata_path

from .process_metadata_store import ProcessMetadataStore, ProcessInfo
from .process_inventory import RecorderProcess, list_recorder_processes


class ProcessStartResult:
    """Result of starting a process"""

    def __init__(self, success: bool, process_id: Optional[int], pid: Optional[int],
                 error: Optional[str], started_new: bool = False):
        self.success = success
        self.process_id = process_id  # Database process ID
        self.pid = pid  # System process ID
        self.error = error
        self.started_new = started_new


@dataclass(frozen=True)
class OrphanProcessCandidate:
    """A recorder that can be stopped without touching the canonical process."""

    process: RecorderProcess
    reason: str
    process_id: Optional[int] = None
    kept_pid: Optional[int] = None
    kept_fingerprint: Optional[Tuple[int, float, Tuple[str, ...]]] = None


class LiveProcessManager:
    """
    Manages lifecycle of persistent live recording processes

    Handles creation, monitoring, and termination of detached recording processes
    that can continue running independently of the supervisor.
    """

    def __init__(self, metadata_store: ProcessMetadataStore, config: Dict):
        self.metadata_store = metadata_store
        self.config = config
        self.logger = logging.getLogger('PROC')

        # Configuration parameters
        self.detached_process_mode = config.get('detached_process_mode', True)
        self.process_restart_delay = config.get('process_restart_delay', 10)
        self.max_restart_attempts = config.get('max_restart_attempts', 3)
        self.no_cookies = config.get('no_cookies', False)
        self.cookie_file_path = config.get('cookie_file_path', None)
        self.config_path = config.get('config_path')
        self.segment_on_reconnect = config.get('segment_on_reconnect', False)
        self.metadata_enabled = config.get('metadata_enabled', False)
        self.no_stream_data_retry_cooldown = float(
            config.get('no_stream_data_retry_cooldown', 300)
        )
        self.no_stream_data_cooldowns: Dict[str, float] = {}
        self.metadata_path = config.get('metadata_path')
        self.compressed_output_path = config.get('compressed_output_path')
        if self.metadata_enabled and not self.segment_on_reconnect:
            raise ValueError("metadata_enabled requires segment_on_reconnect")
        if self.metadata_enabled and not self.metadata_path:
            raise ValueError(
                "metadata_path is required when metadata_enabled is true"
            )
        if self.metadata_enabled and not self.compressed_output_path:
            raise ValueError(
                "compressed_output_path is required when metadata_enabled is true"
            )
        if self.metadata_enabled:
            try:
                self.metadata_path = str(validate_metadata_path(self.metadata_path))
            except OSError as error:
                raise ValueError(
                    f"metadata_path is not writable: {self.metadata_path}: {error}"
                ) from error

        # Paths
        self.project_root = project_root
        self.recordings_path = Path(config.get('recordings_path', './recordings'))
        self.log_path = config.get('log_path', './logs/aplikacja.log')
        self.recorder_log_path = Path(
            config.get('recorder_log_path', '/tmp/tiktok_live_logs')
        )

        # Process tracking
        self.startup_callbacks: List[callable] = []
        self.shutdown_callbacks: List[callable] = []
        self.room_id_unavailable_callbacks: List[callable] = []
        self.startup_metadata_tasks: Set[asyncio.Task] = set()
        self._start_locks: Dict[str, asyncio.Lock] = {}

        # Statistics
        self.stats = {
            'processes_started': 0,
            'processes_stopped': 0,
            'processes_restarted': 0,
            'startup_failures': 0
        }

    def add_startup_callback(self, callback: callable) -> None:
        """Add callback function to be called when a process starts"""
        self.startup_callbacks.append(callback)

    def add_shutdown_callback(self, callback: callable) -> None:
        """Add callback function to be called when a process stops"""
        self.shutdown_callbacks.append(callback)

    def add_room_id_unavailable_callback(self, callback: callable) -> None:
        """Add callback called when a live user has no resolvable RoomID."""
        self.room_id_unavailable_callbacks.append(callback)

    def _ensure_recorder_log_directory(self) -> Path:
        self.recorder_log_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            self.recorder_log_path.chmod(0o700)
        return self.recorder_log_path

    def _notify_room_id_unavailable(self, username: str, error: str) -> None:
        for callback in self.room_id_unavailable_callbacks:
            try:
                callback(username, error)
            except Exception as callback_error:
                self.logger.error(
                    f"RoomID unavailable callback failed for {username}: {callback_error}"
                )

    @staticmethod
    def _is_room_id_unavailable_error(stderr_text: str) -> bool:
        """Detect recorder exits caused by TikTok not exposing a room ID."""
        if not stderr_text:
            return False

        stderr_lower = stderr_text.lower()
        return (
            "could not find room_id" in stderr_lower
            or "error extracting roomid" in stderr_lower
            or "exiting with code 10" in stderr_lower
        )

    @staticmethod
    def _room_id_unavailable_message() -> str:
        return "ROOM_ID_UNAVAILABLE: Recorder could not resolve RoomID for live user"

    @staticmethod
    def _is_no_stream_data_exit(returncode: Optional[int], stderr_text: str) -> bool:
        return returncode == 12 or "[NO_STREAM_DATA]" in stderr_text

    @staticmethod
    def _no_stream_data_message() -> str:
        return (
            "NO_STREAM_DATA: TikTok did not expose a stream URL; "
            "will retry on the next live detection"
        )

    def _no_stream_data_cooldown_remaining(self, username: str) -> float:
        deadline = self.no_stream_data_cooldowns.get(username)
        if deadline is None:
            return 0.0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self.no_stream_data_cooldowns.pop(username, None)
            return 0.0
        return remaining

    async def start_live_recording(
        self,
        username: str,
        room_id: Optional[str] = None,
        force_restart: bool = False,
        ignore_no_stream_data_cooldown: bool = False,
    ) -> ProcessStartResult:
        """Serialize start decisions for one username to prevent duplicates."""
        if normalize_tiktok_username(username, strip_at=False) != username:
            return ProcessStartResult(
                False, None, None, "Invalid TikTok username", started_new=False
            )
        lock = self._start_locks.setdefault(username, asyncio.Lock())
        async with lock:
            return await self._start_live_recording_locked(
                username,
                room_id=room_id,
                force_restart=force_restart,
                ignore_no_stream_data_cooldown=ignore_no_stream_data_cooldown,
            )

    async def _start_live_recording_locked(
        self,
        username: str,
        room_id: Optional[str] = None,
        force_restart: bool = False,
        ignore_no_stream_data_cooldown: bool = False,
    ) -> ProcessStartResult:
        """
        Start a live recording process for a user

        Args:
            username: TikTok username
            room_id: TikTok room ID (optional)
            force_restart: Force restart if process already exists

        Returns:
            ProcessStartResult with success status and details
        """
        try:
            # Check if user already has an active recording
            existing_process = self.metadata_store.get_process_by_username(username)
            if existing_process and not force_restart:
                if self._is_process_running(existing_process.pid):
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.info(f"User already has a running recording (PID={existing_process.pid})")
                    return ProcessStartResult(
                        True, existing_process.id, existing_process.pid, None, started_new=False
                    )

                # A verified dead PID must not force a duplicate active row.
                self.cleanup_zero_byte_output(existing_process)
                self.metadata_store.mark_process_stopped(existing_process.id)

            if not force_restart:
                system_processes = [
                    process
                    for process in self.get_system_recorder_processes()
                    if process.username == username
                ]
                if system_processes:
                    process = min(system_processes, key=lambda item: item.create_time)
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.debug(
                        f"Recorder already exists in the system (PID={process.pid}); "
                        "not starting a duplicate"
                    )
                    return ProcessStartResult(
                        True, None, process.pid, None, started_new=False
                    )

            if not ignore_no_stream_data_cooldown:
                cooldown_remaining = self._no_stream_data_cooldown_remaining(
                    username
                )
                if cooldown_remaining > 0:
                    error = (
                        "NO_STREAM_DATA_COOLDOWN: recorder retry suppressed for "
                        f"{cooldown_remaining:.0f} more seconds"
                    )
                    UserContextLogger(self.logger, username).debug(error)
                    return ProcessStartResult(False, None, None, error)

            # Check process limit only when a new process is actually needed.
            max_processes = self.config.get('max_live_processes', 20)
            current_count = await self._count_running_processes()

            if current_count >= max_processes:
                error = f"Maximum live processes limit reached ({current_count}/{max_processes})"
                self.logger.warning(error)
                return ProcessStartResult(False, None, None, error)
            elif current_count >= max_processes * 0.8:  # 80% capacity warning
                self.logger.warning(f"Approaching live processes limit: {current_count}/{max_processes} ({current_count/max_processes*100:.0f}%)")

            # Create recordings directory for user
            recordings_root = self.recordings_path.resolve()
            user_recording_dir = recordings_root / username
            if user_recording_dir.parent != recordings_root:
                raise ValueError("User recording directory escapes recordings root")
            if user_recording_dir.is_symlink():
                raise ValueError("Refusing symlinked user recording directory")
            user_recording_dir.mkdir(parents=True, exist_ok=True)

            # Generate recording file path with new scheme: username_YYYYMMDD_HHMMSS.mp4
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            recording_filename = f"{username}_{timestamp}.mp4"
            recording_file_path = user_recording_dir / recording_filename

            # Create the process command
            process_cmd = self._build_recording_command(username, room_id, user_recording_dir, recording_file_path)
            self._ensure_recorder_log_directory()
            startup_metadata_file = (
                self.recorder_log_path
                / f"startup_{username}_{uuid.uuid4().hex}.json"
            )
            process_cmd.extend([
                "--startup-metadata-file",
                str(startup_metadata_file),
            ])

            user_logger = UserContextLogger(self.logger, username)
            user_logger.info(f"Starting live recording: {' '.join(process_cmd)}")

            # Start the process
            if self.detached_process_mode:
                process, pid, error_msg = await self._start_detached_process(process_cmd, username)
            else:
                process, pid, error_msg = await self._start_attached_process(process_cmd, username)

            if not process or not pid:
                error = error_msg or "Failed to start recording process"
                # Check if it's a race condition (normal behavior) vs actual error
                if "RACE_CONDITION" in str(error):
                    user_logger.debug(f"Race condition: {error}")
                    self.stats['race_conditions'] = self.stats.get('race_conditions', 0) + 1
                elif "NO_STREAM_DATA" in str(error):
                    user_logger.debug(error)
                elif "ROOM_ID_UNAVAILABLE" in str(error):
                    user_logger.info(error)
                    self.stats['room_id_unavailable'] = self.stats.get('room_id_unavailable', 0) + 1
                    self._notify_room_id_unavailable(username, str(error))
                else:
                    user_logger.debug(error)
                    self.stats['startup_failures'] += 1
                return ProcessStartResult(False, None, None, error)

            # Register process in database
            defer_metadata_cleanup = False
            try:
                register_kwargs = dict(
                    username=username,
                    pid=pid,
                    room_id=room_id,
                    recording_file_path=str(recording_file_path)
                )
                live_id = getattr(process, "_live_id", None)
                if live_id is not None:
                    register_kwargs["live_id"] = live_id
                process_id = self.metadata_store.register_process(**register_kwargs)
                if live_id is None:
                    task = asyncio.create_task(
                        self._link_delayed_startup_metadata(
                            process_id,
                            pid,
                            username,
                            startup_metadata_file,
                        )
                    )
                    self.startup_metadata_tasks.add(task)
                    task.add_done_callback(self.startup_metadata_tasks.discard)
                    defer_metadata_cleanup = True

                self.stats['processes_started'] += 1
                user_logger.info(f"Registered live recording process: PID={pid}, DB_ID={process_id}")

                # Call startup callbacks
                for callback in self.startup_callbacks:
                    try:
                        await callback(username, pid, process_id, str(recording_file_path))
                    except Exception as e:
                        self.logger.error(f"Error in startup callback: {e}")

                return ProcessStartResult(True, process_id, pid, None, started_new=True)

            except Exception as e:
                # If database registration fails, try to kill the process
                error_msg = str(e)
                if "race condition" in error_msg:
                    # This is an expected race condition, not a real error
                    user_logger.info("⚡ Race condition: brief live session ended before recording started")
                    self.stats['race_conditions'] = self.stats.get('race_conditions', 0) + 1
                    error = f"RACE_CONDITION: {e}"
                else:
                    # This is a real database error
                    self.logger.error(f"Failed to register process in database: {e}")
                    self.stats['startup_failures'] += 1
                    error = f"Failed to register process in database: {e}"

                try:
                    terminated = await self._terminate_process(pid)
                    if terminated:
                        self._cleanup_exact_zero_byte_output(
                            username,
                            pid,
                            str(recording_file_path),
                        )
                except:
                    pass
                return ProcessStartResult(False, None, pid, error)
            finally:
                if not defer_metadata_cleanup:
                    try:
                        startup_metadata_file.unlink(missing_ok=True)
                    except OSError:
                        pass

        except Exception as e:
            error = f"Unexpected error starting recording for {username}: {e}"
            self.logger.error(error)
            self.stats['startup_failures'] += 1
            return ProcessStartResult(False, None, None, error)

    @staticmethod
    def _recording_output_from_command(cmd: List[str]) -> Optional[str]:
        """Return the exact ``--output-file`` value from a recorder command."""
        try:
            index = cmd.index("--output-file")
        except ValueError:
            return None
        if index + 1 >= len(cmd):
            return None
        return str(cmd[index + 1])

    def _cleanup_exact_zero_byte_output(
        self,
        username: str,
        pid: int,
        recording_file_path: Optional[str],
    ) -> bool:
        """Delete one verified recorder output after its exact PID has stopped."""
        user_logger = UserContextLogger(self.logger, username)
        if not recording_file_path or self._is_process_running(pid):
            return False

        path = Path(recording_file_path)
        if path.suffix.lower() != ".mp4":
            user_logger.debug(
                f"Skipping empty output cleanup for PID={pid}: not an MP4 path"
            )
            return False

        try:
            expected_directory = (self.recordings_path / username).resolve()
            resolved_path = path.resolve(strict=True)
            file_stat = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            user_logger.info(
                f"Could not inspect exact recorder output for PID={pid}: {error}"
            )
            return False

        if resolved_path.parent != expected_directory:
            user_logger.info(
                f"Refusing empty output cleanup for PID={pid}: path is outside "
                f"the user recording directory: {recording_file_path}"
            )
            return False
        if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
            user_logger.info(
                f"Refusing empty output cleanup for PID={pid}: path is not a regular file"
            )
            return False

        try:
            active_recorders = self.get_system_recorder_processes()
        except Exception as error:
            user_logger.info(
                f"Could not verify output ownership for PID={pid}; keeping file: {error}"
            )
            return False

        for recorder in active_recorders:
            if not recorder.output_file:
                continue
            try:
                active_output = Path(recorder.output_file).resolve(strict=False)
            except OSError:
                continue
            if active_output == resolved_path:
                user_logger.debug(
                    f"Skipping empty output cleanup for PID={pid}: "
                    f"the path belongs to active PID={recorder.pid}"
                )
                return False

        if file_stat.st_size != 0:
            return False

        try:
            resolved_path.unlink()
            user_logger.info(
                f"Removed exact 0-byte recorder output after PID={pid} stopped: "
                f"{resolved_path}"
            )
            return True
        except OSError as error:
            user_logger.info(
                f"Could not remove exact 0-byte recorder output for PID={pid}: {error}"
            )
            return False

    def cleanup_zero_byte_output(self, process_info: ProcessInfo) -> bool:
        """Clean the exact output path recorded for one stopped process."""
        return self._cleanup_exact_zero_byte_output(
            process_info.username,
            process_info.pid,
            process_info.recording_file_path,
        )

    def _build_recording_command(self, username: str, room_id: Optional[str],
                                output_dir: Path, recording_file_path: Path) -> List[str]:
        """
        Build the command to start a recording process

        Args:
            username: TikTok username
            room_id: TikTok room ID (optional)
            output_dir: Output directory for recording
            recording_file_path: Full path to recording file

        Returns:
            Command list ready for subprocess execution
        """
        cmd = [
            sys.executable,
            "-m", "recorder.main",
            "-user", username,
            "-mode", "automatic",
            "--output-file", str(recording_file_path),  # Use exact file path instead of directory
            "--supervisor-log-path", str(self.log_path),
            "--persistent-mode"  # New flag for persistent mode
        ]

        if self.config_path:
            cmd.extend(["--config", str(self.config_path)])

        if self.segment_on_reconnect:
            cmd.append("--segment-on-reconnect")

        if self.metadata_enabled:
            cmd.extend([
                "--metadata-path",
                str(self.metadata_path),
                "--compressed-output-path",
                str(self.compressed_output_path),
            ])

        if room_id:
            cmd.extend(["-room_id", room_id])

        if self.no_cookies:
            cmd.append("--no-cookies")
        elif self.cookie_file_path:
            cmd.extend(["--cookie-file", self.cookie_file_path])

        return cmd

    def _attach_startup_metadata(
        self,
        process: subprocess.Popen,
        cmd: List[str],
        username: str,
    ) -> None:
        """Attach the recorder-owned live row ID to the spawned process object."""
        try:
            option_index = cmd.index("--startup-metadata-file")
            metadata_path = Path(cmd[option_index + 1])
            if not metadata_path.exists():
                UserContextLogger(self.logger, username).debug(
                    "Startup metadata not ready; delayed linking scheduled"
                )
                return

            with open(metadata_path, "r", encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)

            if int(metadata.get("pid", -1)) != process.pid:
                UserContextLogger(self.logger, username).warning(
                    "Recorder startup metadata PID does not match spawned process"
                )
                return

            live_id = metadata.get("live_id")
            if live_id is not None:
                process._live_id = int(live_id)
        except (ValueError, TypeError, OSError, json.JSONDecodeError, IndexError) as error:
            UserContextLogger(self.logger, username).warning(
                f"Could not read recorder startup metadata: {error}"
            )

    async def _link_delayed_startup_metadata(
        self,
        process_id: int,
        pid: int,
        username: str,
        metadata_path: Path,
    ) -> None:
        """Backfill live_id when recorder initialization takes longer than five seconds."""
        user_logger = UserContextLogger(self.logger, username)
        try:
            for _ in range(60):
                if metadata_path.exists():
                    try:
                        with open(metadata_path, "r", encoding="utf-8") as metadata_file:
                            metadata = json.load(metadata_file)
                        if int(metadata.get("pid", -1)) != pid:
                            user_logger.error(
                                "Delayed startup metadata PID does not match recorder"
                            )
                            return
                        live_id = int(metadata["live_id"])
                        if self.metadata_store.link_process_live_session(process_id, live_id):
                            user_logger.info(
                                f"Linked delayed startup metadata: DB_ID={process_id}, live_id={live_id}"
                            )
                        else:
                            user_logger.warning(
                                "Could not link delayed startup metadata: "
                                f"DB_ID={process_id}, live_id={live_id}"
                            )
                        return
                    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
                        user_logger.warning(
                            f"Could not parse delayed startup metadata: {error}"
                        )
                        return
                if not self._is_process_running(pid):
                    return
                await asyncio.sleep(1)
            user_logger.warning(
                f"Recorder did not publish startup metadata within 60 seconds: PID={pid}"
            )
        finally:
            try:
                metadata_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _start_detached_process(self, cmd: List[str], username: str) -> Tuple[Optional[subprocess.Popen], Optional[int], Optional[str]]:
        """
        Start a process in detached mode using nohup

        Args:
            cmd: Command to execute
            username: Username for logging/tracking

        Returns:
            Tuple of (process object, PID, error_message) or (None, None, error_msg) if failed
        """
        try:
            recording_output = self._recording_output_from_command(cmd)
            # Create output files for stdout/stderr
            log_dir = self._ensure_recorder_log_directory()

            log_token = uuid.uuid4().hex
            stdout_file = log_dir / f".live_{username}_{log_token}.out"
            stderr_file = log_dir / f".live_{username}_{log_token}.err"

            # Build nohup command
            nohup_cmd = ["nohup"] + cmd

            self.logger.debug(f"Starting detached process: {' '.join(nohup_cmd)}")

            # Start process with nohup in detached mode
            stdout_fd = os.open(
                stdout_file,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            stderr_fd = os.open(
                stderr_file,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            os.fchmod(stdout_fd, 0o600)
            os.fchmod(stderr_fd, 0o600)

            with os.fdopen(stdout_fd, "w") as stdout_stream, os.fdopen(stderr_fd, "w") as stderr_stream:
                process = subprocess.Popen(
                    nohup_cmd,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    stdin=subprocess.DEVNULL,
                    cwd=str(self.project_root),
                    start_new_session=True  # Detach from parent process group
                    # Remove preexec_fn - start_new_session is sufficient for detaching
                )

            pid_stdout_file = log_dir / f"live_{username}_{process.pid}_{log_token}.out"
            pid_stderr_file = log_dir / f"live_{username}_{process.pid}_{log_token}.err"
            stdout_file.rename(pid_stdout_file)
            stderr_file.rename(pid_stderr_file)
            stdout_file = pid_stdout_file
            stderr_file = pid_stderr_file

            # Give the process more time to start and detect early errors
            # Increased from 2 to 5 seconds to better catch child process failures
            await asyncio.sleep(5)

            # Check stderr for WAF errors and race conditions even if parent process is still running
            # This catches cases where child process fails but nohup parent is still alive
            user_logger = UserContextLogger(self.logger, username)
            waf_detected = False
            race_condition_detected = False
            room_id_unavailable_detected = False
            stderr_content = ""

            try:
                with open(stderr_file, 'r') as f:
                    stderr_content = f.read()
                if stderr_content:
                    for stderr_line in stderr_content.splitlines():
                        marker_position = stderr_line.find("[LIVE_IDENTITY]")
                        if marker_position >= 0:
                            user_logger.info(stderr_line[marker_position:])

                    if "WAF" in stderr_content or "blocked" in stderr_content.lower():
                        waf_detected = True
                        user_logger.warning(f"WAF block detected in stderr: {stderr_content[:200]}")
                    elif "not hosting a live stream at the moment" in stderr_content:
                        race_condition_detected = True
                        user_logger.debug("Race condition detected - user went offline between detection and recording start")
                    elif self._is_room_id_unavailable_error(stderr_content):
                        room_id_unavailable_detected = True
                        user_logger.info("Recorder could not resolve RoomID from TikTok response")
            except Exception:
                pass  # Ignore file read errors

            # Check if parent process has terminated
            parent_terminated = process.poll() is not None
            no_stream_data_detected = (
                parent_terminated
                and self._is_no_stream_data_exit(
                    process.returncode,
                    stderr_content,
                )
            )

            if parent_terminated or waf_detected or room_id_unavailable_detected:
                if parent_terminated:
                    # Check if this is a race condition (normal exit) vs actual error
                    if race_condition_detected and process.returncode in (0, 11):
                        user_logger.debug(f"Process exited normally - user went offline (returncode={process.returncode})")
                        self._cleanup_exact_zero_byte_output(
                            username, process.pid, recording_output
                        )
                        # Return a specific error type that supervisor can handle gracefully
                        return None, None, "RACE_CONDITION: User went offline between detection and recording start"
                    elif room_id_unavailable_detected:
                        user_logger.info(f"Process exited because RoomID is unavailable (returncode={process.returncode})")
                        self._cleanup_exact_zero_byte_output(
                            username, process.pid, recording_output
                        )
                        return None, None, self._room_id_unavailable_message()
                    elif no_stream_data_detected:
                        error = self._no_stream_data_message()
                        user_logger.warning(error)
                        self._cleanup_exact_zero_byte_output(
                            username, process.pid, recording_output
                        )
                        return None, None, error
                    else:
                        user_logger.debug(
                            "Detached recorder exited unexpectedly during startup "
                            f"(returncode={process.returncode})"
                        )
                elif room_id_unavailable_detected:
                    self._cleanup_exact_zero_byte_output(
                        username, process.pid, recording_output
                    )
                    return None, None, self._room_id_unavailable_message()

                if waf_detected:
                    self._cleanup_exact_zero_byte_output(
                        username, process.pid, recording_output
                    )
                    return None, None, f"WAF block detected: {stderr_content[:200]}"

                self._cleanup_exact_zero_byte_output(
                    username, process.pid, recording_output
                )
                return None, None, (
                    "Recorder exited unexpectedly during startup "
                    f"(returncode={process.returncode if parent_terminated else 'N/A'}); "
                    "inspect the child stderr log"
                )

            pid = process.pid
            self._attach_startup_metadata(process, cmd, username)
            user_logger = UserContextLogger(self.logger, username)
            user_logger.info(f"Started detached recording process: PID={pid}")

            return process, pid, None

        except Exception as e:
            UserContextLogger(self.logger, username).debug(
                f"Failed to start detached process: {e}"
            )
            return None, None, f"Failed to start detached process: {e}"

    async def _start_attached_process(self, cmd: List[str], username: str) -> Tuple[Optional[subprocess.Popen], Optional[int], Optional[str]]:
        """
        Start a process in attached mode (for debugging/testing)

        Args:
            cmd: Command to execute
            username: Username for logging/tracking

        Returns:
            Tuple of (process object, PID, error_message) or (None, None, error_msg) if failed
        """
        try:
            recording_output = self._recording_output_from_command(cmd)
            self.logger.debug(f"Starting attached process: {' '.join(cmd)}")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=str(self.project_root)
            )

            # Give the process a moment to start
            await asyncio.sleep(1)

            # Check if process is still running
            if process.poll() is not None:
                # Process has already terminated
                stdout, stderr = process.communicate()
                user_logger = UserContextLogger(self.logger, username)

                stderr_text = stderr.decode() if stderr else ""
                room_id_unavailable_detected = self._is_room_id_unavailable_error(stderr_text)
                race_condition_detected = (
                    "not hosting a live stream at the moment" in stderr_text
                    and process.returncode in (0, 11)
                )
                no_stream_data_detected = self._is_no_stream_data_exit(
                    process.returncode,
                    stderr_text,
                )
                if race_condition_detected:
                    user_logger.debug(
                        "Attached process exited normally because the user went offline "
                        f"(returncode={process.returncode})"
                    )
                elif room_id_unavailable_detected:
                    user_logger.info(f"Attached process exited because RoomID is unavailable (returncode={process.returncode})")
                elif no_stream_data_detected:
                    error = self._no_stream_data_message()
                    user_logger.warning(error)
                else:
                    user_logger.debug(
                        "Attached recorder exited unexpectedly during startup "
                        f"(returncode={process.returncode})"
                    )
                if stderr_text:
                    if room_id_unavailable_detected:
                        user_logger.info(f"Process stderr: {stderr_text}")
                    else:
                        self.logger.debug(f"Process stderr: {stderr_text}")

                # Surface WAF failures without starting an external recorder.
                if "WAF" in stderr_text or "blocked" in stderr_text.lower():
                    user_logger.warning(f"WAF block detected in attached mode: {stderr_text[:200]}")
                    self._cleanup_exact_zero_byte_output(
                        username, process.pid, recording_output
                    )
                    return None, None, f"WAF block detected: {stderr_text[:200]}"

                if room_id_unavailable_detected:
                    self._cleanup_exact_zero_byte_output(
                        username, process.pid, recording_output
                    )
                    return None, None, self._room_id_unavailable_message()

                if race_condition_detected:
                    self._cleanup_exact_zero_byte_output(
                        username, process.pid, recording_output
                    )
                    return None, None, (
                        "RACE_CONDITION: User went offline between detection "
                        "and recording start"
                    )

                if no_stream_data_detected:
                    self._cleanup_exact_zero_byte_output(
                        username, process.pid, recording_output
                    )
                    return None, None, error

                self._cleanup_exact_zero_byte_output(
                    username, process.pid, recording_output
                )
                return None, None, (
                    "Recorder exited unexpectedly during startup "
                    f"(returncode={process.returncode}); inspect the child stderr log"
                )

            pid = process.pid
            self._attach_startup_metadata(process, cmd, username)
            user_logger = UserContextLogger(self.logger, username)
            user_logger.info(f"Started attached recording process: PID={pid}")

            return process, pid, None

        except Exception as e:
            UserContextLogger(self.logger, username).debug(
                f"Failed to start attached process: {e}"
            )
            return None, None, f"Failed to start attached process: {e}"

    async def stop_live_recording(self, username: str, graceful: bool = True) -> bool:
        """
        Stop a live recording process for a user

        Args:
            username: TikTok username
            graceful: Whether to attempt graceful shutdown first

        Returns:
            True if process was stopped successfully, False otherwise
        """
        try:
            processes = [
                process
                for process in self.metadata_store.get_active_processes()
                if process.username == username
            ]
            if not processes:
                user_logger = UserContextLogger(self.logger, username)
                user_logger.warning("No active recording process found")
                return True  # Consider this success since there's nothing to stop

            results = []
            for process_info in processes:
                results.append(
                    await self._stop_process_info(
                        process_info,
                        graceful=graceful,
                        final_status='stopped',
                    )
                )

            success = all(results)
            if success and not any(
                process.username == username
                for process in self.get_system_recorder_processes()
            ):
                self.metadata_store.reset_user_live_status(username)
            return success

        except Exception as e:
            user_logger = UserContextLogger(self.logger, username)
            user_logger.error(f"Error stopping recording: {e}")
            return False

    async def stop_process(
        self,
        process_info: ProcessInfo,
        graceful: bool = True,
        final_status: str = 'stopped',
        reset_live_status: bool = False,
    ) -> bool:
        """Stop one exact tracked process and only then update its metadata."""
        success = await self._stop_process_info(
            process_info,
            graceful=graceful,
            final_status=final_status,
        )
        if success and reset_live_status and not any(
            process.username == process_info.username
            for process in self.get_system_recorder_processes()
        ):
            self.metadata_store.reset_user_live_status(process_info.username)
        return success

    async def _stop_process_info(
        self,
        process_info: ProcessInfo,
        graceful: bool,
        final_status: str,
    ) -> bool:
        success = await self._terminate_process(process_info.pid, graceful)
        user_logger = UserContextLogger(self.logger, process_info.username)
        if not success:
            self.metadata_store.update_health_status(
                process_info.id,
                'termination_failed',
                process_info.file_size_bytes,
            )
            user_logger.error(f"Failed to stop recording process (PID={process_info.pid})")
            return False

        self.metadata_store.mark_process_stopped(process_info.id, final_status)
        self.metadata_store.close_process_live_session(process_info.id)
        self.cleanup_zero_byte_output(process_info)
        self.stats['processes_stopped'] += 1
        user_logger.info(f"Stopped recording process (PID={process_info.pid})")

        for callback in self.shutdown_callbacks:
            try:
                await callback(process_info.username, process_info.pid, process_info.id)
            except Exception as e:
                self.logger.error(f"Error in shutdown callback: {e}")

        return True

    async def restart_live_recording(self, process_info: ProcessInfo, issues: List[str]) -> bool:
        """
        Restart a live recording process

        Args:
            process_info: Information about the process to restart
            issues: List of issues that triggered the restart

        Returns:
            True if restart was successful, False otherwise
        """
        username = process_info.username

        try:
            user_logger = UserContextLogger(self.logger, username)

            if normalize_tiktok_username(username, strip_at=False) != username:
                user_logger.error("Restart refused: invalid TikTok username")
                return False

            # Check if user is still live before attempting restart
            if not self.metadata_store.is_user_live(username):
                user_logger.info("⚡ Skipping restart: user is no longer live")
                return False

            user_logger.info(f"Restarting recording process due to: {', '.join(issues)}")

            # Stop ALL existing processes for this user to prevent duplicates
            cleanup_success = await self._cleanup_all_user_processes(username)
            if not cleanup_success:
                user_logger.error("Restart aborted: an existing recorder is still running")
                return False
            await asyncio.sleep(self.process_restart_delay)

            # Start new process
            result = await self.start_live_recording(
                username, process_info.room_id, force_restart=True
            )

            if result.success:
                # start_live_recording already registered the replacement row.
                # Do not also point the stopped historical row at the new PID.
                if result.started_new:
                    self.stats['processes_restarted'] += 1
                user_logger.info(f"Successfully restarted recording: new PID={result.pid}")
                return True
            else:
                # Check if it's a race condition (normal behavior) vs actual error
                if "RACE_CONDITION" in str(result.error):
                    user_logger.debug(f"Race condition during restart: {result.error}")
                elif (
                    "ROOM_ID_UNAVAILABLE" in str(result.error)
                    or "NO_STREAM_DATA" in str(result.error)
                ):
                    user_logger.info(f"Restart deferred: {result.error}")
                else:
                    user_logger.error(f"Failed to restart recording: {result.error}")
                return False

        except Exception as e:
            user_logger.error(f"Error restarting recording: {e}") if 'user_logger' in locals() else UserContextLogger(self.logger, username).error(f"Error restarting recording: {e}")
            return False

    async def replace_exact_process(
        self,
        process_info: ProcessInfo,
        reason: str,
        final_status: str,
        start_new: bool,
    ) -> bool:
        """Stop one exact recorder and optionally start a replacement."""
        user_logger = UserContextLogger(self.logger, process_info.username)
        if (
            start_new
            and normalize_tiktok_username(process_info.username, strip_at=False)
            != process_info.username
        ):
            user_logger.error("Replacement refused: invalid TikTok username")
            return False
        user_logger.info(
            f"Replacing exact recorder PID={process_info.pid}: {reason}"
        )
        stop_success = await self.stop_process(
            process_info,
            graceful=True,
            final_status=final_status,
            reset_live_status=False,
        )

        if not start_new:
            return stop_success

        ignore_no_stream_data_cooldown = False
        if reason == "recorder produced no stream data":
            cooldown_remaining = self._no_stream_data_cooldown_remaining(
                process_info.username
            )
            if cooldown_remaining > 0:
                user_logger.info(
                    "Skipping repeated zero-data replacement; retry cooldown "
                    f"has {cooldown_remaining:.0f} seconds remaining"
                )
                return stop_success
            self.no_stream_data_cooldowns[process_info.username] = (
                time.monotonic() + self.no_stream_data_retry_cooldown
            )
            # Permit one immediate replacement. A second zero-data exit is
            # suppressed until the cooldown expires.
            ignore_no_stream_data_cooldown = True

        result = await self.start_live_recording(
            process_info.username,
            room_id=None,
            force_restart=True,
            ignore_no_stream_data_cooldown=ignore_no_stream_data_cooldown,
        )
        if result.success:
            if result.started_new:
                self.stats['processes_restarted'] += 1
            user_logger.info(
                f"Started replacement recorder PID={result.pid}; "
                f"old_stop_success={stop_success}"
            )
            return True

        error_text = str(result.error)
        message = f"Failed to start replacement after {reason}: {error_text}"
        if "RACE_CONDITION" in error_text:
            user_logger.debug(message)
            if not any(
                process.username == process_info.username
                for process in self.get_system_recorder_processes()
            ):
                self.metadata_store.reset_user_live_status(process_info.username)
        elif (
            "ROOM_ID_UNAVAILABLE" in error_text
            or "NO_STREAM_DATA" in error_text
        ):
            user_logger.info(message)
        else:
            user_logger.error(message)
        return False

    async def _cleanup_all_user_processes(self, username: str) -> bool:
        """
        Clean up ALL active processes for a specific user to prevent duplicates

        Args:
            username: Username to clean up processes for
        """
        try:
            user_logger = UserContextLogger(self.logger, username)

            # Get all active processes for this user
            active_processes = self.metadata_store.get_active_processes()
            user_processes = [p for p in active_processes if p.username == username]

            all_terminated = True

            if not user_processes:
                user_logger.debug("No active database processes found to cleanup")

            user_logger.info(f"Cleaning up {len(user_processes)} active processes")

            for process_info in user_processes:
                try:
                    terminated = True
                    if process_info.pid:
                        user_logger.info(f"Terminating process PID={process_info.pid}")
                        terminated = await self._terminate_process(process_info.pid, graceful=False)
                        if terminated:
                            user_logger.debug(f"Successfully terminated PID={process_info.pid}")
                        else:
                            user_logger.warning(f"Failed to terminate PID={process_info.pid}")
                            all_terminated = False

                    if terminated:
                        self.cleanup_zero_byte_output(process_info)
                        self.metadata_store.mark_process_stopped(process_info.id)
                        self.stats['processes_stopped'] += 1
                        user_logger.debug(f"Marked process {process_info.id} as stopped in database")
                    else:
                        self.metadata_store.update_health_status(
                            process_info.id,
                            'termination_failed',
                            process_info.file_size_bytes,
                        )

                except Exception as e:
                    user_logger.error(f"Error cleaning up process {process_info.id}: {e}")
                    all_terminated = False

            remaining = [
                process
                for process in self.get_system_recorder_processes()
                if process.username == username
            ]
            if remaining:
                all_terminated = False
                user_logger.error(
                    "Recorder PIDs still running after cleanup: "
                    + ", ".join(str(process.pid) for process in remaining)
                )

            user_logger.info("Completed cleanup of all user processes")
            return all_terminated

        except Exception as e:
            user_logger = UserContextLogger(self.logger, username)
            user_logger.error(f"Error during user process cleanup: {e}")
            return False

    async def _terminate_process(self, pid: int, graceful: bool = True) -> bool:
        """
        Terminate a process by PID, handling zombies appropriately

        Args:
            pid: Process ID to terminate
            graceful: Whether to attempt graceful shutdown first

        Returns:
            True if process was terminated, False otherwise
        """
        try:
            # Check if process exists and handle zombies
            if not self._process_exists(pid):
                self.logger.debug(f"Process {pid} does not exist")
                return True

            # If it's a zombie, we can't terminate it normally - the parent needs to reap it
            if self._is_zombie_process(pid):
                self.logger.warning(f"Process {pid} is a zombie - attempting to clean up")
                await self._cleanup_zombie_process(pid)
                return True

            if graceful:
                # Try graceful termination first (SIGTERM)
                try:
                    os.kill(pid, signal.SIGTERM)
                    self.logger.debug(f"Sent SIGTERM to process {pid}")

                    # Wait for graceful shutdown
                    for _ in range(30):  # Wait up to 30 seconds
                        await asyncio.sleep(1)
                        if not self._is_process_running(pid):
                            self.logger.debug(f"Process {pid} terminated gracefully")
                            return True

                    self.logger.warning(f"Process {pid} did not terminate gracefully, using force")
                except ProcessLookupError:
                    # Process already terminated
                    return True

            # Force termination (SIGKILL)
            try:
                os.kill(pid, signal.SIGKILL)
                self.logger.debug(f"Sent SIGKILL to process {pid}")

                # Wait a bit for force kill to take effect
                await asyncio.sleep(2)

                if not self._is_process_running(pid):
                    self.logger.debug(f"Process {pid} force terminated")
                    return True
                else:
                    self.logger.error(f"Process {pid} could not be terminated")
                    return False

            except ProcessLookupError:
                # Process already terminated
                return True

        except Exception as e:
            self.logger.error(f"Error terminating process {pid}: {e}")
            return False

    def _process_exists(self, pid: int) -> bool:
        """
        Check if a process exists (including zombies)

        Args:
            pid: Process ID to check

        Returns:
            True if process exists in any state, False otherwise
        """
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

    async def _cleanup_zombie_process(self, pid: int) -> None:
        """
        Attempt to clean up a zombie process

        Args:
            pid: PID of the zombie process
        """
        try:
            # First, try direct waitpid on the zombie process if we're the parent
            try:
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
                if waited_pid == pid:
                    self.logger.info(f"Successfully reaped zombie process {pid} (exit_status={status})")
                    return
            except ChildProcessError:
                # We're not the parent or process doesn't exist anymore
                pass
            except ProcessLookupError:
                # Process doesn't exist anymore
                return

            # If direct wait failed, try to notify the parent process
            parent_pid = self._get_parent_pid(pid)
            if parent_pid and parent_pid != 1:  # Not init
                self.logger.info(f"Zombie process {pid} has parent {parent_pid}, sending SIGCHLD to parent")
                try:
                    os.kill(parent_pid, signal.SIGCHLD)
                    await asyncio.sleep(2)  # Give parent time to handle signal

                    # Check if zombie was cleaned up
                    if not self._is_zombie_process(pid):
                        self.logger.info(f"Zombie process {pid} successfully cleaned up by parent")
                        return
                except ProcessLookupError:
                    self.logger.debug(f"Parent process {parent_pid} no longer exists")
                except PermissionError:
                    self.logger.debug(f"No permission to signal parent process {parent_pid}")

            # If we're the parent process, try to reap all zombie children
            try:
                while True:
                    waited_pid, status = os.waitpid(-1, os.WNOHANG)
                    if waited_pid == 0:  # No more children to reap
                        break
                    if waited_pid == pid:
                        self.logger.info(f"Successfully reaped target zombie process {pid} (exit_status={status})")
                        return
                    else:
                        self.logger.debug(f"Reaped other zombie process {waited_pid} (exit_status={status})")
            except ChildProcessError:
                # No child processes to reap
                pass

            # Final check if zombie still exists
            if self._is_zombie_process(pid):
                self.logger.warning(f"Zombie process {pid} still exists - may need system-level cleanup")
            else:
                self.logger.debug(f"Zombie process {pid} was cleaned up")

        except Exception as e:
            self.logger.error(f"Error cleaning up zombie process {pid}: {e}")

    def _get_parent_pid(self, pid: int) -> Optional[int]:
        """
        Get the parent PID of a process

        Args:
            pid: Process ID

        Returns:
            Parent PID or None if not found
        """
        try:
            with open(f"/proc/{pid}/stat", "r") as f:
                stat_data = f.read().strip().split()
                if len(stat_data) > 3:
                    return int(stat_data[3])  # Fourth field is parent PID
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            pass
        return None

    def _is_process_running(self, pid: int) -> bool:
        """
        Check if a process is running and not a zombie

        Args:
            pid: Process ID to check

        Returns:
            True if process is running and healthy, False if dead or zombie
        """
        try:
            os.kill(pid, 0)  # Signal 0 just checks if process exists

            # Additional check for zombie processes
            if self._is_zombie_process(pid):
                self.logger.debug(f"Process {pid} is a zombie, treating as dead")
                return False

            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we don't have permission to signal it
            # Still check if it's a zombie
            if self._is_zombie_process(pid):
                return False
            return True
        except Exception:
            return False

    def _is_zombie_process(self, pid: int) -> bool:
        """
        Check if a process is a zombie (defunct)

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

    @staticmethod
    def _recording_mtime(process: RecorderProcess) -> float:
        if not process.output_file:
            return -1.0
        try:
            return Path(process.output_file).stat().st_mtime
        except OSError:
            return -1.0

    def get_system_recorder_processes(self) -> List[RecorderProcess]:
        """Return the exact recorder processes currently visible to the OS."""
        return list_recorder_processes()

    def build_orphan_cleanup_plan(self) -> List[OrphanProcessCandidate]:
        """Find untracked recorders and redundant active recorder processes."""
        system_processes = self.get_system_recorder_processes()
        active_processes = self.metadata_store.get_active_processes()
        active_by_identity: Dict[Tuple[int, str], List[ProcessInfo]] = {}

        for process_info in active_processes:
            active_by_identity.setdefault(
                (process_info.pid, process_info.username), []
            ).append(process_info)

        candidates: Dict[int, OrphanProcessCandidate] = {}
        matched_by_user: Dict[str, List[Tuple[RecorderProcess, ProcessInfo]]] = {}

        for process in system_processes:
            matches = active_by_identity.get((process.pid, process.username), [])
            if not matches:
                candidates[process.pid] = OrphanProcessCandidate(
                    process=process,
                    reason='no matching active database record',
                )
                continue

            # The newest row is the one normal lookups would return.
            process_info = max(matches, key=lambda item: item.started_at)
            matched_by_user.setdefault(process.username, []).append((process, process_info))

        for username, matches in matched_by_user.items():
            if len(matches) <= 1:
                continue

            keeper, _ = max(
                matches,
                key=lambda item: (
                    self._recording_mtime(item[0]),
                    -item[0].create_time,
                ),
            )

            for process, process_info in matches:
                if process.pid == keeper.pid:
                    continue
                candidates[process.pid] = OrphanProcessCandidate(
                    process=process,
                    reason='redundant active duplicate',
                    process_id=process_info.id,
                    kept_pid=keeper.pid,
                    kept_fingerprint=keeper.fingerprint,
                )

        return sorted(candidates.values(), key=lambda item: (item.process.username, item.process.pid))

    async def stop_orphan_candidates(
        self,
        candidates: List[OrphanProcessCandidate],
        force: bool = False,
    ) -> Dict[int, bool]:
        """Stop only revalidated candidates and update status after verified exit."""
        results: Dict[int, bool] = {}

        for candidate in candidates:
            process = candidate.process
            current_processes = {
                item.pid: item
                for item in self.get_system_recorder_processes()
            }
            current = current_processes.get(process.pid)

            if current is None or current.fingerprint != process.fingerprint:
                UserContextLogger(self.logger, process.username).warning(
                    f"Skipping PID={process.pid}: process identity changed"
                )
                results[process.pid] = False
                continue

            if candidate.kept_pid is not None:
                keeper = current_processes.get(candidate.kept_pid)
                keeper_is_tracked = any(
                    item.pid == candidate.kept_pid
                    and item.username == process.username
                    for item in self.metadata_store.get_active_processes()
                )
                if (
                    keeper is None
                    or keeper.username != process.username
                    or keeper.fingerprint != candidate.kept_fingerprint
                    or not keeper_is_tracked
                ):
                    UserContextLogger(self.logger, process.username).warning(
                        f"Skipping PID={process.pid}: kept PID={candidate.kept_pid} "
                        "is no longer the same active recorder"
                    )
                    results[process.pid] = False
                    continue

            stopped = await self._terminate_process(process.pid, graceful=not force)
            results[process.pid] = stopped
            if not stopped:
                if candidate.process_id is not None:
                    process_info = self.metadata_store.get_process_by_id(candidate.process_id)
                    file_size = process_info.file_size_bytes if process_info else 0
                    self.metadata_store.update_health_status(
                        candidate.process_id, 'termination_failed', file_size
                    )
                continue

            if candidate.process_id is not None:
                process_info = self.metadata_store.get_process_by_id(candidate.process_id)
                if (
                    process_info is not None
                    and process.output_file == process_info.recording_file_path
                ):
                    self.cleanup_zero_byte_output(process_info)
                self.metadata_store.mark_process_stopped(candidate.process_id)
            else:
                self._cleanup_exact_zero_byte_output(
                    process.username,
                    process.pid,
                    process.output_file,
                )

            if not any(
                item.username == process.username
                for item in self.get_system_recorder_processes()
            ):
                self.metadata_store.reset_user_live_status(process.username)

        return results

    async def stop_untracked_process(
        self,
        process: RecorderProcess,
        graceful: bool = True,
    ) -> bool:
        """Stop one exact OS recorder that has no active metadata row."""
        current = {
            item.pid: item
            for item in self.get_system_recorder_processes()
        }.get(process.pid)
        if current is None or current.fingerprint != process.fingerprint:
            UserContextLogger(self.logger, process.username).warning(
                f"Skipping PID={process.pid}: untracked process identity changed"
            )
            return False

        stopped = await self._terminate_process(process.pid, graceful=graceful)
        if stopped:
            self._cleanup_exact_zero_byte_output(
                process.username,
                process.pid,
                process.output_file,
            )
        if stopped and not any(
            item.username == process.username
            for item in self.get_system_recorder_processes()
        ):
            self.metadata_store.reset_user_live_status(process.username)
        return stopped

    async def get_running_processes(self) -> List[ProcessInfo]:
        """
        Get list of currently running live recording processes

        Returns:
            List of ProcessInfo objects for running processes
        """
        active_processes = self.metadata_store.get_active_processes()
        running_processes = []

        for process_info in active_processes:
            if self._is_process_running(process_info.pid):
                running_processes.append(process_info)
            else:
                # Process is dead but still marked as active - this will be handled by health monitor
                user_logger = UserContextLogger(self.logger, process_info.username)
                user_logger.debug(f"Process (PID={process_info.pid}) is marked active but not running")

        return running_processes

    async def stop_all_processes(self, graceful: bool = True) -> Dict[str, bool]:
        """
        Stop all active recording processes

        Args:
            graceful: Whether to attempt graceful shutdown

        Returns:
            Dictionary mapping username to success status
        """
        active_processes = self.metadata_store.get_active_processes()
        results = {}

        if not active_processes:
            self.logger.info("No active processes to stop")
            return results

        self.logger.info(f"Stopping {len(active_processes)} active recording processes")

        # Stop processes concurrently
        tasks = []
        for process_info in active_processes:
            task = asyncio.create_task(self.stop_live_recording(process_info.username, graceful))
            tasks.append((process_info.username, task))

        # Wait for all stops to complete
        for username, task in tasks:
            try:
                success = await task
                results[username] = success
            except Exception as e:
                user_logger = UserContextLogger(self.logger, username)
                user_logger.error(f"Error stopping process: {e}")
                results[username] = False

        successful_stops = sum(1 for success in results.values() if success)
        self.logger.info(f"Stopped {successful_stops}/{len(results)} processes successfully")

        return results

    async def _count_running_processes(self) -> int:
        """
        Count actual running live recording processes in the system

        This method counts real processes in the system, not just database entries,
        to ensure we don't exceed the limit due to database inconsistencies.

        Returns:
            Number of currently running live recording processes
        """
        try:
            count = len(self.get_system_recorder_processes())
            self.logger.debug(f"Found {count} running live recording processes in system")
            return count

        except Exception as e:
            self.logger.error(f"Error counting running processes: {e}")
            # Fallback to database count as safety measure
            db_count = len(self.metadata_store.get_active_processes())
            self.logger.warning(f"Using database count as fallback: {db_count}")
            return db_count

    def get_statistics(self) -> Dict[str, any]:
        """
        Get process manager statistics

        Returns:
            Dictionary with statistics
        """
        stats = self.stats.copy()

        # Add current state information
        active_processes = self.metadata_store.get_active_processes()
        stats['active_processes'] = len(active_processes)
        stats['detached_mode'] = self.detached_process_mode

        return stats
