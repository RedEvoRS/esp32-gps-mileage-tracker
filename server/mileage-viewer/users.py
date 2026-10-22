"""Everyday login storage shared by the viewer and its standalone CLI."""

import hmac
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash


def init_schema(conn):
    """Called inside the caller's BEGIN IMMEDIATE schema transaction."""
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0,
        session_version INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")


def init_db(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=30)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        init_schema(conn)


class UserError(ValueError):
    def __init__(self, message, code=3):
        super().__init__(message)
        self.code = code


def validate_username(username, admin_username=""):
    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", username):
        raise UserError("Username must be 1-64 letters, digits, or . _ @ -.")
    if username.lower().startswith("replace-me"):
        raise UserError("Username must not start with replace-me.")
    if admin_username and username.casefold() == admin_username.casefold():
        raise UserError("Username clashes with the admin login.")


def validate_password(password):
    if not 8 <= len(password) <= 1024:
        raise UserError("Password must be 8-1024 characters.")


def get_user(db_path, username):
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_users(db_path):
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT username, disabled, created_at FROM users ORDER BY username COLLATE NOCASE").fetchall()


def session_tag(secret_key, user):
    payload = user["username"] + user["password_hash"] + str(user["session_version"])
    return hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), "sha256").hexdigest()[:32]


def change_user(db_path, action, username, password=None, admin_username="", expected_tag=None, secret_key=None):
    """Serialize mutations with session revocation and optional stale-session check.

    Hash before reserving the writer, so slow scrypt does not block requests.
    The own-password form supplies its session tag: a concurrent reset must
    never be overwritten by a request authenticated under the old password.
    """
    if action not in {"add", "reset-password", "disable", "enable", "remove"}:
        raise UserError("Unknown command.", 2)
    validate_username(username, admin_username if action == "add" else "")
    password_hash = None
    if action in {"add", "reset-password"}:
        validate_password(password)
        password_hash = generate_password_hash(password)
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(db_path, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if action == "add":
            if user is not None:
                raise UserError("Login already exists.", 5)
            conn.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         (username, password_hash, now, now))
        else:
            if user is None:
                raise UserError("No such login.", 4)
            if expected_tag is not None and (user["disabled"] or not hmac.compare_digest(
                    expected_tag, session_tag(secret_key, user))):
                raise UserError("Login changed. Sign in again.")
            if action == "remove":
                # Increment in the same transaction before deletion, as for
                # other mutations. Existence is also checked on every request.
                conn.execute("UPDATE users SET session_version=session_version+1 WHERE username=?", (username,))
                conn.execute("DELETE FROM users WHERE username=?", (username,))
            elif action == "reset-password":
                conn.execute("UPDATE users SET password_hash=?, session_version=session_version+1, updated_at=? WHERE username=?",
                             (password_hash, now, username))
            else:
                conn.execute("UPDATE users SET disabled=?, session_version=session_version+1, updated_at=? WHERE username=?",
                             (int(action == "disable"), now, username))
        # Return exactly the committed version, never a later concurrent reset.
        return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
