import io
import logging

from selenium_supervisor import _route_logger_through_root


def test_recorder_info_goes_to_file_handler_but_not_warning_console():
    root_logger = logging.getLogger()
    recorder_logger = logging.getLogger("logger")
    original_root_handlers = list(root_logger.handlers)
    original_root_level = root_logger.level
    original_handlers = list(recorder_logger.handlers)
    original_level = recorder_logger.level
    original_propagate = recorder_logger.propagate

    file_output = io.StringIO()
    console_output = io.StringIO()
    bypass_output = io.StringIO()
    file_handler = logging.StreamHandler(file_output)
    file_handler.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(console_output)
    console_handler.setLevel(logging.WARNING)
    recorder_logger.addHandler(logging.StreamHandler(bypass_output))

    try:
        root_logger.handlers = [file_handler, console_handler]
        root_logger.setLevel(logging.DEBUG)
        _route_logger_through_root("logger", logging.DEBUG)

        recorder_logger.info("identity details")
        recorder_logger.warning("identity failure")

        assert "identity details" in file_output.getvalue()
        assert "identity details" not in console_output.getvalue()
        assert "identity failure" in console_output.getvalue()
        assert bypass_output.getvalue() == ""
    finally:
        root_logger.handlers = original_root_handlers
        root_logger.setLevel(original_root_level)
        recorder_logger.handlers = original_handlers
        recorder_logger.setLevel(original_level)
        recorder_logger.propagate = original_propagate


def test_pmds_debug_uses_root_formatter_without_direct_console_handler():
    root_logger = logging.getLogger()
    pmds_logger = logging.getLogger("PMDS")
    original_root_handlers = list(root_logger.handlers)
    original_root_level = root_logger.level
    original_handlers = list(pmds_logger.handlers)
    original_level = pmds_logger.level
    original_propagate = pmds_logger.propagate

    file_output = io.StringIO()
    console_output = io.StringIO()
    bypass_output = io.StringIO()
    formatter = logging.Formatter(
        "%(asctime)s - %(name)-4s - %(levelname)-5s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.StreamHandler(file_output)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(console_output)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    pmds_logger.addHandler(logging.StreamHandler(bypass_output))

    try:
        root_logger.handlers = [file_handler, console_handler]
        root_logger.setLevel(logging.DEBUG)
        _route_logger_through_root("PMDS", logging.DEBUG)

        pmds_logger.debug("Updated health status")

        formatted = file_output.getvalue()
        assert " - PMDS - DEBUG - Updated health status" in formatted
        assert console_output.getvalue() == ""
        assert bypass_output.getvalue() == ""
    finally:
        root_logger.handlers = original_root_handlers
        root_logger.setLevel(original_root_level)
        pmds_logger.handlers = original_handlers
        pmds_logger.setLevel(original_level)
        pmds_logger.propagate = original_propagate
