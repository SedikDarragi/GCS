"""Safe backup and restore of the SQLite database.

Restoring a backup is destructive: the live database file is replaced. These
helpers make that operation defensible without any UI dependency so the rules
can be tested headlessly:

* the candidate file is validated (real SQLite database, integrity check,
  GCS account schema, at least one active administrator) before anything is
  touched;
* the current database is backed up automatically before the swap;
* the new file is written to a temporary file next to the live database and
  swapped in with :func:`os.replace`, so a partial copy can never destroy the
  current data;
* if the post-restore migration/validation fails, the previous database is
  put back automatically.
"""

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from accounts import ensure_account_schema
from money import migrate_money_to_cents

REQUIRED_TABLES = ("users",)
REQUIRED_USER_COLUMNS = (
    "id",
    "username",
    "password",
    "email",
    "full_name",
    "is_admin",
)
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


class DatabaseMaintenanceError(Exception):
    """A maintenance failure carrying an i18n key."""

    def __init__(self, key, **kwargs):
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


def _absolute(path):
    return os.path.abspath(os.path.expanduser(path))


def _same_file(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.normcase(
            os.path.realpath(first)
        ) == os.path.normcase(os.path.realpath(second))


def _readonly_uri(path):
    return Path(path).resolve().as_uri() + "?mode=ro"


def _remove_sidecars(path):
    for suffix in _SQLITE_SIDECARS:
        sidecar = path + suffix
        if os.path.exists(sidecar):
            try:
                os.remove(sidecar)
            except OSError:
                pass


def _validate_connection(conn):
    result = conn.execute("PRAGMA quick_check").fetchall()
    if not result or any(row[0] != "ok" for row in result):
        raise DatabaseMaintenanceError("admin.restore_integrity_failed")

    tables = {
        row[0].lower()
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table in REQUIRED_TABLES:
        if table not in tables:
            raise DatabaseMaintenanceError(
                "admin.restore_missing_schema", table=table
            )

    columns = {
        row[1].lower() for row in conn.execute("PRAGMA table_info(users)")
    }
    missing = [column for column in REQUIRED_USER_COLUMNS if column not in columns]
    if missing:
        raise DatabaseMaintenanceError(
            "admin.restore_missing_columns", columns=", ".join(missing)
        )

    active_clause = " AND is_active = 1" if "is_active" in columns else ""
    row = conn.execute(
        f"SELECT COUNT(*) FROM users WHERE is_admin = 1{active_clause}"
    ).fetchone()
    if not row or int(row[0]) < 1:
        raise DatabaseMaintenanceError("admin.restore_no_admin")


def validate_database_file(path):
    """Raise :class:`DatabaseMaintenanceError` unless ``path`` is restorable."""
    path = _absolute(path)
    if not os.path.isfile(path):
        raise DatabaseMaintenanceError("admin.restore_invalid_file")

    conn = None
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True)
        conn.execute("PRAGMA query_only = ON")
        _validate_connection(conn)
    except DatabaseMaintenanceError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DatabaseMaintenanceError(
            "admin.restore_invalid_db", error=str(exc)
        ) from exc
    except Exception as exc:
        raise DatabaseMaintenanceError(
            "admin.restore_invalid_db", error=str(exc)
        ) from exc
    finally:
        if conn is not None:
            conn.close()

    return {"path": path}


def _copy_database(source_path, destination_path, read_only_source=False):
    """Consistent copy using SQLite's backup API (captures WAL too)."""
    if read_only_source:
        source = sqlite3.connect(_readonly_uri(source_path), uri=True)
    else:
        source = sqlite3.connect(source_path)
    try:
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
    finally:
        source.close()


def _temporary_file(directory, prefix):
    descriptor, path = tempfile.mkstemp(
        prefix=prefix, suffix=".db", dir=directory
    )
    os.close(descriptor)
    return path


def _safety_backup_path(db_path):
    directory = os.path.dirname(db_path) or "."
    base = os.path.basename(db_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = os.path.join(directory, f"{base}.pre-restore-{stamp}.bak")
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(
            directory, f"{base}.pre-restore-{stamp}-{counter}.bak"
        )
        counter += 1
    return candidate


def backup_database(db_path, destination_path):
    """Write a consistent, complete copy of ``db_path`` to ``destination_path``."""
    db_path = _absolute(db_path)
    destination_path = _absolute(destination_path)

    if _same_file(db_path, destination_path):
        raise DatabaseMaintenanceError("admin.restore_same_file")
    if not os.path.isfile(db_path):
        raise DatabaseMaintenanceError("admin.backup_missing")

    destination_dir = os.path.dirname(destination_path) or "."
    os.makedirs(destination_dir, exist_ok=True)
    temporary = _temporary_file(destination_dir, ".gcs-backup-")
    try:
        _copy_database(db_path, temporary)
        os.replace(temporary, destination_path)
    except Exception as exc:
        raise DatabaseMaintenanceError(
            "admin.backup_failed", error=str(exc)
        ) from exc
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass

    return destination_path


def _replace_with_previous_database(db_path, backup_path):
    directory = os.path.dirname(db_path) or "."
    temporary = _temporary_file(directory, ".gcs-rollback-")
    try:
        shutil.copyfile(backup_path, temporary)
        _remove_sidecars(db_path)
        os.replace(temporary, db_path)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def restore_database(db_path, source_path, create_backup=True):
    """Validate and atomically replace ``db_path`` with ``source_path``.

    Returns the path of the automatic safety backup (or ``None`` when there
    was no previous database). Raises :class:`DatabaseMaintenanceError` and
    leaves the live database untouched when the candidate is not restorable.
    """
    db_path = _absolute(db_path)
    source_path = _absolute(source_path)

    if _same_file(db_path, source_path):
        raise DatabaseMaintenanceError("admin.restore_same_file")

    validate_database_file(source_path)

    backup_path = None
    if create_backup and os.path.isfile(db_path):
        backup_path = _safety_backup_path(db_path)
        backup_database(db_path, backup_path)

    destination_dir = os.path.dirname(db_path) or "."
    os.makedirs(destination_dir, exist_ok=True)
    temporary = _temporary_file(destination_dir, ".gcs-restore-")
    try:
        _copy_database(source_path, temporary, read_only_source=True)
        validate_database_file(temporary)
        _remove_sidecars(db_path)
        os.replace(temporary, db_path)
    except DatabaseMaintenanceError:
        raise
    except Exception as exc:
        raise DatabaseMaintenanceError(
            "admin.restore_failed", error=str(exc)
        ) from exc
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass

    # Migrate the restored file to the current schema and verify it once more.
    try:
        conn = sqlite3.connect(db_path)
        try:
            ensure_account_schema(conn)
            migrate_money_to_cents(conn)
        finally:
            conn.close()
        validate_database_file(db_path)
    except Exception as exc:
        if backup_path and os.path.isfile(backup_path):
            try:
                _replace_with_previous_database(db_path, backup_path)
            except Exception as rollback_exc:
                raise DatabaseMaintenanceError(
                    "admin.restore_rollback_failed",
                    backup=backup_path,
                    error=str(exc),
                ) from rollback_exc
            raise DatabaseMaintenanceError(
                "admin.restore_rolled_back", error=str(exc)
            ) from exc
        raise

    return backup_path
