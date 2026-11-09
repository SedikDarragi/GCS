"""Account model, password hashing and authentication helpers.

The UI layers only render forms and display errors; every account rule that
matters for security (password hashing, normalisation, uniqueness, lockout,
role/last-admin guards) lives here so it can be tested headlessly.
"""

import hashlib
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta

PBKDF2_ALGO = "pbkdf2"
PBKDF2_ITERATIONS = 600000

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
NAME_MAX_LENGTH = 120
EMAIL_MAX_LENGTH = 254
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_EMAIL = "admin@example.com"
DEFAULT_ADMIN_NAME = "Administrateur"
INITIAL_PASSWORD_ENV = "GCS_INITIAL_ADMIN_PASSWORD"

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_COMMON_PASSWORDS = {
    "12345678",
    "123456789",
    "admin123",
    "letmein1",
    "password",
    "password1",
    "password123",
    "qwerty123",
    "welcome1",
}

_USER_COLUMNS = (
    "id, username, password, email, full_name, is_admin, is_active, "
    "must_change_password, failed_attempts, locked_until, last_login_at, "
    "password_changed_at, created_at"
)

# Added to databases created by older releases. ``is_admin`` is included in
# case a very old database predates it.
_USER_MIGRATIONS = {
    "is_admin": "INTEGER NOT NULL DEFAULT 0",
    "is_active": "INTEGER NOT NULL DEFAULT 1",
    "must_change_password": "INTEGER NOT NULL DEFAULT 0",
    "failed_attempts": "INTEGER NOT NULL DEFAULT 0",
    "locked_until": "TEXT",
    "last_login_at": "TEXT",
    "password_changed_at": "TEXT",
    "created_at": "TEXT",
}

_DUMMY_HASH = None


class AccountError(Exception):
    """A validation/authorisation failure carrying an i18n key."""

    def __init__(self, key, **kwargs):
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password):
    if not isinstance(password, str):
        password = str(password)
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "{}${}${}${}".format(
        PBKDF2_ALGO, PBKDF2_ITERATIONS, salt.hex(), dk.hex()
    )


def verify_password(password, stored):
    if not isinstance(password, str):
        password = "" if password is None else str(password)
    if not isinstance(stored, str) or not stored:
        return False

    if not stored.startswith(PBKDF2_ALGO + "$"):
        # Legacy plaintext accounts are migrated on their next successful login.
        return secrets.compare_digest(password, stored)

    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$", 3)
        if algo != PBKDF2_ALGO:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        iterations = int(iterations)
        if iterations <= 0 or iterations > 10_000_000:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return secrets.compare_digest(dk, expected)
    except Exception:
        return False


def is_password_hashed(stored):
    return isinstance(stored, str) and stored.startswith(PBKDF2_ALGO + "$")


def _verify_dummy(password):
    """Burn roughly the same time as a real verification for unknown users."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _DUMMY_HASH)


# ---------------------------------------------------------------------------
# Validation / normalisation
# ---------------------------------------------------------------------------
def normalize_username(value):
    return (value or "").strip()


def normalize_email(value):
    return (value or "").strip().lower()


def normalize_full_name(value):
    return " ".join((value or "").split())


def validate_username(username):
    username = normalize_username(username)
    if not username:
        return "validation.username_required"
    if len(username) < USERNAME_MIN_LENGTH:
        return "validation.username_too_short"
    if len(username) > USERNAME_MAX_LENGTH:
        return "validation.username_too_long"
    if not _USERNAME_RE.fullmatch(username):
        return "validation.username_invalid"
    return None


def validate_email(email):
    email = normalize_email(email)
    if not email:
        return "validation.email_required"
    if len(email) > EMAIL_MAX_LENGTH:
        return "validation.email_too_long"
    if not _EMAIL_RE.fullmatch(email):
        return "validation.email_invalid"
    return None


def validate_full_name(full_name):
    full_name = normalize_full_name(full_name)
    if not full_name:
        return "validation.fullname_required"
    if len(full_name) > NAME_MAX_LENGTH:
        return "validation.fullname_too_long"
    return None


def validate_password(password, confirm_password=None):
    if not isinstance(password, str) or not password:
        return "validation.password_required"
    if not password.strip():
        return "validation.password_required"
    if len(password) < PASSWORD_MIN_LENGTH:
        return "validation.password_too_short"
    if len(password) > PASSWORD_MAX_LENGTH:
        return "validation.password_too_long"
    if password.lower() in _COMMON_PASSWORDS:
        return "validation.password_weak"
    if confirm_password is not None and password != confirm_password:
        return "validation.password_mismatch"
    return None


# ---------------------------------------------------------------------------
# Schema / row helpers
# ---------------------------------------------------------------------------
def ensure_account_schema(conn):
    """Create or migrate the ``users`` table and its indexes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            last_login_at TEXT,
            password_changed_at TEXT,
            created_at TEXT
        )
        """
    )

    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    added = set()
    for column, ddl in _USER_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
            added.add(column)

    # Case-insensitive uniqueness. Legacy databases with duplicate rows are
    # still usable; the account helpers also check manually in that case.
    for index_name, column in (
        ("idx_users_username_nocase", "username"),
        ("idx_users_email_nocase", "email"),
    ):
        try:
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                f"ON users({column} COLLATE NOCASE)"
            )
        except sqlite3.IntegrityError:
            pass

    if "must_change_password" in added:
        # Upgrading an installation still using the published default password
        # must rotate it before entering the application.
        try:
            row = conn.execute(
                "SELECT id, password FROM users WHERE username = ? COLLATE NOCASE",
                (DEFAULT_ADMIN_USERNAME,),
            ).fetchone()
            if row and verify_password("admin123", row[1]):
                conn.execute(
                    "UPDATE users SET must_change_password = 1 WHERE id = ?",
                    (row[0],),
                )
        except sqlite3.Error:
            pass

    conn.commit()


def _now():
    return datetime.now().strftime(_DATETIME_FORMAT)


def _parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, _DATETIME_FORMAT)
    except (TypeError, ValueError):
        return None


def _row_to_user(row):
    if not row:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "password": row[2],
        "email": row[3] or "",
        "full_name": row[4] or "",
        "is_admin": bool(row[5]),
        "is_active": True if row[6] is None else bool(row[6]),
        "must_change_password": bool(row[7]),
        "failed_attempts": int(row[8] or 0),
        "locked_until": row[9],
        "last_login_at": row[10],
        "password_changed_at": row[11],
        "created_at": row[12],
    }


def _public_user(user):
    if user is None:
        return None
    return {key: value for key, value in user.items() if key != "password"}


def get_user_by_id(conn, user_id):
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return _row_to_user(row)


def get_public_user_by_id(conn, user_id):
    return _public_user(get_user_by_id(conn, user_id))


def get_user_by_username(conn, username):
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE username = ? COLLATE NOCASE",
        (normalize_username(username),),
    ).fetchone()
    return _row_to_user(row)


def _ensure_username_available(conn, username, exclude_id=None):
    if exclude_id is None:
        row = conn.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE AND id <> ?",
            (username, exclude_id),
        ).fetchone()
    if row:
        raise AccountError("validation.username_taken")


def _ensure_email_available(conn, email, exclude_id=None):
    if exclude_id is None:
        row = conn.execute(
            "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE AND id <> ?",
            (email, exclude_id),
        ).fetchone()
    if row:
        raise AccountError("validation.email_taken")


# ---------------------------------------------------------------------------
# Account creation / authentication
# ---------------------------------------------------------------------------
def create_user(
    conn,
    username,
    password,
    email,
    full_name,
    is_admin=False,
    must_change_password=False,
):
    username = normalize_username(username)
    email = normalize_email(email)
    full_name = normalize_full_name(full_name)

    for error in (
        validate_username(username),
        validate_email(email),
        validate_full_name(full_name),
        validate_password(password),
    ):
        if error:
            raise AccountError(error)

    _ensure_username_available(conn, username)
    _ensure_email_available(conn, email)

    now = _now()
    try:
        cursor = conn.execute(
            """
            INSERT INTO users (
                username, password, email, full_name, is_admin, is_active,
                must_change_password, failed_attempts, created_at,
                password_changed_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, 0, ?, ?)
            """,
            (
                username,
                hash_password(password),
                email,
                full_name,
                1 if is_admin else 0,
                1 if must_change_password else 0,
                now,
                now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        message = str(exc).lower()
        if "username" in message:
            raise AccountError("validation.username_taken") from exc
        if "email" in message:
            raise AccountError("validation.email_taken") from exc
        raise

    return get_public_user_by_id(conn, cursor.lastrowid)


def authenticate_user(conn, username, password, now=None):
    """Authenticate an account.

    Returns ``(user, status, info)`` where status is ``ok``, ``invalid``,
    ``inactive`` or ``locked``. Failed attempts and lockouts are persisted.
    """
    now = now or datetime.now()
    user = get_user_by_username(conn, username)
    if not user:
        _verify_dummy(password)
        return None, "invalid", {}

    if not user["is_active"]:
        return None, "inactive", {}

    locked_until = _parse_timestamp(user["locked_until"])
    if locked_until and locked_until > now:
        remaining = int((locked_until - now).total_seconds())
        minutes = max(1, (remaining + 59) // 60)
        return None, "locked", {"minutes": minutes}
    if locked_until:
        # The lockout has expired: start a fresh attempt window.
        conn.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (user["id"],),
        )
        conn.commit()
        user["failed_attempts"] = 0
        user["locked_until"] = None

    if not verify_password(password, user["password"]):
        attempts = user["failed_attempts"] + 1
        if attempts >= MAX_FAILED_ATTEMPTS:
            until = (now + timedelta(seconds=LOCKOUT_SECONDS)).strftime(
                _DATETIME_FORMAT
            )
            conn.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                (attempts, until, user["id"]),
            )
            conn.commit()
            minutes = max(1, (LOCKOUT_SECONDS + 59) // 60)
            return None, "locked", {"minutes": minutes}

        conn.execute(
            "UPDATE users SET failed_attempts = ? WHERE id = ?",
            (attempts, user["id"]),
        )
        conn.commit()
        return None, "invalid", {
            "attempts_left": MAX_FAILED_ATTEMPTS - attempts
        }

    updates = [
        "failed_attempts = 0",
        "locked_until = NULL",
        "last_login_at = ?",
    ]
    params = [_now()]
    if not is_password_hashed(user["password"]):
        updates.extend(["password = ?", "password_changed_at = ?"])
        params.extend([hash_password(password), _now()])
    params.append(user["id"])
    conn.execute(
        f"UPDATE users SET {', '.join(updates)} WHERE id = ?", tuple(params)
    )
    conn.commit()

    return get_public_user_by_id(conn, user["id"]), "ok", {}


# ---------------------------------------------------------------------------
# Password changes
# ---------------------------------------------------------------------------
def _store_new_password(
    conn, user_id, password, confirm_password=None, must_change_password=False
):
    error = validate_password(password, confirm_password)
    if error:
        raise AccountError(error)

    user = get_user_by_id(conn, user_id)
    if not user:
        raise AccountError("validation.user_not_found")
    if verify_password(password, user["password"]):
        raise AccountError("validation.password_unchanged")

    now = _now()
    conn.execute(
        """
        UPDATE users
        SET password = ?, password_changed_at = ?, must_change_password = ?,
            failed_attempts = 0, locked_until = NULL
        WHERE id = ?
        """,
        (
            hash_password(password),
            now,
            1 if must_change_password else 0,
            user_id,
        ),
    )
    conn.commit()
    return get_public_user_by_id(conn, user_id)


def change_password(conn, user_id, current_password, new_password, confirm_password=None):
    user = get_user_by_id(conn, user_id)
    if not user:
        raise AccountError("validation.user_not_found")
    if not verify_password(current_password, user["password"]):
        raise AccountError("validation.current_password_invalid")
    return _store_new_password(
        conn, user_id, new_password, confirm_password, must_change_password=False
    )


def reset_user_password(
    conn, user_id, new_password, confirm_password=None, must_change_password=True
):
    return _store_new_password(
        conn,
        user_id,
        new_password,
        confirm_password,
        must_change_password=must_change_password,
    )


# ---------------------------------------------------------------------------
# Administration / authorisation
# ---------------------------------------------------------------------------
def require_admin(conn, user_id):
    user = get_user_by_id(conn, user_id)
    if not user or not user["is_active"] or not user["is_admin"]:
        raise AccountError("admin.error_not_authorized")
    return user


def count_active_admins(conn):
    row = conn.execute(
        "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1"
    ).fetchone()
    return int(row[0]) if row else 0


def seed_default_admin(conn):
    """Create the first administrator if there are no accounts.

    Uses ``GCS_INITIAL_ADMIN_PASSWORD`` when it satisfies the password policy.
    Otherwise a cryptographically random password is generated and returned so
    the caller can show it once.
    """
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count:
        return None

    configured = os.environ.get(INITIAL_PASSWORD_ENV)
    generated = True
    password = configured
    if configured and validate_password(configured) is None:
        generated = False
    elif configured:
        password = None
    if not password:
        password = secrets.token_urlsafe(16)

    user = create_user(
        conn,
        DEFAULT_ADMIN_USERNAME,
        password,
        DEFAULT_ADMIN_EMAIL,
        DEFAULT_ADMIN_NAME,
        is_admin=True,
        must_change_password=True,
    )
    return {"user": user, "password": password, "generated": generated}


def admin_create_user(
    conn,
    actor_id,
    username,
    password,
    email,
    full_name,
    is_admin=False,
    must_change_password=True,
):
    require_admin(conn, actor_id)
    return create_user(
        conn,
        username,
        password,
        email,
        full_name,
        is_admin=is_admin,
        must_change_password=must_change_password,
    )


def admin_update_user(
    conn,
    actor_id,
    target_id,
    email,
    full_name,
    is_admin,
    is_active,
    new_password=None,
    confirm_password=None,
):
    actor = require_admin(conn, actor_id)
    target = get_user_by_id(conn, target_id)
    if not target:
        raise AccountError("validation.user_not_found")

    email = normalize_email(email)
    full_name = normalize_full_name(full_name)
    for error in (validate_email(email), validate_full_name(full_name)):
        if error:
            raise AccountError(error)

    if target_id == actor["id"] and not is_active:
        raise AccountError("admin.error_self_deactivate")

    if (
        target["is_admin"]
        and target["is_active"]
        and (not is_admin or not is_active)
        and count_active_admins(conn) <= 1
    ):
        raise AccountError("admin.error_last_admin")

    if new_password:
        error = validate_password(new_password, confirm_password)
        if error:
            raise AccountError(error)
        if verify_password(new_password, target["password"]):
            raise AccountError("validation.password_unchanged")

    _ensure_email_available(conn, email, exclude_id=target_id)

    conn.execute(
        """
        UPDATE users SET email = ?, full_name = ?, is_admin = ?, is_active = ?
        WHERE id = ?
        """,
        (
            email,
            full_name,
            1 if is_admin else 0,
            1 if is_active else 0,
            target_id,
        ),
    )

    if new_password:
        _store_new_password(
            conn, target_id, new_password, confirm_password, must_change_password=True
        )
    else:
        conn.commit()

    return get_public_user_by_id(conn, target_id)


def admin_delete_user(conn, actor_id, target_id):
    actor = require_admin(conn, actor_id)
    if target_id == actor["id"]:
        raise AccountError("admin.error_self_delete")

    target = get_user_by_id(conn, target_id)
    if not target:
        raise AccountError("validation.user_not_found")
    if target["is_admin"] and target["is_active"] and count_active_admins(conn) <= 1:
        raise AccountError("admin.error_last_admin")

    conn.execute("DELETE FROM users WHERE id = ?", (target_id,))
    conn.commit()
