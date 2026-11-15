"""Database-level protection against duplicate document numbers.

The UI already checks for duplicates before saving, but imports, restores and
concurrent app instances can bypass those checks. Clean databases get a unique
expression index. Databases that already contain duplicates cannot get that
index without rewriting user data, so they keep their existing numbers and get
write-time triggers instead: every new insert/update is still checked by
SQLite itself.
"""

import sqlite3

UNIQUE_INDEX = "idx_documents_numero_unique"
INSERT_TRIGGER = "trg_documents_numero_unique_insert"
UPDATE_TRIGGER = "trg_documents_numero_unique_update"


def _documents_table_exists(conn):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
    ).fetchone()
    return row is not None


def find_duplicate_numeros(conn):
    """Return ``(normalized_number, count)`` for every duplicated number."""
    rows = conn.execute(
        """
        SELECT UPPER(TRIM(numero)) AS normalized, COUNT(*) AS total
        FROM documents
        GROUP BY normalized
        HAVING total > 1
        ORDER BY normalized
        """
    ).fetchall()
    return [(row[0], int(row[1])) for row in rows]


def _create_triggers(conn):
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {INSERT_TRIGGER}
        BEFORE INSERT ON documents
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM documents
            WHERE UPPER(TRIM(numero)) = UPPER(TRIM(NEW.numero))
        )
        BEGIN
            SELECT RAISE(ABORT, 'duplicate document number');
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {UPDATE_TRIGGER}
        BEFORE UPDATE OF numero ON documents
        FOR EACH ROW
        WHEN UPPER(TRIM(NEW.numero)) <> UPPER(TRIM(OLD.numero))
         AND EXISTS (
            SELECT 1 FROM documents
            WHERE id <> OLD.id
              AND UPPER(TRIM(numero)) = UPPER(TRIM(NEW.numero))
        )
        BEGIN
            SELECT RAISE(ABORT, 'duplicate document number');
        END
        """
    )


def ensure_unique_document_numbers(conn):
    """Add database-level uniqueness, returning legacy duplicate groups.

    Existing duplicates are left untouched. In that case triggers reject only
    *new* duplicate numbers, so editing or reading those old rows still works.
    """
    if not _documents_table_exists(conn):
        return []

    duplicates = find_duplicate_numeros(conn)
    if duplicates:
        # A unique index cannot be created over the existing duplicates.
        conn.execute(f"DROP INDEX IF EXISTS {UNIQUE_INDEX}")
        _create_triggers(conn)
    else:
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_INDEX} "
            "ON documents(UPPER(TRIM(numero)))"
        )
        conn.execute(f"DROP TRIGGER IF EXISTS {INSERT_TRIGGER}")
        conn.execute(f"DROP TRIGGER IF EXISTS {UPDATE_TRIGGER}")
    return duplicates
