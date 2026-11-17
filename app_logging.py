"""Rotating file logging for the desktop app.

The app is usually launched by double-clicking, so console output is lost.
All diagnostics go to a bounded log file that can be opened from the About
dialog. Importing this module has no side effects: logging is configured once
from the application entry point.
"""

import logging
import os
import subprocess
import sys
import tempfile
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "gcs"
LOG_FILENAME = "gcs.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_DIR = os.path.join(BASE_DIR, "logs")

_configured = False
_log_file = None


def get_logger():
    """Return the shared application logger."""
    return logging.getLogger(LOGGER_NAME)


def _choose_log_dir(log_dir):
    candidates = [log_dir, os.environ.get("GCS_LOG_DIR"), DEFAULT_LOG_DIR]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError:
            continue
    fallback = os.path.join(tempfile.gettempdir(), "gcs-logs")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def configure_logging(log_dir=None):
    """Configure the rotating file handler once and return the logger."""
    global _configured, _log_file
    if _configured:
        return get_logger()

    directory = _choose_log_dir(log_dir)
    _log_file = os.path.join(directory, LOG_FILENAME)

    logger = get_logger()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = RotatingFileHandler(
        _log_file,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    _configured = True
    return logger


def log_path():
    """Path of the active log file (creating the configuration if needed)."""
    if not _configured:
        configure_logging()
    return _log_file


def open_log_file():
    """Open the log file in the OS default text editor.

    Returns ``True`` when the file was handed to the operating system.
    """
    path = log_path()
    if not os.path.exists(path):
        with open(path, "a", encoding="utf-8"):
            pass
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        get_logger().exception("Could not open log file %s", path)
        return False
    return True
