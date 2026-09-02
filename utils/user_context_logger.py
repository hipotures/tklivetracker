"""
User Context Logger - Automatically adds [username] prefix to log messages

This module provides a wrapper around Python's standard logger to automatically
add username context to log messages, making it easier to filter logs by user.
"""

import logging
from typing import Optional


class UserContextLogger:
    """
    Logger wrapper that automatically adds [username] prefix to log messages
    when username context is provided.

    Usage:
        # Create context logger for specific user
        user_logger = UserContextLogger(base_logger, "john_doe")
        user_logger.info("Status changed")  # Logs: [john_doe] Status changed

        # Create logger without context (normal logging)
        general_logger = UserContextLogger(base_logger)
        general_logger.info("System started")  # Logs: System started

        # Change context dynamically
        user_logger.set_username("jane_smith")
        user_logger.info("New message")  # Logs: [jane_smith] New message
    """

    def __init__(self, base_logger: logging.Logger, username: Optional[str] = None):
        """
        Initialize UserContextLogger

        Args:
            base_logger: The underlying Python logger instance
            username: Optional username to add as prefix. If None, no prefix is added.
        """
        self.base_logger = base_logger
        self.username = username

    def _format_message(self, message: str) -> str:
        """
        Add username prefix if username is set

        Args:
            message: Original log message

        Returns:
            Formatted message with [username] prefix if username is set,
            otherwise returns original message
        """
        if self.username:
            return f"[{self.username}] {message}"
        return message

    def info(self, message: str, *args, **kwargs):
        """Log info message with username context"""
        self.base_logger.info(self._format_message(message), *args, **kwargs)

    def debug(self, message: str, *args, **kwargs):
        """Log debug message with username context"""
        self.base_logger.debug(self._format_message(message), *args, **kwargs)

    def warning(self, message: str, *args, **kwargs):
        """Log warning message with username context"""
        self.base_logger.warning(self._format_message(message), *args, **kwargs)

    def error(self, message: str, *args, **kwargs):
        """Log error message with username context"""
        self.base_logger.error(self._format_message(message), *args, **kwargs)

    def critical(self, message: str, *args, **kwargs):
        """Log critical message with username context"""
        self.base_logger.critical(self._format_message(message), *args, **kwargs)

    def exception(self, message: str, *args, **kwargs):
        """Log exception message with username context"""
        self.base_logger.exception(self._format_message(message), *args, **kwargs)

    def set_username(self, username: Optional[str]):
        """
        Update username context

        Args:
            username: New username to use as prefix, or None to disable prefix
        """
        self.username = username

    def get_user_logger(self, username: str) -> 'UserContextLogger':
        """
        Create new logger instance with specific username context

        Args:
            username: Username for the new logger context

        Returns:
            New UserContextLogger instance with the specified username
        """
        return UserContextLogger(self.base_logger, username)

    # Delegate other logger properties and methods
    @property
    def level(self):
        """Get the logger's effective level"""
        return self.base_logger.level

    def setLevel(self, level):
        """Set the logger's level"""
        return self.base_logger.setLevel(level)

    def isEnabledFor(self, level):
        """Check if logger is enabled for given level"""
        return self.base_logger.isEnabledFor(level)

    def getEffectiveLevel(self):
        """Get the logger's effective level"""
        return self.base_logger.getEffectiveLevel()

    def hasHandlers(self):
        """Check if logger has handlers"""
        return self.base_logger.hasHandlers()

    def addHandler(self, handler):
        """Add handler to logger"""
        return self.base_logger.addHandler(handler)

    def removeHandler(self, handler):
        """Remove handler from logger"""
        return self.base_logger.removeHandler(handler)

    @property
    def name(self):
        """Get logger name"""
        return self.base_logger.name

    def __repr__(self):
        """String representation of UserContextLogger"""
        if self.username:
            return f"UserContextLogger(name='{self.base_logger.name}', username='{self.username}')"
        else:
            return f"UserContextLogger(name='{self.base_logger.name}', username=None)"