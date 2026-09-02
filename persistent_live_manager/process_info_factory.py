"""
Process Info Factory

Factory class for creating ProcessInfo objects from database rows.
Eliminates code duplication across process metadata operations.
"""

import logging
from typing import Tuple, Any
from .process_info import ProcessInfo


class ProcessInfoFactory:
    """
    Factory for creating ProcessInfo objects from database rows

    Centralizes the creation logic that was duplicated across multiple methods
    in ProcessMetadataStore, reducing code duplication and improving maintainability.
    """

    def __init__(self):
        self.logger = logging.getLogger('PINF')

    @staticmethod
    def from_database_row(row: Tuple[Any, ...]) -> ProcessInfo:
        """
        Create ProcessInfo object from database row

        Args:
            row: Database row tuple with process information
                 Expected format: (id, user_id, username, pid, recording_file_path,
                                 started_at, last_health_check, health_status,
                                 restart_count, file_size_bytes, is_active, room_id)

        Returns:
            ProcessInfo object populated with row data

        Raises:
            ValueError: If row format is invalid
            TypeError: If row data types are incompatible
        """
        if not row or len(row) < 12:
            raise ValueError(f"Invalid database row: expected 12 fields, got {len(row) if row else 0}")

        try:
            return ProcessInfo(
                id=row[0],
                user_id=row[1],
                username=row[2],
                pid=row[3],
                recording_file_path=row[4],
                started_at=row[5],
                last_health_check=row[6],
                health_status=row[7],
                restart_count=row[8],
                file_size_bytes=row[9],
                is_active=bool(row[10]),
                room_id=row[11]
            )
        except (TypeError, ValueError) as e:
            raise TypeError(f"Failed to create ProcessInfo from row data: {e}") from e

    @staticmethod
    def from_database_dict(row_dict: dict) -> ProcessInfo:
        """
        Create ProcessInfo object from database row dictionary

        Args:
            row_dict: Dictionary with database row data (sqlite3.Row format)

        Returns:
            ProcessInfo object populated with dictionary data

        Raises:
            KeyError: If required fields are missing
            TypeError: If data types are incompatible
        """
        required_fields = [
            'id', 'user_id', 'username', 'pid', 'recording_file_path',
            'started_at', 'last_health_check', 'health_status',
            'restart_count', 'file_size_bytes', 'is_active', 'room_id'
        ]

        # Check for required fields
        missing_fields = [field for field in required_fields if field not in row_dict]
        if missing_fields:
            raise KeyError(f"Missing required fields in row dictionary: {missing_fields}")

        try:
            return ProcessInfo(
                id=row_dict['id'],
                user_id=row_dict['user_id'],
                username=row_dict['username'],
                pid=row_dict['pid'],
                recording_file_path=row_dict['recording_file_path'],
                started_at=row_dict['started_at'],
                last_health_check=row_dict['last_health_check'],
                health_status=row_dict['health_status'],
                restart_count=row_dict['restart_count'],
                file_size_bytes=row_dict['file_size_bytes'],
                is_active=bool(row_dict['is_active']),
                room_id=row_dict['room_id']
            )
        except (TypeError, ValueError) as e:
            raise TypeError(f"Failed to create ProcessInfo from dictionary: {e}") from e

    def create_multiple_from_rows(self, rows: list) -> list[ProcessInfo]:
        """
        Create multiple ProcessInfo objects from database rows

        Args:
            rows: List of database row tuples

        Returns:
            List of ProcessInfo objects

        Note:
            Invalid rows are logged as warnings and skipped
        """
        processes = []

        for i, row in enumerate(rows):
            try:
                process_info = self.from_database_row(row)
                processes.append(process_info)
            except (ValueError, TypeError) as e:
                self.logger.warning(f"Skipping invalid row {i}: {e}")
                continue

        # Only log if there are issues (warnings already logged above)
        return processes

    @staticmethod
    def validate_process_info(process_info: ProcessInfo) -> bool:
        """
        Validate ProcessInfo object has required fields

        Args:
            process_info: ProcessInfo object to validate

        Returns:
            True if valid, False otherwise
        """
        if not process_info:
            return False

        # Check required fields are not None/empty
        if not process_info.username or not process_info.username.strip():
            return False

        if process_info.pid is None or process_info.pid <= 0:
            return False

        if not process_info.health_status:
            return False

        return True


# Convenience instance for importing
process_info_factory = ProcessInfoFactory()