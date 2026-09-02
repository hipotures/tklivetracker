import logging

from recorder.utils.logger_manager import MaxLevelFilter, logger


def test_recorder_stream_handler_keeps_warnings_but_excludes_errors():
    log_filter = next(
        handler_filter
        for handler in logger.handlers
        for handler_filter in handler.filters
        if isinstance(handler_filter, MaxLevelFilter)
    )

    warning = logging.LogRecord(
        "logger", logging.WARNING, __file__, 1, "warning", (), None
    )
    error = logging.LogRecord(
        "logger", logging.ERROR, __file__, 1, "error", (), None
    )

    assert log_filter.filter(warning) is True
    assert log_filter.filter(error) is False
