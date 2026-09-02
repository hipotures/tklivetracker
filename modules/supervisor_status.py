"""
Supervisor Status Management Module

Handles supervisor heartbeat updates and status tracking in the database.
"""

import json
import os
import hashlib
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from .db_setup import create_connection

logger = logging.getLogger('STAT')

class SupervisorStatusManager:
    """Manages supervisor status tracking and heartbeat updates"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.pid = os.getpid()
        self.version = self._get_version()
        self.config_hash = self._calculate_config_hash()
        self.stats = {
            'live_detections': 0,
            'recording_starts': 0,
            'recording_stops': 0,
            'health_checks': 0,
            'restarts': 0
        }

    def _get_version(self) -> str:
        """Get supervisor version information"""
        try:
            # Try to get git commit hash
            import subprocess
            result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                                 capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return f"git-{result.stdout.strip()}"
        except:
            pass
        return "unknown"

    def _calculate_config_hash(self) -> str:
        """Calculate hash of config for change detection"""
        config_str = json.dumps(self.config, sort_keys=True)
        return hashlib.md5(
            config_str.encode(), usedforsecurity=False
        ).hexdigest()[:8]

    def start_supervisor(self) -> None:
        """Record supervisor startup in database"""
        try:
            db_path = self.config.get('database', {}).get('path', './db.sqlite')
            conn = create_connection(db_path)
            cursor = conn.cursor()

            # Clear any existing entries for this supervisor
            cursor.execute("DELETE FROM supervisor_status WHERE pid = ?", (self.pid,))

            # Insert new startup record
            cursor.execute("""
                INSERT INTO supervisor_status
                (pid, started_at, last_heartbeat, status, version, stats_json, config_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                self.pid,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'active',
                self.version,
                json.dumps(self.stats),
                self.config_hash
            ))

            conn.commit()
            conn.close()
            logger.info(f"✅ Supervisor status initialized (PID: {self.pid}, Version: {self.version})")

        except Exception as e:
            logger.error(f"❌ Failed to initialize supervisor status: {e}")

    def update_heartbeat(self, additional_stats: Optional[Dict[str, Any]] = None) -> None:
        """Update supervisor heartbeat and statistics"""
        try:
            # Update internal stats if provided
            if additional_stats:
                self.stats.update(additional_stats)

            db_path = self.config.get('database', {}).get('path', './db.sqlite')
            conn = create_connection(db_path)
            cursor = conn.cursor()

            # Update heartbeat for this PID
            cursor.execute("""
                UPDATE supervisor_status
                SET last_heartbeat = ?, stats_json = ?, status = ?
                WHERE pid = ?
            """, (
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                json.dumps(self.stats),
                'active',
                self.pid
            ))

            conn.commit()
            conn.close()
            logger.debug(f"💓 Supervisor heartbeat updated (PID: {self.pid})")

        except Exception as e:
            logger.error(f"❌ Failed to update supervisor heartbeat: {e}")

    def stop_supervisor(self) -> None:
        """Record supervisor shutdown in database"""
        try:
            db_path = self.config.get('database', {}).get('path', './db.sqlite')
            conn = create_connection(db_path)
            cursor = conn.cursor()

            # Update status to stopped
            cursor.execute("""
                UPDATE supervisor_status
                SET status = ?, last_heartbeat = ?
                WHERE pid = ?
            """, (
                'stopped',
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                self.pid
            ))

            conn.commit()
            conn.close()
            logger.info(f"⏹️ Supervisor status set to stopped (PID: {self.pid})")

        except Exception as e:
            logger.error(f"❌ Failed to update supervisor stop status: {e}")

    def increment_stat(self, stat_name: str, count: int = 1) -> None:
        """Increment a statistic counter"""
        if stat_name in self.stats:
            self.stats[stat_name] += count

    @staticmethod
    def get_supervisor_status(config: Dict[str, Any]) -> Dict[str, Any]:
        """Get current supervisor status from database"""
        try:
            db_path = config.get('database', {}).get('path', './db.sqlite')
            conn = create_connection(db_path)
            cursor = conn.cursor()

            # Get the most recent supervisor status
            cursor.execute("""
                SELECT pid, started_at, last_heartbeat, status, version, stats_json, config_hash
                FROM supervisor_status
                ORDER BY last_heartbeat DESC
                LIMIT 1
            """)

            row = cursor.fetchone()
            conn.close()

            if not row:
                return {
                    'status': 'unknown',
                    'message': 'No supervisor status found',
                    'last_seen': None
                }

            # Calculate time since last heartbeat
            now = datetime.now()
            # Handle both string and datetime formats
            if isinstance(row['last_heartbeat'], str):
                last_heartbeat = datetime.strptime(row['last_heartbeat'], '%Y-%m-%d %H:%M:%S')
            else:
                last_heartbeat = row['last_heartbeat']
            minutes_ago = int((now - last_heartbeat).total_seconds() / 60)

            # Determine status based on heartbeat age
            if minutes_ago < 3:
                status = 'connected'
            elif minutes_ago < 5:
                status = 'warning'
            else:
                status = 'disconnected'

            # Parse stats
            try:
                stats = json.loads(row['stats_json']) if row['stats_json'] else {}
            except:
                stats = {}

            # Convert datetime objects to strings for JSON serialization
            started_at_str = row['started_at']
            if not isinstance(started_at_str, str):
                started_at_str = started_at_str.strftime('%Y-%m-%d %H:%M:%S') if started_at_str else None

            last_heartbeat_str = row['last_heartbeat']
            if not isinstance(last_heartbeat_str, str):
                last_heartbeat_str = last_heartbeat_str.strftime('%Y-%m-%d %H:%M:%S') if last_heartbeat_str else None

            return {
                'status': status,
                'pid': row['pid'],
                'started_at': started_at_str,
                'last_heartbeat': last_heartbeat_str,
                'supervisor_status': row['status'],
                'version': row['version'],
                'config_hash': row['config_hash'],
                'stats': stats,
                'minutes_ago': minutes_ago,
                'message': f'Last seen {minutes_ago} minutes ago' if minutes_ago > 0 else 'Active now'
            }

        except Exception as e:
            logger.error(f"❌ Failed to get supervisor status: {e}")
            return {
                'status': 'error',
                'message': f'Database error: {str(e)}',
                'last_seen': None
            }
