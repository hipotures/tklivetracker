import logging
import os

# Mapping strings to logging levels
LOG_LEVELS = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}

def setup_logger(log_path, logger_name="tiktok_supervisor", log_level_str="INFO"):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logger = logging.getLogger(logger_name)
    # Ustaw poziom logowania na podstawie stringa
    log_level = LOG_LEVELS.get(log_level_str.upper(), logging.INFO)
    logger.setLevel(log_level)
    if logger.hasHandlers():
        logger.handlers.clear()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger