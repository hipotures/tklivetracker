"""
User Data Transformer

Standardizes user data processing and transformation across web API endpoints.
Eliminates code duplication in user data mapping and formatting.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime


class UserDataTransformer:
    """
    Centralized transformer for user data objects

    Handles consistent user data mapping, validation, and formatting
    across different API endpoints and database operations.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def calculate_live_duration(started_at: Any) -> Optional[int]:
        """
        Calculate how long a user has been live

        Args:
            started_at: Start time (string or datetime object)

        Returns:
            Duration in seconds or None if invalid
        """
        if not started_at:
            return None

        try:
            if isinstance(started_at, str):
                start_time = datetime.strptime(started_at.split('.')[0], "%Y-%m-%d %H:%M:%S")
            else:
                start_time = started_at

            duration = datetime.now() - start_time
            return int(duration.total_seconds())

        except (ValueError, TypeError, AttributeError) as e:
            logging.getLogger(__name__).warning(f"Error calculating live duration: {e}")
            return None

    @staticmethod
    def calculate_days_since_last_live(last_live_at: Any) -> int:
        """
        Calculate full days since the latest known live timestamp.

        Returns -1 when there is no usable live history.
        """
        if not last_live_at:
            return -1

        try:
            if isinstance(last_live_at, str):
                live_time = datetime.strptime(last_live_at.split('.')[0], "%Y-%m-%d %H:%M:%S")
            else:
                live_time = last_live_at

            elapsed = datetime.now() - live_time
            return max(0, elapsed.days)

        except (ValueError, TypeError, AttributeError) as e:
            logging.getLogger(__name__).warning(f"Error calculating days since last live: {e}")
            return -1

    def transform_user_row(self, row: Any, include_live_duration: bool = True) -> Dict[str, Any]:
        """
        Transform a database user row into standardized user data object

        Args:
            row: Database row (sqlite3.Row or tuple)
            include_live_duration: Whether to calculate live duration

        Returns:
            Standardized user data dictionary
        """
        try:
            # Handle both sqlite3.Row and tuple formats
            if hasattr(row, 'keys'):  # sqlite3.Row
                # Use direct key access with defaults for sqlite3.Row
                user_data = {
                    'username': row['username'] if 'username' in row.keys() else '',
                    'check_interval': row['check_interval'] if 'check_interval' in row.keys() else 300,
                    'is_live': bool(row['is_live']) if 'is_live' in row.keys() else False,
                    'is_active': bool(row['is_active']) if 'is_active' in row.keys() else True,
                    'total_lives': row['total_lives'] if 'total_lives' in row.keys() else 0,
                    'next_check': row['next_check'] if 'next_check' in row.keys() else None,
                    'added_at': row['added_at'] if 'added_at' in row.keys() else None,
                    'is_favorite': bool(row['is_favorite']) if 'is_favorite' in row.keys() else False,
                    'notifications_enabled': bool(row['notifications_enabled']) if 'notifications_enabled' in row.keys() else False,
                    'live_duration': None,
                    'last_live_days_ago': self.calculate_days_since_last_live(row['last_live_at']) if 'last_live_at' in row.keys() else -1
                }

                # Add live duration if user is live and we have started_at
                if (include_live_duration and
                    user_data['is_live'] and
                    'live_started_at' in row.keys() and
                    row['live_started_at']):
                    user_data['live_duration'] = self.calculate_live_duration(row['live_started_at'])

            else:  # Tuple format
                user_data = {
                    'username': row[0] if len(row) > 0 else '',
                    'check_interval': row[1] if len(row) > 1 else 300,
                    'is_live': bool(row[2]) if len(row) > 2 else False,
                    'is_active': bool(row[3]) if len(row) > 3 else True,
                    'total_lives': row[4] if len(row) > 4 else 0,
                    'next_check': row[5] if len(row) > 5 else None,
                    'added_at': row[6] if len(row) > 6 else None,
                    'is_favorite': bool(row[7]) if len(row) > 7 else False,
                    'notifications_enabled': bool(row[8]) if len(row) > 8 else False,
                    'live_duration': None,
                    'last_live_days_ago': self.calculate_days_since_last_live(row[10]) if len(row) > 10 else -1
                }

                # Add live duration if available
                if (include_live_duration and
                    user_data['is_live'] and
                    len(row) > 9 and row[9]):
                    user_data['live_duration'] = self.calculate_live_duration(row[9])

            return user_data

        except (IndexError, KeyError, TypeError) as e:
            self.logger.error(f"Error transforming user row: {e}")
            # Return minimal valid user data structure
            return {
                'username': 'unknown',
                'check_interval': 300,
                'is_live': False,
                'is_active': True,
                'total_lives': 0,
                'next_check': None,
                'added_at': None,
                'is_favorite': False,
                'notifications_enabled': False,
                'live_duration': None,
                'last_live_days_ago': -1
            }

    def transform_multiple_user_rows(self, rows: List[Any], include_live_duration: bool = True) -> List[Dict[str, Any]]:
        """
        Transform multiple database user rows into standardized user data objects

        Args:
            rows: List of database rows
            include_live_duration: Whether to calculate live duration

        Returns:
            List of standardized user data dictionaries
        """
        users = []
        for row in rows:
            try:
                user_data = self.transform_user_row(row, include_live_duration)
                users.append(user_data)
            except Exception as e:
                self.logger.warning(f"Skipping invalid user row: {e}")
                continue

        self.logger.debug(f"Transformed {len(users)} user rows from {len(rows)} input rows")
        return users

    def create_live_user_summary(self, row: Any) -> Dict[str, Any]:
        """
        Create a summary object for live users (for dashboard)

        Args:
            row: Database row with live user information

        Returns:
            Live user summary dictionary
        """
        try:
            user_data = self.transform_user_row(row, include_live_duration=True)
            is_recording = bool(row['is_recording']) if 'is_recording' in row.keys() else False
            if is_recording:
                recording_state = 'recording'
            elif (
                user_data['is_active']
                and user_data['live_duration'] is not None
                and 0 <= user_data['live_duration'] <= 30
            ):
                recording_state = 'starting'
            else:
                recording_state = 'not_recording'

            # Add additional fields for live user summary
            summary = {
                'username': user_data['username'],
                'is_live': user_data['is_live'],
                'is_active': user_data['is_active'],
                'is_favorite': user_data['is_favorite'],
                'notifications_enabled': user_data['notifications_enabled'],
                'total_lives': user_data['total_lives'],
                'live_duration': user_data['live_duration'],
                'started_at': row['live_started_at'] if 'live_started_at' in row.keys() else None,
                'check_interval': user_data['check_interval'],
                # Add fields expected by frontend
                'is_recording': is_recording,
                'recording_state': recording_state,
                'viewer_count': None,  # Not tracked in current system
                # Add previous live session information
                'previous_live_started_at': row['previous_live_started_at'] if 'previous_live_started_at' in row.keys() else None,
                'previous_live_ended_at': row['previous_live_ended_at'] if 'previous_live_ended_at' in row.keys() else None
            }

            return summary

        except Exception as e:
            self.logger.error(f"Error creating live user summary: {e}")
            return {
                'username': 'unknown',
                'is_live': False,
                'is_active': False,
                'is_favorite': False,
                'notifications_enabled': False,
                'total_lives': 0,
                'live_duration': None,
                'started_at': None,
                'check_interval': 300,
                'is_recording': False,
                'recording_state': 'not_recording',
                'viewer_count': None,
                'previous_live_started_at': None,
                'previous_live_ended_at': None
            }

    def validate_user_data(self, user_data: Dict[str, Any]) -> bool:
        """
        Validate user data structure has required fields

        Args:
            user_data: User data dictionary to validate

        Returns:
            True if valid, False otherwise
        """
        required_fields = ['username', 'check_interval', 'is_live', 'is_active']

        try:
            # Check all required fields are present
            for field in required_fields:
                if field not in user_data:
                    return False

            # Validate specific field types and values
            if not user_data['username'] or not isinstance(user_data['username'], str):
                return False

            if not isinstance(user_data['check_interval'], int) or user_data['check_interval'] <= 0:
                return False

            if not isinstance(user_data['is_live'], bool):
                return False

            if not isinstance(user_data['is_active'], bool):
                return False

            return True

        except (TypeError, KeyError):
            return False

    def create_pagination_info(self, page: int, per_page: int, total_users: int) -> Dict[str, Any]:
        """
        Create standardized pagination information

        Args:
            page: Current page number
            per_page: Items per page
            total_users: Total number of users

        Returns:
            Pagination information dictionary
        """
        total_pages = (total_users + per_page - 1) // per_page

        return {
            'page': page,
            'per_page': per_page,
            'total_users': total_users,
            'total_pages': total_pages,
            'has_next': page < total_pages,
            'has_prev': page > 1
        }

    def format_api_response(self, users: List[Dict[str, Any]], pagination: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Format standardized API response

        Args:
            users: List of user data dictionaries
            pagination: Optional pagination information

        Returns:
            Formatted API response
        """
        response = {
            'users': users,
            'count': len(users),
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        if pagination:
            response['pagination'] = pagination

        return response


# Convenience instance for importing
user_data_transformer = UserDataTransformer()
