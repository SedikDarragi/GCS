import os
import sqlite3

import pytest

import database_maintenance
from accounts import ensure_account_schema
from database_maintenance import (
    DatabaseMaintenanceError,
    backup_database,
    reset_database,
    restore_database,
    validate_database_file,
)


def _make_database(path, users=(), with_users_table=True):
    conn = sqlite3.connect(path)
    try:
        if with_users_table:
            ensure_account_schema(conn)
            for username, is_admin, is_active in users:
                conn.execute(
                    "INSERT INTO users (username, password, email, full_name, "
                    "is_admin, is_active) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        username,
                        "legacy-password",
                        f"{username}@example.com",
                        username.title(),
                        1 if is_admin else 0,
                        1 if is_active else 0,
                    ),
                )
        else:
            conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    return path


def _usernames(path):
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT username FROM users ORDER BY username")]
    finally:
        conn.close()


def test_validate_accepts_gcs_database_with_active_admin(tmp_path):
    db = _make_database(tmp_path / "good.db", [("admin", True, True)])
    assert validate_database_file(db)["path"] == os.path.abspath(str(db))


def test_validate_rejects_non_database_file(tmp_path):
    path = tmp_path / "notes.db"
    path.write_text("this is not a database", encoding="utf-8")

    with pytest.raises(DatabaseMaintenanceError) as exc:
        validate_database_file(path)
    assert exc.value.key == "admin.restore_invalid_db"


def test_validate_rejects_missing_file(tmp_path):
    with pytest.raises(DatabaseMaintenanceError) as exc:
        validate_database_file(tmp_path / "missing.db")
    assert exc.value.key == "admin.restore_invalid_file"


def test_validate_rejects_database_without_users_table(tmp_path):
    db = _make_database(tmp_path / "other.db", with_users_table=False)

    with pytest.raises(DatabaseMaintenanceError) as exc:
        validate_database_file(db)
    assert exc.value.key == "admin.restore_missing_schema"
    assert exc.value.kwargs == {"table": "users"}


def test_validate_rejects_database_without_active_admin(tmp_path):
    db = _make_database(tmp_path / "normal.db", [("alice", False, True)])

    with pytest.raises(DatabaseMaintenanceError) as exc:
        validate_database_file(db)
    assert exc.value.key == "admin.restore_no_admin"


def test_validate_rejects_inactive_admin(tmp_path):
    db = _make_database(tmp_path / "inactive.db", [("admin", True, False)])

    with pytest.raises(DatabaseMaintenanceError) as exc:
        validate_database_file(db)
    assert exc.value.key == "admin.restore_no_admin"


def test_validate_rejects_incompatible_users_table(tmp_path):
    path = tmp_path / "incompatible.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(DatabaseMaintenanceError) as exc:
        validate_database_file(path)
    assert exc.value.key == "admin.restore_missing_columns"
    assert "email" in exc.value.kwargs["columns"]


def test_backup_database_is_consistent_and_rejects_same_file(tmp_path):
    db = _make_database(tmp_path / "live.db", [("admin", True, True)])
    backup = tmp_path / "backup.db"

    result = backup_database(db, backup)

    assert result == os.path.abspath(str(backup))
    assert _usernames(backup) == ["admin"]
    validate_database_file(backup)

    with pytest.raises(DatabaseMaintenanceError) as exc:
        backup_database(db, db)
    assert exc.value.key == "admin.restore_same_file"


def test_restore_replaces_database_and_creates_safety_backup(tmp_path):
    live = _make_database(tmp_path / "live.db", [("admin", True, True)])
    source = _make_database(tmp_path / "source.db", [("restored", True, True)])

    backup_path = restore_database(live, source)

    assert _usernames(live) == ["restored"]
    assert backup_path is not None
    assert backup_path.endswith(".bak")
    assert _usernames(backup_path) == ["admin"]
    validate_database_file(live)
    assert not list(tmp_path.glob(".gcs-restore-*"))


def test_restore_rejects_invalid_source_without_touching_live(tmp_path):
    live = _make_database(tmp_path / "live.db", [("admin", True, True)])
    bad = tmp_path / "bad.db"
    bad.write_text("garbage", encoding="utf-8")
    before = live.read_bytes()

    with pytest.raises(DatabaseMaintenanceError) as exc:
        restore_database(live, bad)
    assert exc.value.key == "admin.restore_invalid_db"
    assert live.read_bytes() == before


def test_restore_rejects_source_without_admin(tmp_path):
    live = _make_database(tmp_path / "live.db", [("admin", True, True)])
    source = _make_database(tmp_path / "source.db", [("alice", False, True)])
    before = live.read_bytes()

    with pytest.raises(DatabaseMaintenanceError) as exc:
        restore_database(live, source)
    assert exc.value.key == "admin.restore_no_admin"
    assert live.read_bytes() == before


def test_restore_rejects_restoring_the_live_database(tmp_path):
    live = _make_database(tmp_path / "live.db", [("admin", True, True)])

    with pytest.raises(DatabaseMaintenanceError) as exc:
        restore_database(live, live)
    assert exc.value.key == "admin.restore_same_file"


def test_restore_removes_stale_sidecars_before_swap(tmp_path, monkeypatch):
    live = _make_database(tmp_path / "live.db", [("admin", True, True)])
    source = _make_database(tmp_path / "source.db", [("restored", True, True)])
    real_validate = database_maintenance.validate_database_file
    planted = []

    def validate_and_plant(path):
        result = real_validate(path)
        if os.path.basename(path).startswith(".gcs-restore-"):
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = str(live) + suffix
                with open(sidecar, "wb") as handle:
                    handle.write(b"stale")
                planted.append(sidecar)
        return result

    monkeypatch.setattr(
        database_maintenance, "validate_database_file", validate_and_plant
    )

    restore_database(live, source)

    assert planted
    assert not any(os.path.exists(path) for path in planted)
    assert _usernames(live) == ["restored"]
    validate_database_file(live)


def test_restore_rolls_back_when_post_restore_migration_fails(tmp_path, monkeypatch):
    live = _make_database(tmp_path / "live.db", [("admin", True, True)])
    source = _make_database(tmp_path / "source.db", [("restored", True, True)])

    def fail_migration(conn):
        raise sqlite3.DatabaseError("simulated migration failure")

    monkeypatch.setattr(
        database_maintenance, "ensure_account_schema", fail_migration
    )

    with pytest.raises(DatabaseMaintenanceError) as exc:
        restore_database(live, source)

    assert exc.value.key == "admin.restore_rolled_back"
    assert "simulated migration failure" in exc.value.kwargs["error"]
    assert _usernames(live) == ["admin"]
    assert len(list(tmp_path.glob("live.db.pre-restore-*.bak"))) == 1
    validate_database_file(live)


def test_backup_rejects_missing_database(tmp_path):
    with pytest.raises(DatabaseMaintenanceError) as exc:
        backup_database(tmp_path / "missing.db", tmp_path / "backup.db")
    assert exc.value.key == "admin.backup_missing"


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()


def test_reset_preserves_accounts_and_drops_business_data(tmp_path):
    live = _make_database(
        tmp_path / "live.db", [("admin", True, True), ("alice", False, True)]
    )
    conn = sqlite3.connect(live)
    try:
        conn.execute(
            "CREATE TABLE articles (id INTEGER PRIMARY KEY, designation TEXT)"
        )
        conn.execute("INSERT INTO articles (designation) VALUES ('Widget')")
        conn.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, numero TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    preserved = reset_database(live)

    assert preserved == 2
    assert _usernames(live) == ["admin", "alice"]
    validate_database_file(live)
    tables = _tables(live)
    assert "users" in tables
    assert "articles" not in tables
    assert "documents" not in tables
    conn = sqlite3.connect(live)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    finally:
        conn.close()


def test_reset_removes_stale_sidecars_before_swap(tmp_path, monkeypatch):
    live = _make_database(tmp_path / "live.db", [("admin", True, True)])
    real_validate = database_maintenance.validate_database_file
    planted = []

    def validate_and_plant(path):
        result = real_validate(path)
        if os.path.basename(path).startswith(".gcs-reset-"):
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = str(live) + suffix
                with open(sidecar, "wb") as handle:
                    handle.write(b"stale")
                planted.append(sidecar)
        return result

    monkeypatch.setattr(
        database_maintenance, "validate_database_file", validate_and_plant
    )

    reset_database(live)

    assert planted
    assert not any(os.path.exists(path) for path in planted)
    assert _usernames(live) == ["admin"]
    validate_database_file(live)


def test_reset_refuses_to_remove_the_last_admin(tmp_path):
    live = _make_database(tmp_path / "live.db", [("alice", False, True)])
    before = live.read_bytes()

    with pytest.raises(DatabaseMaintenanceError) as exc:
        reset_database(live)

    assert exc.value.key == "admin.restore_no_admin"
    assert live.read_bytes() == before
    assert _usernames(live) == ["alice"]
