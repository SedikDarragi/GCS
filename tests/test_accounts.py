import sqlite3
from datetime import datetime, timedelta

import pytest

import accounts
from accounts import (
    AccountError,
    admin_create_user,
    admin_delete_user,
    admin_update_user,
    authenticate_user,
    change_password,
    create_user,
    ensure_account_schema,
    get_user_by_id,
    hash_password,
    is_password_hashed,
    seed_default_admin,
    validate_email,
    validate_full_name,
    validate_password,
    validate_username,
)


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    monkeypatch.setattr(accounts, "PBKDF2_ITERATIONS", 1000)
    monkeypatch.setattr(accounts, "_DUMMY_HASH", None)


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "accounts.db")
    ensure_account_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def _user(conn, username="alice", password="StrongPass1", **kwargs):
    return create_user(
        conn,
        username,
        password,
        kwargs.pop("email", f"{username}@example.com"),
        kwargs.pop("full_name", username.title()),
        **kwargs,
    )


def test_ensure_account_schema_adds_columns_and_is_idempotent(tmp_path):
    connection = sqlite3.connect(tmp_path / "legacy.db")
    try:
        connection.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                is_admin BOOLEAN DEFAULT 0
            )
            """
        )
        connection.execute(
            "INSERT INTO users (username, password, email, full_name, is_admin) "
            "VALUES ('admin', 'admin123', 'admin@example.com', 'Administrateur', 1)"
        )
        connection.commit()

        ensure_account_schema(connection)
        ensure_account_schema(connection)

        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        for expected in (
            "is_active",
            "must_change_password",
            "failed_attempts",
            "locked_until",
            "last_login_at",
            "password_changed_at",
            "created_at",
        ):
            assert expected in columns

        admin = connection.execute(
            "SELECT must_change_password FROM users WHERE username = 'admin'"
        ).fetchone()
        assert admin[0] == 1
    finally:
        connection.close()


def test_create_user_hashes_and_normalizes(conn):
    user = create_user(
        conn,
        "  Alice  ",
        "StrongPass1",
        "  ALICE@Example.COM ",
        "  Alice   Smith ",
    )

    assert user["username"] == "Alice"
    assert user["email"] == "alice@example.com"
    assert user["full_name"] == "Alice Smith"
    assert user["is_admin"] is False
    assert user["is_active"] is True
    assert "password" not in user

    stored = get_user_by_id(conn, user["id"])["password"]
    assert is_password_hashed(stored)
    assert stored.startswith("pbkdf2$")


def test_usernames_and_emails_are_case_insensitively_unique(conn):
    _user(conn, "Alice", email="alice@example.com")

    with pytest.raises(AccountError) as exc:
        _user(conn, "alice", email="other@example.com")
    assert exc.value.key == "validation.username_taken"

    with pytest.raises(AccountError) as exc:
        _user(conn, "bob", email="ALICE@example.com")
    assert exc.value.key == "validation.email_taken"


def test_account_input_validation():
    assert validate_username("") == "validation.username_required"
    assert validate_username("ab") == "validation.username_too_short"
    assert validate_username("a" * 40) == "validation.username_too_long"
    assert validate_username("bad username") == "validation.username_invalid"
    assert validate_username("good.user-name_1") is None

    assert validate_email("") == "validation.email_required"
    assert validate_email("not-an-email") == "validation.email_invalid"
    assert validate_email("user@example.com") is None

    assert validate_full_name("") == "validation.fullname_required"
    assert validate_full_name("Alice") is None

    assert validate_password("") == "validation.password_required"
    assert validate_password("        ") == "validation.password_required"
    assert validate_password("short") == "validation.password_too_short"
    assert validate_password("x" * 200) == "validation.password_too_long"
    assert validate_password("admin123") == "validation.password_weak"
    assert validate_password("StrongPass1") is None
    assert validate_password("StrongPass1", "OtherPass1") == "validation.password_mismatch"


def test_authenticate_is_case_insensitive_and_updates_login(conn):
    _user(conn, "Alice", password="StrongPass1")

    user, status, info = authenticate_user(conn, "alice", "StrongPass1")
    assert status == "ok"
    assert info == {}
    assert user["username"] == "Alice"
    assert "password" not in user

    stored = get_user_by_id(conn, user["id"])
    assert stored["last_login_at"] is not None


def test_legacy_plaintext_password_is_migrated_on_login(conn):
    conn.execute(
        "INSERT INTO users (username, password, email, full_name, is_admin, is_active) "
        "VALUES ('legacy', 'legacy-password', 'legacy@example.com', 'Legacy', 0, 1)"
    )
    conn.commit()

    user, status, _ = authenticate_user(conn, "legacy", "legacy-password")
    assert status == "ok"
    assert user["username"] == "legacy"

    stored = get_user_by_id(conn, user["id"])["password"]
    assert is_password_hashed(stored)


def test_failed_logins_lock_the_account_temporarily(conn):
    _user(conn, "Alice", password="StrongPass1")
    now = datetime(2026, 1, 1, 12, 0, 0)

    for _ in range(accounts.MAX_FAILED_ATTEMPTS - 1):
        _, status, info = authenticate_user(conn, "Alice", "wrong", now=now)
        assert status == "invalid"
        assert info["attempts_left"] >= 1

    _, status, info = authenticate_user(conn, "Alice", "wrong", now=now)
    assert status == "locked"
    assert info["minutes"] == accounts.LOCKOUT_SECONDS // 60

    _, status, _ = authenticate_user(
        conn, "Alice", "StrongPass1", now=now + timedelta(minutes=1)
    )
    assert status == "locked"

    user, status, _ = authenticate_user(
        conn,
        "Alice",
        "StrongPass1",
        now=now + timedelta(seconds=accounts.LOCKOUT_SECONDS + 1),
    )
    assert status == "ok"
    assert user["failed_attempts"] == 0


def test_lockout_window_resets_after_expiry(conn):
    _user(conn, "Alice", password="StrongPass1")
    now = datetime(2026, 1, 1, 12, 0, 0)

    for _ in range(accounts.MAX_FAILED_ATTEMPTS):
        authenticate_user(conn, "Alice", "wrong", now=now)

    after_expiry = now + timedelta(seconds=accounts.LOCKOUT_SECONDS + 1)
    _, status, info = authenticate_user(
        conn, "Alice", "wrong", now=after_expiry
    )
    assert status == "invalid"
    assert info["attempts_left"] == accounts.MAX_FAILED_ATTEMPTS - 1


def test_inactive_account_cannot_log_in(conn):
    user = _user(conn, "Alice", password="StrongPass1")
    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user["id"],))
    conn.commit()

    authenticated, status, _ = authenticate_user(conn, "Alice", "StrongPass1")
    assert authenticated is None
    assert status == "inactive"


def test_change_password_requires_current_and_different_password(conn):
    user = _user(conn, "Alice", password="StrongPass1")

    with pytest.raises(AccountError) as exc:
        change_password(conn, user["id"], "wrong", "NewStrong2", "NewStrong2")
    assert exc.value.key == "validation.current_password_invalid"

    with pytest.raises(AccountError) as exc:
        change_password(
            conn, user["id"], "StrongPass1", "StrongPass1", "StrongPass1"
        )
    assert exc.value.key == "validation.password_unchanged"

    with pytest.raises(AccountError) as exc:
        change_password(conn, user["id"], "StrongPass1", "NewStrong2", "Mismatch2")
    assert exc.value.key == "validation.password_mismatch"

    changed = change_password(
        conn, user["id"], "StrongPass1", "NewStrong2", "NewStrong2"
    )
    assert changed["must_change_password"] is False
    assert authenticate_user(conn, "Alice", "NewStrong2")[1] == "ok"


def test_seed_default_admin_generates_strong_password(conn, monkeypatch):
    monkeypatch.delenv(accounts.INITIAL_PASSWORD_ENV, raising=False)

    seeded = seed_default_admin(conn)
    assert seeded is not None
    assert seeded["generated"] is True
    assert seeded["password"] != "admin123"
    assert validate_password(seeded["password"]) is None
    assert seeded["user"]["is_admin"] is True
    assert seeded["user"]["must_change_password"] is True
    assert seed_default_admin(conn) is None

    authenticated, status, _ = authenticate_user(
        conn, "admin", seeded["password"]
    )
    assert status == "ok"
    assert authenticated["must_change_password"] is True


def test_seed_default_admin_uses_configured_password(conn, monkeypatch):
    monkeypatch.setenv(accounts.INITIAL_PASSWORD_ENV, "ConfiguredPass1")

    seeded = seed_default_admin(conn)
    assert seeded["generated"] is False
    assert seeded["password"] == "ConfiguredPass1"
    assert authenticate_user(conn, "admin", "ConfiguredPass1")[1] == "ok"


def test_admin_can_create_update_and_delete_users(conn):
    admin = _user(conn, "root", password="RootPass12", is_admin=True)

    created = admin_create_user(
        conn,
        admin["id"],
        "bob",
        "BobPass123",
        "bob@example.com",
        "Bob",
    )
    assert created["must_change_password"] is True

    updated = admin_update_user(
        conn,
        admin["id"],
        created["id"],
        "bob.new@example.com",
        "Bob New",
        is_admin=True,
        is_active=True,
        new_password="BobPass456",
        confirm_password="BobPass456",
    )
    assert updated["email"] == "bob.new@example.com"
    assert updated["is_admin"] is True
    assert updated["must_change_password"] is True
    assert authenticate_user(conn, "bob", "BobPass456")[1] == "ok"

    admin_delete_user(conn, admin["id"], created["id"])
    assert get_user_by_id(conn, created["id"]) is None


def test_admin_guards_last_admin_and_self_delete(conn):
    admin = _user(conn, "root", password="RootPass12", is_admin=True)
    _user(conn, "bob", password="BobPass123")

    with pytest.raises(AccountError) as exc:
        admin_delete_user(conn, admin["id"], admin["id"])
    assert exc.value.key == "admin.error_self_delete"

    with pytest.raises(AccountError) as exc:
        admin_update_user(
            conn,
            admin["id"],
            admin["id"],
            admin["email"],
            admin["full_name"],
            is_admin=False,
            is_active=True,
        )
    assert exc.value.key == "admin.error_last_admin"

    with pytest.raises(AccountError) as exc:
        admin_update_user(
            conn,
            admin["id"],
            admin["id"],
            admin["email"],
            admin["full_name"],
            is_admin=True,
            is_active=False,
        )
    assert exc.value.key == "admin.error_self_deactivate"


def test_non_admin_cannot_manage_accounts(conn):
    user = _user(conn, "alice", password="StrongPass1")

    with pytest.raises(AccountError) as exc:
        admin_create_user(
            conn,
            user["id"],
            "mallory",
            "MalloryPass1",
            "mallory@example.com",
            "Mallory",
        )
    assert exc.value.key == "admin.error_not_authorized"

    with pytest.raises(AccountError) as exc:
        admin_delete_user(conn, user["id"], user["id"])
    assert exc.value.key == "admin.error_not_authorized"


def test_hash_password_still_produces_expected_format():
    stored = hash_password("secret")
    algo, iterations, salt, digest = stored.split("$")
    assert algo == "pbkdf2"
    assert int(iterations) > 0
    assert len(salt) == 32
    assert len(digest) == 64
