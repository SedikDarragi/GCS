"""Tests for database-level document-number uniqueness."""

import sqlite3

import pytest

from document_numbers import (
    INSERT_TRIGGER,
    UNIQUE_INDEX,
    UPDATE_TRIGGER,
    ensure_unique_document_numbers,
)
from main_app import MainApp


def _make_app(tmp_path):
    app = MainApp.__new__(MainApp)
    app.db_path = str(tmp_path / "commercial.db")
    app.init_db()
    return app


def _names(db_path, kind):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
            )
        }
    finally:
        conn.close()


def _insert(conn, numero, doc_type="devis"):
    conn.execute(
        "INSERT INTO documents (type, numero, date) VALUES (?, ?, '2026-01-01')",
        (doc_type, numero),
    )


def test_init_db_creates_unique_index(tmp_path):
    app = _make_app(tmp_path)

    assert UNIQUE_INDEX in _names(app.db_path, "index")
    assert INSERT_TRIGGER not in _names(app.db_path, "trigger")
    assert UPDATE_TRIGGER not in _names(app.db_path, "trigger")


def test_duplicate_insert_is_rejected_case_and_space_insensitive(tmp_path):
    app = _make_app(tmp_path)
    conn = sqlite3.connect(app.db_path)
    try:
        _insert(conn, "DEV-2026-0001")
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, "  dev-2026-0001  ")
    finally:
        conn.rollback()
        conn.close()


def test_duplicate_update_is_rejected(tmp_path):
    app = _make_app(tmp_path)
    conn = sqlite3.connect(app.db_path)
    try:
        _insert(conn, "DEV-2026-0001")
        _insert(conn, "DEV-2026-0002")
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE documents SET numero = 'dev-2026-0001' "
                "WHERE numero = 'DEV-2026-0002'"
            )
    finally:
        conn.rollback()
        conn.close()


def test_legacy_duplicates_are_kept_and_block_new_ones(tmp_path):
    app = _make_app(tmp_path)

    # Simulate a database imported before this migration existed.
    conn = sqlite3.connect(app.db_path)
    try:
        conn.execute(f"DROP INDEX IF EXISTS {UNIQUE_INDEX}")
        _insert(conn, "DEV-2026-0001")
        _insert(conn, "dev-2026-0001")
        _insert(conn, "DEV-2026-0002")
        conn.commit()
    finally:
        conn.close()

    app.init_db()

    assert UNIQUE_INDEX not in _names(app.db_path, "index")
    assert INSERT_TRIGGER in _names(app.db_path, "trigger")
    assert UPDATE_TRIGGER in _names(app.db_path, "trigger")

    conn = sqlite3.connect(app.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3

        # Editing one of the legacy rows without touching its number works.
        conn.execute(
            "UPDATE documents SET date = '2026-02-02' "
            "WHERE numero = 'DEV-2026-0001'"
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, "DEV-2026-0001")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE documents SET numero = 'DEV-2026-0001' "
                "WHERE numero = 'DEV-2026-0002'"
            )
    finally:
        conn.rollback()
        conn.close()


def test_cleanup_of_legacy_duplicates_restores_the_unique_index(tmp_path):
    app = _make_app(tmp_path)
    conn = sqlite3.connect(app.db_path)
    try:
        conn.execute(f"DROP INDEX IF EXISTS {UNIQUE_INDEX}")
        _insert(conn, "DEV-2026-0001")
        _insert(conn, "DEV-2026-0001")
        conn.commit()
    finally:
        conn.close()

    app.init_db()
    conn = sqlite3.connect(app.db_path)
    try:
        conn.execute("DELETE FROM documents WHERE id = 2")
        conn.commit()
    finally:
        conn.close()

    app.init_db()

    assert UNIQUE_INDEX in _names(app.db_path, "index")
    assert INSERT_TRIGGER not in _names(app.db_path, "trigger")
    assert UPDATE_TRIGGER not in _names(app.db_path, "trigger")


def test_helper_reports_duplicate_groups(tmp_path):
    app = _make_app(tmp_path)
    conn = sqlite3.connect(app.db_path)
    try:
        conn.execute(f"DROP INDEX IF EXISTS {UNIQUE_INDEX}")
        _insert(conn, "DEV-2026-0001")
        _insert(conn, "DEV-2026-0001")
        _insert(conn, "DEV-2026-0002")
        conn.commit()

        duplicates = ensure_unique_document_numbers(conn)
    finally:
        conn.close()

    assert duplicates == [("DEV-2026-0001", 2)]
