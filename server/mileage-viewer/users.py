"""Everyday login storage shared by the viewer and its standalone CLI."""

import hmac
import re
import sqlite3
from contextlib import closing
from pathlib import Path


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
