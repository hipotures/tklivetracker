"""
Supervisor Lock Management

Prevents multiple supervisor instances from running in --server mode.
CLI operations are not affected by the lock file.
"""

import os
import json
import logging
from typing import Dict, Optional, Tuple
from datetime import datetime
from pathlib import Path


class SupervisorLock:
    """
    Manages lock file for supervisor --server mode

    Prevents multiple supervisor daemons from running simultaneously
    while allowing CLI operations to work normally.
    """

    def __init__(self, lock_file_path: str):
        self.lock_file_path = Path(lock_file_path)
        self.logger = logging.getLogger('LOCK')
        self.current_lock_data: Optional[Dict] = None

    def check_server_lock(self) -> Tuple[bool, Optional[Dict]]:
        """
        Check if supervisor lock file exists and is valid

        Returns:
            Tuple of (lock_exists, lock_data)
            - lock_exists: True if valid lock exists, False otherwise
            - lock_data: Lock file content if exists, None otherwise
        """
        try:
            if not self.lock_file_path.exists():
                return False, None

            # Read lock file
            with open(self.lock_file_path, 'r') as f:
                lock_data = json.load(f)

            # Validate lock file structure
            required_fields = ['pid', 'started_at', 'config_path', 'hostname']
            if not all(field in lock_data for field in required_fields):
                self.logger.warning(f"Invalid lock file structure, removing: {self.lock_file_path}")
                self._remove_lock_file()
                return False, None

            # Check if process is still running
            pid = lock_data['pid']
            if not self._is_process_running(pid):
                self.logger.info(f"Found stale lock file (process {pid} no longer running)")
                self.logger.info(f"Removing stale lock: {self.lock_file_path}")
                self._remove_lock_file()
                return False, None

            # Lock is valid and process is running
            return True, lock_data

        except Exception as e:
            self.logger.error(f"Error checking lock file: {e}")
            # If we can't read the lock file, try to remove it and continue
            try:
                self._remove_lock_file()
            except:
                pass
            return False, None

    def create_server_lock(self) -> bool:
        """
        Create lock file for current supervisor instance

        Returns:
            True if lock created successfully, False otherwise
        """
        try:
            # Ensure directory exists
            self.lock_file_path.parent.mkdir(parents=True, exist_ok=True)

            # Create lock data
            import socket
            lock_data = {
                'pid': os.getpid(),
                'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'config_path': os.path.abspath('config.yaml'),  # Default, can be updated
                'hostname': socket.gethostname(),
                'version': self._get_version()
            }

            # Write lock file
            with open(self.lock_file_path, 'w') as f:
                json.dump(lock_data, f, indent=2)

            self.current_lock_data = lock_data
            self.logger.debug(f"Created supervisor lock file: {self.lock_file_path}")

            # Note: Signal handlers are managed by the main supervisor

            return True

        except Exception as e:
            self.logger.error(f"Failed to create lock file: {e}")
            return False

    def remove_server_lock(self) -> bool:
        """
        Remove supervisor lock file

        Returns:
            True if removed successfully, False otherwise
        """
        try:
            return self._remove_lock_file()
        except Exception as e:
            self.logger.error(f"Error removing lock file: {e}")
            return False

    def update_config_path(self, config_path: str) -> None:
        """Update config path in current lock data"""
        if self.current_lock_data:
            self.current_lock_data['config_path'] = os.path.abspath(config_path)
            try:
                with open(self.lock_file_path, 'w') as f:
                    json.dump(self.current_lock_data, f, indent=2)
            except Exception as e:
                self.logger.warning(f"Failed to update config path in lock file: {e}")

    def _remove_lock_file(self) -> bool:
        """Internal method to remove lock file"""
        try:
            if self.lock_file_path.exists():
                self.lock_file_path.unlink()
                self.logger.debug(f"Removed lock file: {self.lock_file_path}")
            self.current_lock_data = None
            return True
        except Exception as e:
            self.logger.error(f"Failed to remove lock file: {e}")
            return False

    def _is_process_running(self, pid: int) -> bool:
        """Check if process with given PID is running"""
        try:
            # Send signal 0 to check if process exists
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _get_version(self) -> str:
        """Get supervisor version information"""
        try:
            import subprocess
            result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                                 capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return f"git-{result.stdout.strip()}"
        except:
            pass
        return "unknown"

    def cleanup_on_signal(self, signum: int) -> None:
        """Called by main supervisor when signal is received"""
        self.logger.debug(f"Signal {signum} received, removing lock file")
        self._remove_lock_file()

    def get_lock_info_message(self, lock_data: Dict) -> str:
        """
        Generate user-friendly message about existing lock

        Args:
            lock_data: Lock file content

        Returns:
            Formatted message string
        """
        pid = lock_data.get('pid', 'unknown')
        started_at = lock_data.get('started_at', 'unknown')
        hostname = lock_data.get('hostname', 'unknown')
        config_path = lock_data.get('config_path', 'unknown')

        message = f"""❌ Error: Another supervisor instance is already running in --server mode
   Lock file: {self.lock_file_path}
   Process PID: {pid}, started: {started_at}
   Hostname: {hostname}
   Config: {config_path}

   If you're sure no supervisor is running, delete the lock file:
   rm {self.lock_file_path}"""

        return message