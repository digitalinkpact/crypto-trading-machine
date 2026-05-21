"""Core auth service — single owner account, sessions, tokens, lockout, audit.

Why this shape:
- Single-owner system → no signup form after the first user exists.
- Email verification is required before login is allowed.
- Sessions are random opaque tokens; only their SHA-256 hash lives in the DB.
- Tokens (verify/reset) follow the same rule: raw token in the email link,
  only its hash is persisted, single-use.
- Lockout after N failed logins; counter resets on success or password change.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from email_validator import EmailNotValidError, validate_email
from fastapi import Request
from passlib.context import CryptContext

from app.config import get_settings
from app.logging_setup import get_logger
from app.storage import storage

from . import email as email_mod

log = get_logger(__name__)

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Sentinel module-level flag used by routes to decide whether to render the
# bootstrap signup form. Re-evaluated each request via `signup_open()`.
OWNER_BOOTSTRAP_OPEN = True


# ── Errors ────────────────────────────────────────────────────────────
class AuthError(Exception):
    """Base auth error."""


class InvalidCredentials(AuthError):
    pass


class LockedOut(AuthError):
    def __init__(self, locked_until: str) -> None:
        super().__init__(f"locked until {locked_until}")
        self.locked_until = locked_until


class EmailNotVerified(AuthError):
    pass


class SignupClosed(AuthError):
    pass


# ── Helpers ───────────────────────────────────────────────────────────
def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _validate_email_strict(raw: str) -> str:
    try:
        # check_deliverability=False keeps it offline-safe for tests.
        info = validate_email(raw, check_deliverability=False)
    except EmailNotValidError as exc:
        raise AuthError(f"invalid email: {exc}") from exc
    return info.normalized.lower()


def _validate_password(pw: str) -> None:
    if len(pw) < 12:
        raise AuthError("password must be at least 12 characters")
    if pw.lower() == pw or pw.upper() == pw or pw.isalpha() or pw.isdigit():
        raise AuthError("password must mix upper, lower, and digit characters")


def signup_open() -> bool:
    """Bootstrap signup is open only until the first user is created."""
    return storage.user_count() == 0


def ip_is_allowlisted(ip: Optional[str]) -> bool:
    s = get_settings()
    if not s.auth_ip_allowlist or not ip:
        return False
    allowed = {x.strip() for x in s.auth_ip_allowlist.split(",") if x.strip()}
    if ip in allowed:
        return True
    # Also accept CIDR ranges.
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowed:
        if "/" in entry:
            try:
                if ip_obj in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
    return False


def client_ip(request: Request) -> Optional[str]:
    """Trust X-Forwarded-For only when force_https is on (i.e. behind proxy)."""
    s = get_settings()
    if s.force_https:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else None


# ── Session helpers ───────────────────────────────────────────────────
@dataclass
class IssuedSession:
    raw_token: str
    expires_at: datetime


def _issue_session(
    *, user_id: int, remember: bool, ip: Optional[str], user_agent: Optional[str]
) -> IssuedSession:
    s = get_settings()
    ttl = timedelta(days=s.auth_remember_days) if remember else timedelta(hours=s.auth_session_hours)
    exp = _now() + ttl
    raw = secrets.token_urlsafe(32)
    storage.create_session(
        token_hash=_sha(raw),
        user_id=user_id,
        expires_at=_iso(exp),
        ip=ip,
        user_agent=user_agent,
    )
    return IssuedSession(raw_token=raw, expires_at=exp)


def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("session")
    if not token:
        return None
    sess = storage.get_session(_sha(token))
    if not sess:
        return None
    return storage.get_user(int(sess["user_id"]))


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise AuthError("not authenticated")
    return u


# ── Public operations ─────────────────────────────────────────────────
async def signup(*, email: str, password: str, ip: Optional[str]) -> dict:
    """Bootstrap signup — only allowed when no users exist yet."""
    if not signup_open():
        raise SignupClosed("signup is closed (owner account already exists)")
    email = _validate_email_strict(email)
    _validate_password(password)
    user_id = storage.create_user(email=email, password_hash=_pwd.hash(password))
    storage.record_audit(action="signup", user_id=user_id, ip=ip, detail=email)
    await _send_verify_email(user_id=user_id, email=email)
    return {"user_id": user_id, "email": email}


async def _send_verify_email(*, user_id: int, email: str) -> None:
    s = get_settings()
    raw = secrets.token_urlsafe(32)
    exp = _now() + timedelta(minutes=s.auth_token_minutes)
    storage.create_auth_token(
        user_id=user_id, purpose="verify", token_hash=_sha(raw), expires_at=_iso(exp)
    )
    link = f"{s.base_url.rstrip('/')}/auth/verify?token={raw}"
    body = (
        f"Welcome to AI Crypto Trading Machine.\n\n"
        f"Click to verify your email (valid {s.auth_token_minutes} min):\n{link}\n\n"
        f"If you did not request this, ignore the email."
    )
    await email_mod.send(to=email, subject="Verify your email", body=body)


async def resend_verification(email: str) -> None:
    user = storage.get_user_by_email(email)
    if not user or int(user["email_verified"]) == 1:
        return  # Silent — don't leak account state.
    await _send_verify_email(user_id=int(user["id"]), email=user["email"])


def verify_email_token(raw_token: str, *, ip: Optional[str]) -> Optional[dict]:
    user_id = storage.consume_auth_token(token_hash=_sha(raw_token), purpose="verify")
    if user_id is None:
        return None
    storage.mark_email_verified(user_id)
    storage.record_audit(action="email_verified", user_id=user_id, ip=ip)
    return storage.get_user(user_id)


async def login(
    *,
    email: str,
    password: str,
    remember: bool,
    ip: Optional[str],
    user_agent: Optional[str],
) -> IssuedSession:
    s = get_settings()
    user = storage.get_user_by_email(email)
    if not user:
        # Constant-time compare against a dummy hash to avoid user enumeration.
        _pwd.verify(password, "$2b$12$" + "a" * 53)
        storage.record_audit(action="login_failed", ip=ip, detail=email)
        raise InvalidCredentials("invalid email or password")

    locked = user.get("locked_until")
    if locked and locked > _iso(_now()):
        storage.record_audit(
            action="login_locked", user_id=int(user["id"]), ip=ip, detail=email
        )
        raise LockedOut(locked)

    if not _pwd.verify(password, user["password_hash"]):
        info = storage.record_login_failure(
            int(user["id"]),
            max_failed=s.auth_max_failed,
            lockout_minutes=s.auth_lockout_minutes,
        )
        storage.record_audit(
            action="login_failed",
            user_id=int(user["id"]),
            ip=ip,
            detail=f"attempts={info['attempts']}",
        )
        raise InvalidCredentials("invalid email or password")

    if int(user["email_verified"]) != 1:
        storage.record_audit(
            action="login_unverified", user_id=int(user["id"]), ip=ip
        )
        raise EmailNotVerified("email not verified")

    storage.record_login_success(int(user["id"]))
    sess = _issue_session(
        user_id=int(user["id"]), remember=remember, ip=ip, user_agent=user_agent
    )
    storage.record_audit(action="login_ok", user_id=int(user["id"]), ip=ip)
    return sess


def logout(request: Request) -> None:
    token = request.cookies.get("session")
    if not token:
        return
    sess = storage.get_session(_sha(token))
    user_id = int(sess["user_id"]) if sess else None
    storage.delete_session(_sha(token))
    storage.record_audit(
        action="logout", user_id=user_id, ip=client_ip(request)
    )


async def request_password_reset(*, email: str, ip: Optional[str]) -> None:
    """Always succeeds silently to avoid user enumeration."""
    user = storage.get_user_by_email(email)
    if not user:
        log.info("password reset requested for unknown email")
        return
    s = get_settings()
    raw = secrets.token_urlsafe(32)
    exp = _now() + timedelta(minutes=s.auth_token_minutes)
    storage.create_auth_token(
        user_id=int(user["id"]),
        purpose="reset",
        token_hash=_sha(raw),
        expires_at=_iso(exp),
    )
    storage.record_audit(
        action="reset_requested", user_id=int(user["id"]), ip=ip
    )
    link = f"{s.base_url.rstrip('/')}/auth/reset?token={raw}"
    body = (
        f"Password reset request.\n\n"
        f"Click to set a new password (valid {s.auth_token_minutes} min):\n{link}\n\n"
        f"If you did not request this, ignore the email."
    )
    await email_mod.send(to=user["email"], subject="Reset your password", body=body)


def consume_password_reset(
    *, raw_token: str, new_password: str, ip: Optional[str]
) -> Optional[dict]:
    user_id = storage.consume_auth_token(token_hash=_sha(raw_token), purpose="reset")
    if user_id is None:
        return None
    _validate_password(new_password)
    storage.update_user_password(user_id, _pwd.hash(new_password))
    storage.delete_user_sessions(user_id)  # invalidate everything
    storage.record_audit(action="password_reset", user_id=user_id, ip=ip)
    return storage.get_user(user_id)


def change_password(
    *, user_id: int, current_password: str, new_password: str, ip: Optional[str]
) -> None:
    user = storage.get_user(user_id)
    if not user or not _pwd.verify(current_password, user["password_hash"]):
        raise InvalidCredentials("current password is incorrect")
    _validate_password(new_password)
    if hmac.compare_digest(current_password, new_password):
        raise AuthError("new password must differ from current password")
    storage.update_user_password(user_id, _pwd.hash(new_password))
    storage.record_audit(action="password_changed", user_id=user_id, ip=ip)
