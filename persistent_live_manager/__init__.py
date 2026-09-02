"""
Persistent Live Manager Package

A comprehensive system for managing persistent TikTok live recording processes
that operate independently of the supervisor process.

Components:
- ProcessMetadataStore: Database management for process metadata
- RecordingHealthMonitor: Health monitoring and automatic recovery
- LiveProcessManager: Process lifecycle management
- GracefulShutdownHandler: Safe shutdown procedures
"""

from .process_metadata_store import ProcessMetadataStore
from .recording_health_monitor import RecordingHealthMonitor
from .live_process_manager import LiveProcessManager
from .graceful_shutdown_handler import GracefulShutdownHandler

__version__ = "1.0.0"
__author__ = "TikTok Tracker Persistent System"

__all__ = [
    "ProcessMetadataStore",
    "RecordingHealthMonitor",
    "LiveProcessManager",
    "GracefulShutdownHandler"
]