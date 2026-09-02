"""
Process Info Data Class

Data class representing live process information.
Separated to avoid circular imports.
"""

from datetime import datetime
from dataclasses import dataclass
from typing import Optional


@dataclass
class ProcessInfo:
    """Data class representing live process information"""
    id: Optional[int]
    user_id: int
    username: str
    pid: int
    recording_file_path: Optional[str]
    started_at: datetime
    last_health_check: Optional[datetime]
    health_status: str  # 'healthy', 'unhealthy', 'restarting', 'stopped'
    restart_count: int
    file_size_bytes: int
    is_active: bool
    room_id: Optional[str]