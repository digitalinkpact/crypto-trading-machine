"""Reset an owner's password and mark their email verified.

Secrets are NEVER hardcoded. Provide them via CLI flags or environment
variables. The password can also be entered at an interactive prompt so it
never lands in shell history.

    # env vars
    RESET_EMAIL=you@example.com RESET_PASSWORD='...' python -m scripts.reset_password

    # CLI flag for email, prompt for password (recommended)
    python -m scripts.reset_password --email you@example.com

    # fully explicit (note: password may be visible in shell history)
    python -m scripts.reset_password --email you@example.com --password '...'
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from passlib.context import CryptContext

from app.storage import storage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        default=os.environ.get("RESET_EMAIL"),
        help="Owner email (or set RESET_EMAIL).",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("RESET_PASSWORD"),
        help="New password (or set RESET_PASSWORD, or omit to be prompted).",
    )
    args = parser.parse_args()

    email = args.email
    if not email:
        print("error: provide --email or set RESET_EMAIL", file=sys.stderr)
        return 2

    password = args.password or getpass.getpass("New password: ")
    if not password:
        print("error: empty password", file=sys.stderr)
        return 2

    pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
    user = storage.get_user_by_email(email)
    if not user:
        print(f"error: no user with email {email!r}", file=sys.stderr)
        return 1

    storage.update_user_password(user["id"], pwd.hash(password))
    storage.mark_email_verified(user["id"])
    print(f"reset password + verified email for user id={user['id']} ({email})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
