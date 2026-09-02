import logging

from flask.logging import default_handler

from web_monitor.app import create_app


def test_create_app_uses_single_web_monitor_stdout_handler() -> None:
    logger = logging.getLogger("web_monitor.app")
    werkzeug_logger = logging.getLogger("werkzeug")
    original_handlers = list(logger.handlers)
    original_werkzeug_level = werkzeug_logger.level

    try:
        logger.handlers = [default_handler]

        create_app(read_only=False)
        create_app(read_only=False)

        web_handlers = [
            handler
            for handler in logger.handlers
            if getattr(handler, "_ttracker_web_monitor_handler", False)
        ]
        assert len(web_handlers) == 1
        assert default_handler not in logger.handlers
        assert werkzeug_logger.level == logging.WARNING
    finally:
        logger.handlers = original_handlers
        werkzeug_logger.setLevel(original_werkzeug_level)
