"""
Graceful Shutdown Handler

Handles safe shutdown of all persistent live recording processes.
Provides functionality for listing, stopping, and managing process termination.
"""

import asyncio
import logging
import signal
import time
from typing import Dict, List, Tuple
from datetime import datetime
from pathlib import Path

# Add project root to path for imports
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import UserContextLogger for username-prefixed logging
from utils.user_context_logger import UserContextLogger

from .process_metadata_store import ProcessMetadataStore, ProcessInfo
from .live_process_manager import LiveProcessManager


class ShutdownResult:
    """Result of a shutdown operation"""

    def __init__(self):
        self.total_processes = 0
        self.successful_stops = 0
        self.failed_stops = 0
        self.process_results: Dict[str, bool] = {}
        self.errors: List[str] = []
        self.duration_seconds = 0.0


class GracefulShutdownHandler:
    """
    Handles graceful shutdown of persistent live recording processes

    Provides comprehensive shutdown functionality including:
    - Listing all active processes
    - Graceful termination with fallback to force kill
    - Progress reporting and error handling
    - Cleanup of database records
    """

    def __init__(self, metadata_store: ProcessMetadataStore, process_manager: LiveProcessManager, config: Dict):
        self.metadata_store = metadata_store
        self.process_manager = process_manager
        self.config = config
        self.logger = logging.getLogger('SHUT')

        # Configuration parameters
        self.graceful_timeout = config.get('graceful_shutdown_timeout', 30)
        self.force_kill_timeout = config.get('force_kill_timeout', 10)
        self.shutdown_delay_between_processes = config.get('shutdown_delay_between_processes', 1)

        # Statistics
        self.stats = {
            'total_shutdowns_performed': 0,
            'total_processes_stopped': 0,
            'graceful_stops': 0,
            'forced_kills': 0,
            'failed_stops': 0
        }

    def _mark_process_stopped(self, process_info: ProcessInfo) -> None:
        """Finalize one process and clean only its exact empty output."""
        self.process_manager.cleanup_zero_byte_output(process_info)
        self.metadata_store.mark_process_stopped(process_info.id)

    async def list_active_processes(self, detailed: bool = False) -> List[Dict[str, any]]:
        """
        List all active live recording processes

        Args:
            detailed: Include detailed information about each process

        Returns:
            List of dictionaries containing process information
        """
        try:
            active_processes = self.metadata_store.get_active_processes()
            process_list = []
            dead_processes = []  # Track dead processes for cleanup
            seen_pids = set()  # Track PIDs to avoid duplicates

            for process_info in active_processes:
                # Skip duplicates (same PID)
                if process_info.pid in seen_pids:
                    user_logger = UserContextLogger(self.logger, process_info.username)
                    user_logger.debug(f"Skipping duplicate PID {process_info.pid}")
                    continue
                seen_pids.add(process_info.pid)
                # Check if process is actually running
                is_actually_running = self.process_manager._is_process_running(process_info.pid)

                # Determine real status
                if is_actually_running:
                    real_status = process_info.health_status
                    status_type = 'running'
                else:
                    real_status = 'dead'
                    status_type = 'dead'
                    dead_processes.append(process_info)  # Mark for cleanup

                process_data = {
                    'username': process_info.username,
                    'pid': process_info.pid,
                    'started_at': process_info.started_at,
                    'health_status': process_info.health_status,  # DB status
                    'real_status': real_status,  # Actual status
                    'status_type': status_type,  # running/dead
                    'restart_count': process_info.restart_count,
                    'is_running': is_actually_running
                }

                if detailed:
                    process_data.update({
                        'db_id': process_info.id,
                        'user_id': process_info.user_id,
                        'recording_file_path': process_info.recording_file_path,
                        'file_size_bytes': process_info.file_size_bytes,
                        'last_health_check': process_info.last_health_check,
                        'room_id': process_info.room_id
                    })

                    # Add file information if file exists
                    if process_info.recording_file_path:
                        try:
                            file_path = Path(process_info.recording_file_path)
                            if file_path.exists():
                                process_data['file_exists'] = True
                                process_data['file_size_current'] = file_path.stat().st_size
                                process_data['file_modified'] = datetime.fromtimestamp(file_path.stat().st_mtime)
                            else:
                                process_data['file_exists'] = False
                        except Exception as e:
                            process_data['file_error'] = str(e)

                process_list.append(process_data)

            # Auto-cleanup dead processes
            if dead_processes:
                await self._cleanup_dead_processes(dead_processes)

            # Sort by start time (oldest first)
            process_list.sort(key=lambda x: x['started_at'])

            return process_list

        except Exception as e:
            self.logger.error(f"Error listing active processes: {e}")
            return []

    async def _cleanup_dead_processes(self, dead_processes: List) -> None:
        """
        Clean up processes that are marked as active in DB but are actually dead

        Args:
            dead_processes: List of ProcessInfo objects for dead processes
        """
        try:
            self.logger.info(f"Auto-cleanup: Found {len(dead_processes)} dead processes")

            for process_info in dead_processes:
                try:
                    # Mark process as completed in live_processes table
                    self._mark_process_stopped(process_info)

                    # Reset is_live=0 in users table
                    self.metadata_store.reset_user_live_status(process_info.username)

                    user_logger = UserContextLogger(self.logger, process_info.username)
                    user_logger.info(f"Cleaned up dead process (PID={process_info.pid})")

                except Exception as e:
                    user_logger = UserContextLogger(self.logger, process_info.username)
                    user_logger.error(f"Error cleaning up process: {e}")

        except Exception as e:
            self.logger.error(f"Error in auto-cleanup: {e}")

    async def cleanup_dead_processes_command(self) -> Dict[str, int]:
        """
        Manual cleanup command - find and clean all dead processes

        Returns:
            Dictionary with cleanup statistics
        """
        try:
            active_processes = self.metadata_store.get_active_processes()
            dead_processes = []

            # Find all dead processes
            for process_info in active_processes:
                if not self.process_manager._is_process_running(process_info.pid):
                    dead_processes.append(process_info)

            # Clean them up
            if dead_processes:
                await self._cleanup_dead_processes(dead_processes)

            return {
                'total_checked': len(active_processes),
                'dead_found': len(dead_processes),
                'cleaned_up': len(dead_processes)
            }

        except Exception as e:
            self.logger.error(f"Error in manual cleanup: {e}")
            return {'error': str(e)}

    async def shutdown_all_processes(self, force: bool = False, show_progress: bool = True) -> ShutdownResult:
        """
        Shutdown all active live recording processes

        Args:
            force: Skip graceful shutdown and use force kill immediately
            show_progress: Print progress information to console

        Returns:
            ShutdownResult with detailed shutdown information
        """
        start_time = time.time()
        result = ShutdownResult()

        try:
            # Get list of active processes
            active_processes = self.metadata_store.get_active_processes()
            result.total_processes = len(active_processes)

            if result.total_processes == 0:
                if show_progress:
                    print("No active live recording processes found.")
                self.logger.info("No active processes to shutdown")
                return result

            if show_progress:
                print(f"Found {result.total_processes} active live recording processes:")
                for process_info in active_processes:
                    file_info = ""
                    if process_info.recording_file_path:
                        try:
                            file_path = Path(process_info.recording_file_path)
                            if file_path.exists():
                                size_mb = file_path.stat().st_size / (1024 * 1024)
                                file_info = f" -> {file_path.name} ({size_mb:.1f}MB)"
                        except:
                            pass

                    print(f"  - {process_info.username} (PID: {process_info.pid}){file_info}")
                print()

            # Shutdown processes
            if force:
                await self._force_shutdown_all(active_processes, result, show_progress)
            else:
                await self._graceful_shutdown_all(active_processes, result, show_progress)

            # Update statistics
            self.stats['total_shutdowns_performed'] += 1
            self.stats['total_processes_stopped'] += result.successful_stops

            result.duration_seconds = time.time() - start_time

            if show_progress:
                print(f"\nShutdown completed in {result.duration_seconds:.1f} seconds")
                print(f"Successfully stopped: {result.successful_stops}/{result.total_processes} processes")
                if result.failed_stops > 0:
                    print(f"Failed to stop: {result.failed_stops} processes")
                    for error in result.errors:
                        print(f"  Error: {error}")

            self.logger.info(f"Shutdown completed: {result.successful_stops}/{result.total_processes} successful")

            return result

        except Exception as e:
            error_msg = f"Error during shutdown: {e}"
            result.errors.append(error_msg)
            self.logger.error(error_msg)
            result.duration_seconds = time.time() - start_time
            return result

    async def _graceful_shutdown_all(self, processes: List[ProcessInfo], result: ShutdownResult, show_progress: bool) -> None:
        """
        Perform graceful shutdown of all processes

        Args:
            processes: List of processes to shutdown
            result: ShutdownResult to update
            show_progress: Whether to show progress
        """
        if show_progress:
            print("Initiating graceful shutdown...")

        # Send SIGTERM to all processes first
        term_sent = []
        for process_info in processes:
            try:
                if self.process_manager._is_process_running(process_info.pid):
                    import os
                    os.kill(process_info.pid, signal.SIGTERM)
                    term_sent.append(process_info)
                    if show_progress:
                        print(f"  Sent SIGTERM to {process_info.username} (PID: {process_info.pid})")
                else:
                    # Process already dead
                    self._mark_process_stopped(process_info)
                    result.successful_stops += 1
                    result.process_results[process_info.username] = True
                    if show_progress:
                        print(f"  {process_info.username} (PID: {process_info.pid}) was already stopped")

            except Exception as e:
                error_msg = f"Failed to send SIGTERM to {process_info.username}: {e}"
                result.errors.append(error_msg)
                result.failed_stops += 1
                result.process_results[process_info.username] = False
                self.logger.error(error_msg)

        if not term_sent:
            return

        # Wait for graceful shutdown
        if show_progress:
            print(f"\nWaiting up to {self.graceful_timeout} seconds for graceful shutdown...")

        for i in range(self.graceful_timeout):
            await asyncio.sleep(1)

            # Check which processes have terminated
            remaining = []
            for process_info in term_sent:
                if not self.process_manager._is_process_running(process_info.pid):
                    # Process terminated gracefully
                    self._mark_process_stopped(process_info)
                    result.successful_stops += 1
                    result.process_results[process_info.username] = True
                    self.stats['graceful_stops'] += 1
                    if show_progress:
                        print(f"  ✓ {process_info.username} terminated gracefully")
                else:
                    remaining.append(process_info)

            term_sent = remaining
            if not term_sent:
                break

            if show_progress and i % 5 == 0:
                print(f"  Still waiting for {len(term_sent)} processes... ({self.graceful_timeout - i - 1}s remaining)")

        # Force kill remaining processes
        if term_sent:
            if show_progress:
                print(f"\nForce killing {len(term_sent)} remaining processes...")

            await self._force_kill_processes(term_sent, result, show_progress)

    async def _force_shutdown_all(self, processes: List[ProcessInfo], result: ShutdownResult, show_progress: bool) -> None:
        """
        Perform force shutdown of all processes

        Args:
            processes: List of processes to shutdown
            result: ShutdownResult to update
            show_progress: Whether to show progress
        """
        if show_progress:
            print("Initiating force shutdown (SIGKILL)...")

        await self._force_kill_processes(processes, result, show_progress)

    async def _force_kill_processes(self, processes: List[ProcessInfo], result: ShutdownResult, show_progress: bool) -> None:
        """
        Force kill a list of processes

        Args:
            processes: List of processes to kill
            result: ShutdownResult to update
            show_progress: Whether to show progress
        """
        import os

        for process_info in processes:
            try:
                if self.process_manager._is_process_running(process_info.pid):
                    os.kill(process_info.pid, signal.SIGKILL)
                    if show_progress:
                        print(f"  Sent SIGKILL to {process_info.username} (PID: {process_info.pid})")
                else:
                    # Process already dead
                    self._mark_process_stopped(process_info)
                    result.successful_stops += 1
                    result.process_results[process_info.username] = True
                    if show_progress:
                        print(f"  {process_info.username} (PID: {process_info.pid}) was already stopped")
                    continue

                # Give process time to die
                await asyncio.sleep(self.shutdown_delay_between_processes)

            except Exception as e:
                error_msg = f"Failed to send SIGKILL to {process_info.username}: {e}"
                result.errors.append(error_msg)
                result.failed_stops += 1
                result.process_results[process_info.username] = False
                self.logger.error(error_msg)
                continue

        # Wait a bit and verify kills
        await asyncio.sleep(self.force_kill_timeout)

        for process_info in processes:
            if process_info.username in result.process_results:
                continue  # Already handled

            if not self.process_manager._is_process_running(process_info.pid):
                # Process successfully killed
                self._mark_process_stopped(process_info)
                result.successful_stops += 1
                result.process_results[process_info.username] = True
                self.stats['forced_kills'] += 1
                if show_progress:
                    print(f"  ✓ {process_info.username} force killed successfully")
            else:
                # Process still running - this shouldn't happen
                error_msg = f"Process {process_info.username} (PID: {process_info.pid}) survived SIGKILL"
                result.errors.append(error_msg)
                result.failed_stops += 1
                result.process_results[process_info.username] = False
                self.stats['failed_stops'] += 1
                self.logger.error(error_msg)

    async def shutdown_specific_process(self, username: str, force: bool = False) -> bool:
        """
        Shutdown a specific process by username

        Args:
            username: Username to shutdown
            force: Skip graceful shutdown

        Returns:
            True if successful, False otherwise
        """
        try:
            process_info = self.metadata_store.get_process_by_username(username)
            if not process_info:
                user_logger = UserContextLogger(self.logger, username)
                user_logger.warning("No active process found")
                return True  # Consider this success

            user_logger = UserContextLogger(self.logger, username)
            user_logger.info(f"Shutting down process (PID: {process_info.pid})")

            success = await self.process_manager.stop_live_recording(username, graceful=not force)

            if success:
                user_logger.info("Successfully shut down process")
                self.stats['total_processes_stopped'] += 1
                if force:
                    self.stats['forced_kills'] += 1
                else:
                    self.stats['graceful_stops'] += 1
            else:
                user_logger.error("Failed to shut down process")
                self.stats['failed_stops'] += 1

            return success

        except Exception as e:
            user_logger = UserContextLogger(self.logger, username)
            user_logger.error(f"Error shutting down process: {e}")
            self.stats['failed_stops'] += 1
            return False

    async def cleanup_dead_processes(self) -> int:
        """
        Clean up database records for processes that are no longer running

        Returns:
            Number of dead processes cleaned up
        """
        try:
            active_processes = self.metadata_store.get_active_processes()
            dead_count = 0

            for process_info in active_processes:
                if not self.process_manager._is_process_running(process_info.pid):
                    self._mark_process_stopped(process_info)
                    dead_count += 1
                    user_logger = UserContextLogger(self.logger, process_info.username)
                    user_logger.info(f"Cleaned up dead process (PID: {process_info.pid})")

            if dead_count > 0:
                self.logger.info(f"Cleaned up {dead_count} dead processes")

            return dead_count

        except Exception as e:
            self.logger.error(f"Error cleaning up dead processes: {e}")
            return 0

    def get_statistics(self) -> Dict[str, any]:
        """
        Get shutdown handler statistics

        Returns:
            Dictionary with statistics
        """
        return self.stats.copy()

    async def verify_shutdown_complete(self) -> Tuple[bool, List[str]]:
        """
        Verify that all processes have been properly shut down

        Returns:
            Tuple of (all_stopped, list_of_remaining_usernames)
        """
        try:
            active_processes = self.metadata_store.get_active_processes()
            still_running = []

            for process_info in active_processes:
                if self.process_manager._is_process_running(process_info.pid):
                    still_running.append(process_info.username)

            return len(still_running) == 0, still_running

        except Exception as e:
            self.logger.error(f"Error verifying shutdown: {e}")
            return False, []
