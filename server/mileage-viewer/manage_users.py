"""Manage everyday logins without importing viewer startup configuration."""

import os
import sys

import users


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    commands = {"add", "reset-password", "disable", "enable", "remove"}
    if not args or not ((args[0] == "list" and len(args) == 1)
                        or (args[0] in commands and len(args) == 2)):
        print("Usage: manage_users.py list | {add|reset-password|disable|enable|remove} <username>", file=sys.stderr)
        return 2
    try:
        db_path = os.environ.get("VIEWER_DB_PATH", "/db/annotations.db")
        admin_username = os.environ.get("VIEWER_USERNAME", "").strip()
        users.init_db(db_path)
        if args[0] == "list":
            for user in users.list_users(db_path):
                print(f"{user['username']}\t{'disabled' if user['disabled'] else 'enabled'}")
        else:
            password = None
            if args[0] in {"add", "reset-password"}:
                # Keep embedded newlines and a lone final CR intact. TextIO's
                # universal newline conversion would change the given secret.
                raw = sys.stdin.buffer.read()
                if raw.endswith(b"\n"):
                    raw = raw[:-1]
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                try:
                    password = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise users.UserError("Password must be UTF-8 text.") from None
            users.change_user(db_path, args[0], args[1], password, admin_username)
        return 0
    except users.UserError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except Exception:
        # Database and IO exceptions may contain private paths or values.
        print("Unable to manage logins.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Keep the line-oriented CLI contract identical on Windows and Linux.
    sys.stdout.reconfigure(newline="\n")
    sys.exit(main())
