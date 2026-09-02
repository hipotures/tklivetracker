"""
Process Metadata Store

Manages metadata for persistent live recording processes in the database.
Handles registration, updates, cleanup and querying of process information.
"""

from typing import Dict, List, Optional
from datetime import datetime
from pathlib import Path

# Add project root to path for imports
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import UserContextLogger for username-prefixed logging
from utils.user_context_logger import UserContextLogger  # noqa: E402

from modules.db_setup import create_connection  # noqa: E402
from modules.db_base import DatabaseOperationBase, DatabaseError  # noqa: E402
from .process_info import ProcessInfo  # noqa: E402
from .process_info_factory import ProcessInfoFactory, process_info_factory  # noqa: E402


class ProcessMetadataStore(DatabaseOperationBase):
    """
    Manages metadata for persistent live recording processes

    Handles database operations for tracking process lifecycle,
    health status, and metadata for independent live recordings.
    """

    def __init__(self, db_path: str):
        super().__init__(db_path, 'PMDS')
        self.process_factory = ProcessInfoFactory()

        # Ensure database tables exist
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Ensure the live_processes table exists"""
        try:
            conn = create_connection(self.db_path)
            # The table creation is handled in db_setup.py initialize_db()
            # We just need to call it to ensure tables exist
            from modules.db_setup import initialize_db
            initialize_db(conn)
            conn.close()
            self.logger.debug("Database tables ensured")
        except Exception as e:
            self.logger.error(f"Failed to ensure database tables: {e}")
            raise

    def register_process(self, username: str, pid: int, room_id: Optional[str] = None,
                        recording_file_path: Optional[str] = None,
                        live_id: Optional[int] = None) -> int:
        """
        Register a new live recording process

        Args:
            username: TikTok username
            pid: Process ID of the recording process
            room_id: TikTok room ID (if available)
            recording_file_path: Path to the recording file

        Returns:
            Process ID in database

        Raises:
            DatabaseError: If registration fails
        """
        try:
            # Get user_id and live status from users table
            user_row = self.execute_query(
                "SELECT id, is_live FROM users WHERE username = ?",
                (username,),
                fetch_one=True,
                operation_name="register_process"
            )

            if not user_row:
                raise ValueError(f"User {username} not found in database")

            user_id, is_live = user_row

            # Validate that user is actually live or starting before creating process
            if is_live not in (1, 2):  # Accept both live (1) and starting (2) states
                user_logger = UserContextLogger(self.logger, username)
                user_logger.info("⚡ Race condition detected: user went offline before process registration (brief live session)")
                raise ValueError(f"User {username} is not live (is_live={is_live}) - race condition")

            user_logger = UserContextLogger(self.logger, username)
            user_logger.debug(f"Registering process for live user (user_id={user_id})")

            # Insert new process record
            data = {
                'user_id': user_id,
                'username': username,
                'pid': pid,
                'recording_file_path': recording_file_path,
                'room_id': room_id,
                'started_at': datetime.now(),
                'health_status': 'healthy',
                'restart_count': 0,
                'file_size_bytes': 0,
                'is_active': 1
            }
            if live_id is not None:
                data['live_id'] = live_id

            process_id = self.insert_record(
                'live_processes',
                data,
                "register_process"
            )

            user_logger.info(f"Registered process: PID={pid}, DB_ID={process_id}")
            return process_id

        except (DatabaseError, ValueError):
            raise  # Re-raise our custom errors
        except Exception as e:
            error_msg = f"Failed to register process for {username}: {e}"
            self.logger.error(error_msg)
            raise DatabaseError(error_msg, "register_process") from e

    def has_live_process_link_column(self) -> bool:
        """Check the manually managed live_processes.live_id schema prerequisite."""
        conn = create_connection(self.db_path)
        try:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(live_processes)").fetchall()
            }
            return 'live_id' in columns
        finally:
            conn.close()

    def get_process_live_identity(self, process_id: int) -> Optional[Dict]:
        """Return the exact TikTok identity linked to one recorder process."""
        try:
            row = self.execute_query(
                """
                SELECT lp.live_id, l.tiktok_stream_id, l.tiktok_started_at
                FROM live_processes lp
                LEFT JOIN lives l ON l.id = lp.live_id
                WHERE lp.id = ?
                """,
                (process_id,),
                fetch_one=True,
                operation_name="get_process_live_identity",
            )
            if not row or row[0] is None:
                return None
            return {
                'live_id': row[0],
                'tiktok_stream_id': row[1],
                'tiktok_started_at': row[2],
            }
        except Exception as error:
            self.logger.error(
                f"Failed to get live identity for process {process_id}: {error}"
            )
            return None

    def link_process_live_session(self, process_id: int, live_id: int) -> bool:
        """Link an already registered recorder to its recorder-owned lives row."""
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE live_processes SET live_id = ? WHERE id = ?",
                (live_id, process_id),
            )
            updated = cursor.rowcount == 1
            conn.commit()
            conn.close()
            return updated
        except Exception as error:
            self.logger.error(
                f"Failed to link process {process_id} to live {live_id}: {error}"
            )
            return False

    def close_process_live_session(self, process_id: int) -> bool:
        """Idempotently close only the lives row linked to one process."""
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()
            ended_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """
                UPDATE lives
                SET ended_at = COALESCE(ended_at, ?)
                WHERE id = (
                    SELECT live_id FROM live_processes WHERE id = ?
                )
                """,
                (ended_at, process_id),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as error:
            self.logger.error(
                f"Failed to close live session for process {process_id}: {error}"
            )
            return False

    def update_health_status(self, process_id: int, health_status: str, file_size_bytes: int = 0) -> bool:
        """
        Update health status and file size for a process

        Args:
            process_id: Database process ID
            health_status: New health status ('healthy', 'unhealthy', 'restarting', 'stopped',
                          'stopped_user_offline', 'stopped_max_retries')
            file_size_bytes: Current file size in bytes

        Returns:
            True if update successful, False otherwise
        """
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE live_processes
                SET health_status = ?, file_size_bytes = ?, last_health_check = ?
                WHERE id = ?
            """, (health_status, file_size_bytes, datetime.now(), process_id))

            updated = cursor.rowcount > 0
            conn.commit()
            conn.close()

            if updated:
                # Format file size for logging
                if file_size_bytes > 0:
                    size_mb = file_size_bytes / (1024 * 1024)
                    if size_mb >= 1024:
                        size_str = f"{size_mb / 1024:.2f} GB"
                    else:
                        size_str = f"{size_mb:.1f} MB"
                    self.logger.debug(f"Updated health status for process {process_id}: {health_status} ({size_str})")
                else:
                    self.logger.debug(f"Updated health status for process {process_id}: {health_status}")
            else:
                self.logger.debug(f"No process found with ID {process_id} to update")

            return updated

        except Exception as e:
            self.logger.error(f"Failed to update health status for process {process_id}: {e}")
            return False

    def increment_restart_count(self, process_id: int) -> int:
        """
        Increment restart count for a process

        Args:
            process_id: Database process ID

        Returns:
            New restart count, or -1 if failed
        """
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE live_processes
                SET restart_count = restart_count + 1
                WHERE id = ?
            """, (process_id,))

            # Get new restart count
            cursor.execute("SELECT restart_count FROM live_processes WHERE id = ?", (process_id,))
            row = cursor.fetchone()

            if row:
                new_count = row[0]
                conn.commit()
                conn.close()
                self.logger.debug(f"Incremented restart count for process {process_id}: {new_count}")
                return new_count
            else:
                conn.close()
                self.logger.debug(f"No process found with ID {process_id} to increment restart count")
                return -1

        except Exception as e:
            self.logger.error(f"Failed to increment restart count for process {process_id}: {e}")
            return -1

    def update_process_pid(self, process_id: int, new_pid: int, reset_restart_count: bool = False) -> bool:
        """
        Update PID for a process (used after restart)

        Args:
            process_id: Database process ID
            new_pid: New process PID
            reset_restart_count: Whether to reset restart count

        Returns:
            True if update successful, False otherwise
        """
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            if reset_restart_count:
                cursor.execute("""
                    UPDATE live_processes
                    SET pid = ?, started_at = ?, restart_count = 0, health_status = 'healthy'
                    WHERE id = ?
                """, (new_pid, datetime.now(), process_id))
            else:
                cursor.execute("""
                    UPDATE live_processes
                    SET pid = ?, started_at = ?, health_status = 'healthy'
                    WHERE id = ?
                """, (new_pid, datetime.now(), process_id))

            updated = cursor.rowcount > 0
            conn.commit()
            conn.close()

            if updated:
                self.logger.info(f"Updated PID for process {process_id}: {new_pid}")
            else:
                self.logger.debug(f"No process found with ID {process_id} to update PID")

            return updated

        except Exception as e:
            self.logger.error(f"Failed to update PID for process {process_id}: {e}")
            return False

    def update_process_after_restart(self, process_id: int, new_pid: int, new_recording_file_path: str) -> bool:
        """
        Update process info after restart (PID + recording file path)

        Args:
            process_id: Database process ID
            new_pid: New process PID
            new_recording_file_path: New recording file path

        Returns:
            True if update successful, False otherwise
        """
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE live_processes
                SET pid = ?, recording_file_path = ?, started_at = ?, health_status = 'healthy'
                WHERE id = ?
            """, (new_pid, new_recording_file_path, datetime.now(), process_id))

            updated = cursor.rowcount > 0
            conn.commit()
            conn.close()

            if updated:
                self.logger.info(f"Updated process {process_id} after restart: PID={new_pid}, file={new_recording_file_path}")
            else:
                self.logger.debug(f"No process found with ID {process_id} to update after restart")

            return updated

        except Exception as e:
            self.logger.error(f"Failed to update process {process_id} after restart: {e}")
            return False

    def mark_process_stopped(self, process_id: int, health_status: str = 'stopped') -> bool:
        """
        Mark a process as stopped and inactive

        Args:
            process_id: Database process ID
            health_status: Final status to preserve the stop reason

        Returns:
            True if update successful, False otherwise
        """
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE live_processes
                SET health_status = ?, is_active = 0, last_health_check = ?
                WHERE id = ?
            """, (health_status, datetime.now(), process_id))

            updated = cursor.rowcount > 0
            conn.commit()
            conn.close()

            if updated:
                self.logger.info(f"Marked process {process_id} as {health_status}")
            else:
                self.logger.warning(f"No process found with ID {process_id} to mark as stopped")

            return updated

        except Exception as e:
            self.logger.error(f"Failed to mark process {process_id} as stopped: {e}")
            return False

    def get_active_processes(self) -> List[ProcessInfo]:
        """
        Get all active live recording processes

        Returns:
            List of ProcessInfo objects for active processes
        """
        try:
            rows = self.execute_query("""
                SELECT id, user_id, username, pid, recording_file_path, started_at,
                       last_health_check, health_status, restart_count, file_size_bytes,
                       is_active, room_id
                FROM live_processes
                WHERE is_active = 1
                ORDER BY started_at ASC
            """, operation_name="get_active_processes")

            processes = self.process_factory.create_multiple_from_rows(rows)
            # Only log significant operations
            return processes

        except Exception as e:
            self.logger.error(f"Failed to get active processes: {e}")
            return []

    def get_process_by_username(self, username: str) -> Optional[ProcessInfo]:
        """
        Get active process for a specific username

        Args:
            username: TikTok username

        Returns:
            ProcessInfo if found, None otherwise
        """
        try:
            # Use UserContextLogger for this operation
            user_logger = UserContextLogger(self.logger, username)

            # Manual query execution with user context logging
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            query = """
                SELECT id, user_id, username, pid, recording_file_path, started_at,
                       last_health_check, health_status, restart_count, file_size_bytes,
                       is_active, room_id
                FROM live_processes
                WHERE username = ? AND is_active = 1
                ORDER BY started_at DESC
                LIMIT 1
            """
            params = (username,)

            user_logger.debug(f"Executing get_process_by_username with params: {params}")
            cursor.execute(query, params)
            row = cursor.fetchone()

            if not row:
                user_logger.debug("No active process found")

            conn.close()

            if row:
                process_info = process_info_factory.from_database_row(row)
                user_logger.debug(f"Found active process: PID={process_info.pid}")
                return process_info
            else:
                user_logger.debug("No active process found")
                return None

        except Exception as e:
            user_logger = UserContextLogger(self.logger, username) if 'user_logger' not in locals() else user_logger
            user_logger.error(f"Failed to get process: {e}")
            return None

    def get_process_by_id(self, process_id: int) -> Optional[ProcessInfo]:
        """
        Get process by database ID

        Args:
            process_id: Database process ID

        Returns:
            ProcessInfo if found, None otherwise
        """
        try:
            row = self.execute_query("""
                SELECT id, user_id, username, pid, recording_file_path, started_at,
                       last_health_check, health_status, restart_count, file_size_bytes,
                       is_active, room_id
                FROM live_processes
                WHERE id = ?
            """, (process_id,), fetch_one=True, operation_name="get_process_by_id")

            if row:
                return process_info_factory.from_database_row(row)
            else:
                return None

        except Exception as e:
            self.logger.error(f"Failed to get process {process_id}: {e}")
            return None

    def cleanup_orphaned_processes(self, active_pids: List[int]) -> int:
        """
        Clean up processes that are marked as active but no longer running

        Args:
            active_pids: List of currently running PIDs

        Returns:
            Number of orphaned processes cleaned up
        """
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            # Get all active processes
            cursor.execute("""
                SELECT id, pid, username FROM live_processes
                WHERE is_active = 1 AND health_status != 'stopped'
            """)

            orphaned_count = 0
            for row in cursor.fetchall():
                process_id, pid, username = row

                if pid not in active_pids:
                    # Process is orphaned - mark as stopped
                    cursor.execute("""
                        UPDATE live_processes
                        SET health_status = 'stopped', is_active = 0, last_health_check = ?
                        WHERE id = ?
                    """, (datetime.now(), process_id))

                    orphaned_count += 1
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.debug(f"Cleaned up orphaned process (PID={pid}, DB_ID={process_id})")

            conn.commit()
            conn.close()

            if orphaned_count > 0:
                self.logger.info(f"Cleaned up {orphaned_count} orphaned processes")

            return orphaned_count

        except Exception as e:
            self.logger.error(f"Failed to cleanup orphaned processes: {e}")
            return 0

    def cleanup_dead_database_entries(self) -> int:
        """
        Clean up database entries for processes that are marked as active
        but have been determined to be dead (includes zombies)

        Returns:
            Number of dead entries cleaned up
        """
        try:
            import psutil

            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            # Get all active processes
            cursor.execute("""
                SELECT id, pid, username FROM live_processes
                WHERE is_active = 1
            """)

            dead_count = 0
            for row in cursor.fetchall():
                process_id, pid, username = row

                is_dead = False

                # Check if process is dead or zombie
                try:
                    if not psutil.pid_exists(pid):
                        is_dead = True
                        user_logger = UserContextLogger(self.logger, username)
                        user_logger.debug(f"Process (PID={pid}) does not exist")
                    else:
                        # Check if it's a zombie
                        try:
                            process = psutil.Process(pid)
                            if process.status() == psutil.STATUS_ZOMBIE:
                                is_dead = True
                                user_logger = UserContextLogger(self.logger, username)
                                user_logger.debug(f"Process (PID={pid}) is a zombie")
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            # If we can't access process info, check via /proc
                            try:
                                with open(f"/proc/{pid}/stat", "r") as f:
                                    stat_data = f.read().strip().split()
                                    if len(stat_data) > 2 and stat_data[2] == 'Z':
                                        is_dead = True
                                        user_logger = UserContextLogger(self.logger, username)
                                        user_logger.debug(f"Process (PID={pid}) is a zombie (via /proc)")
                            except (FileNotFoundError, PermissionError, IndexError):
                                pass

                except Exception as e:
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.error(f"Error checking process (PID={pid}): {e}")
                    continue

                if is_dead:
                    # Mark as stopped and inactive
                    cursor.execute("""
                        UPDATE live_processes
                        SET health_status = 'stopped', is_active = 0, last_health_check = ?
                        WHERE id = ?
                    """, (datetime.now(), process_id))

                    # Reset is_live status for this user since process is dead
                    self.reset_user_live_status(username)

                    dead_count += 1
                    user_logger = UserContextLogger(self.logger, username)
                    user_logger.warning(f"Cleaned up dead database entry (PID={pid}, DB_ID={process_id})")

            conn.commit()
            conn.close()

            if dead_count > 0:
                self.logger.info(f"Cleaned up {dead_count} dead database entries")

            return dead_count

        except Exception as e:
            self.logger.error(f"Failed to cleanup dead database entries: {e}")
            return 0

    def update_recording_file_path(self, process_id: int, file_path: str) -> bool:
        """
        Update the recording file path for a process

        Args:
            process_id: Database process ID
            file_path: New recording file path

        Returns:
            True if update successful, False otherwise
        """
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE live_processes
                SET recording_file_path = ?
                WHERE id = ?
            """, (file_path, process_id))

            updated = cursor.rowcount > 0
            conn.commit()
            conn.close()

            if updated:
                self.logger.debug(f"Updated recording file path for process {process_id}: {file_path}")

            return updated

        except Exception as e:
            self.logger.error(f"Failed to update recording file path for process {process_id}: {e}")
            return False

    def get_statistics(self) -> Dict[str, int]:
        """
        Get statistics about live processes

        Returns:
            Dictionary with process statistics
        """
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            stats = {}

            # Total active processes
            cursor.execute("SELECT COUNT(*) FROM live_processes WHERE is_active = 1")
            stats['active_processes'] = cursor.fetchone()[0]

            # Processes by health status
            cursor.execute("""
                SELECT health_status, COUNT(*) FROM live_processes
                WHERE is_active = 1
                GROUP BY health_status
            """)
            for row in cursor.fetchall():
                status, count = row
                stats[f'processes_{status}'] = count

            # Total processes ever created
            cursor.execute("SELECT COUNT(*) FROM live_processes")
            stats['total_processes_created'] = cursor.fetchone()[0]

            # Processes with restart count > 0
            cursor.execute("SELECT COUNT(*) FROM live_processes WHERE restart_count > 0")
            stats['processes_restarted'] = cursor.fetchone()[0]

            conn.close()
            return stats

        except Exception as e:
            self.logger.error(f"Failed to get statistics: {e}")
            return {}

    def reset_user_live_status(self, username: str) -> bool:
        """
        Reset user's is_live status to 0 in users table

        Args:
            username: Username to reset

        Returns:
            True if successful, False otherwise
        """
        try:
            user_logger = UserContextLogger(self.logger, username)
            user_logger.info("[DEBUG] health_monitor resetting live status")
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            # Check current status before update
            cursor.execute("SELECT is_live FROM users WHERE username = ?", (username,))
            current_row = cursor.fetchone()
            current_status = current_row['is_live'] if current_row else None
            user_logger.info(f"[DEBUG] Current is_live status: {current_status}")

            ended_at_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                UPDATE lives
                SET ended_at = ?
                WHERE user_id = (SELECT id FROM users WHERE username = ?)
                  AND ended_at IS NULL
            """, (ended_at_str, username))
            closed_sessions = cursor.rowcount
            user_logger.info(f"[DEBUG] Closed active lives rows: {closed_sessions}")

            cursor.execute("""
                UPDATE users
                SET is_live = 0
                WHERE username = ? AND is_live = 1
            """, (username,))

            rows_affected = cursor.rowcount
            user_logger.info(f"[DEBUG] UPDATE affected {rows_affected} rows")

            if rows_affected == 0:
                user_logger.info("[DEBUG] Race condition detected: user was already offline (recorder completed normally)")
            elif rows_affected == 1:
                user_logger.debug("Process ended: user stream completed while marked as live")
            else:
                user_logger.error(f"[DEBUG] Unexpected rows affected: {rows_affected}")

            conn.commit()
            user_logger.info("[DEBUG] health_monitor commit completed")
            conn.close()

            return rows_affected > 0 or closed_sessions > 0

        except Exception as e:
            user_logger = UserContextLogger(self.logger, username) if 'user_logger' not in locals() else user_logger
            user_logger.error(f"Failed to reset is_live: {e}")
            return False

    def is_user_live(self, username: str) -> bool:
        """
        Check if user is currently marked as live

        Args:
            username: Username to check

        Returns:
            True if user is live, False otherwise
        """
        try:
            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            cursor.execute("SELECT is_live FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            conn.close()

            return bool(row and row['is_live'] in (1, 2)) if row else False  # Consider both live (1) and starting (2) as live

        except Exception as e:
            user_logger = UserContextLogger(self.logger, username)
            user_logger.error(f"Failed to check is_live status: {e}")
            return False

    def remove_process(self, process_id: int, username: str = None) -> bool:
        """
        Remove a process from the database (used for failed/invalid processes)

        Args:
            process_id: Database ID of the process to remove
            username: Username for logging (optional)

        Returns:
            True if process was removed, False otherwise
        """
        try:
            user_logger = UserContextLogger(self.logger, username) if username else self.logger

            conn = create_connection(self.db_path)
            cursor = conn.cursor()

            # First check if process exists
            cursor.execute("SELECT username, pid FROM live_processes WHERE id = ?", (process_id,))
            row = cursor.fetchone()

            if not row:
                user_logger.warning(f"Process {process_id} not found for removal")
                conn.close()
                return False

            _process_username = row[0]
            process_pid = row[1]

            # Remove the process
            cursor.execute("DELETE FROM live_processes WHERE id = ?", (process_id,))
            rows_affected = cursor.rowcount

            if rows_affected > 0:
                conn.commit()
                user_logger.info(f"Removed failed process: PID={process_pid}, DB_ID={process_id}")

            conn.close()
            return rows_affected > 0

        except Exception as e:
            user_logger = UserContextLogger(self.logger, username) if username else self.logger
            user_logger.error(f"Failed to remove process {process_id}: {e}")
            return False
