"""
Disk Alert Handler - Logging handler that emits bell signal on disk full errors

This module provides a custom logging handler that wraps existing handlers
and emits a bell signal (\a) to the console when disk full errors occur.
"""

import logging
import sys


class DiskAlertHandler(logging.Handler):
    """
    Logging handler that wraps another handler and emits a bell signal
    when disk full errors (OSError: Errno 28) are detected.

    Usage:
        # Wrap an existing handler
        file_handler = logging.FileHandler('app.log')
        alert_handler = DiskAlertHandler(file_handler)
        logger.addHandler(alert_handler)
    """

    def __init__(self, wrapped_handler: logging.Handler):
        """
        Initialize DiskAlertHandler

        Args:
            wrapped_handler: The logging handler to wrap
        """
        super().__init__()
        self.wrapped_handler = wrapped_handler
        self.last_alert_time = 0
        self.alert_cooldown = 5  # Seconds between alerts to avoid spam

    def emit(self, record):
        """
        Emit a log record, catching disk full errors and emitting bell signal

        Args:
            record: LogRecord to emit
        """
        import time

        try:
            # Try to emit through wrapped handler
            self.wrapped_handler.emit(record)
        except OSError as e:
            # Check if this is a "No space left on device" error
            if e.errno == 28:  # ENOSPC - No space left on device
                # Emit bell signal to console with cooldown
                current_time = time.time()
                if current_time - self.last_alert_time >= self.alert_cooldown:
                    # Print bell signal to stderr (console)
                    sys.stderr.write('\a')  # ASCII bell character
                    sys.stderr.flush()

                    # Try to print warning message (may also fail if no disk space)
                    try:
                        sys.stderr.write(f'\n⚠️  DISK FULL ERROR: {e}\n')
                        sys.stderr.flush()
                    except:
                        pass  # Even stderr might fail if disk is full

                    self.last_alert_time = current_time
            else:
                # Re-raise non-disk-full OSErrors
                raise
        except Exception:
            # For other exceptions, use standard error handling
            self.handleError(record)

    def setLevel(self, level):
        """Set level for both this handler and wrapped handler"""
        super().setLevel(level)
        self.wrapped_handler.setLevel(level)

    def setFormatter(self, fmt):
        """Set formatter for wrapped handler"""
        self.wrapped_handler.setFormatter(fmt)

    def close(self):
        """Close wrapped handler"""
        self.wrapped_handler.close()
        super().close()


def wrap_handlers_with_disk_alert(logger: logging.Logger) -> None:
    """
    Wrap all handlers of a logger with DiskAlertHandler

    This function replaces all existing handlers with DiskAlertHandler-wrapped versions
    to enable automatic bell signal on disk full errors.

    Args:
        logger: Logger instance whose handlers should be wrapped
    """
    # Get all current handlers
    handlers = logger.handlers.copy()

    # Remove all handlers from logger
    for handler in handlers:
        logger.removeHandler(handler)

    # Wrap each handler and add back
    for handler in handlers:
        # Skip if already wrapped
        if isinstance(handler, DiskAlertHandler):
            logger.addHandler(handler)
        else:
            wrapped = DiskAlertHandler(handler)
            # Inherit level from original handler
            wrapped.level = handler.level
            logger.addHandler(wrapped)
