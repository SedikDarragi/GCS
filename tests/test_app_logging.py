"""Tests for the rotating file logger and removal of console prints."""

import ast
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

import app_logging
import i18n

BASE = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def log_dir(tmp_path_factory):
    """Configure logging once for this module; configuration is idempotent."""
    directory = tmp_path_factory.mktemp("gcs-logs")
    app_logging.configure_logging(log_dir=directory)
    return directory


def _file_handler():
    return next(
        handler
        for handler in app_logging.get_logger().handlers
        if isinstance(handler, RotatingFileHandler)
    )


def test_configure_logging_uses_a_rotating_file(log_dir):
    logger = app_logging.get_logger()

    assert logger.name == "gcs"
    handler = _file_handler()
    assert handler.maxBytes == app_logging.MAX_BYTES
    assert handler.backupCount == app_logging.BACKUP_COUNT
    assert Path(app_logging.log_path()).parent == log_dir


def test_logger_writes_messages_to_the_file(log_dir):
    logger = app_logging.get_logger()

    logger.info("test message %s", "value")
    for handler in logger.handlers:
        handler.flush()

    assert "test message value" in Path(app_logging.log_path()).read_text(
        encoding="utf-8"
    )


def test_configure_logging_is_idempotent(log_dir):
    logger = app_logging.get_logger()
    before = list(logger.handlers)

    assert app_logging.configure_logging(log_dir=log_dir) is logger
    assert list(logger.handlers) == before


def test_open_log_file_reports_failure(monkeypatch, log_dir):
    def boom(*_args, **_kwargs):
        raise OSError("cannot open")

    monkeypatch.setattr(app_logging.os, "startfile", boom, raising=False)
    monkeypatch.setattr(app_logging.subprocess, "run", boom)

    assert app_logging.open_log_file() is False


def test_app_modules_do_not_print_to_console():
    offenders = []
    for name in ("main.py", "main_app.py"):
        tree = ast.parse((BASE / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{name}:{node.lineno}")

    assert offenders == []


def test_log_actions_are_translated():
    for key in ("about.open_logs", "about.logs_open_failed"):
        assert key in i18n.TRANSLATIONS["en"]
        assert key in i18n.TRANSLATIONS["fr"]
