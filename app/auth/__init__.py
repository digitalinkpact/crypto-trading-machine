"""Authentication: single-owner login, email verification, password reset,
sessions, lockout, IP allow-list bypass, audit log."""
from .service import (
    AuthError,
    LockedOut,
    InvalidCredentials,
    EmailNotVerified,
    SignupClosed,
    current_user,
    require_user,
    signup,
    login,
    logout,
    verify_email_token,
    request_password_reset,
    consume_password_reset,
    change_password,
    ip_is_allowlisted,
    OWNER_BOOTSTRAP_OPEN,
)
from .routes import router as auth_router
from .middleware import auth_guard

__all__ = [
    "AuthError",
    "LockedOut",
    "InvalidCredentials",
    "EmailNotVerified",
    "SignupClosed",
    "current_user",
    "require_user",
    "signup",
    "login",
    "logout",
    "verify_email_token",
    "request_password_reset",
    "consume_password_reset",
    "change_password",
    "ip_is_allowlisted",
    "OWNER_BOOTSTRAP_OPEN",
    "auth_router",
    "auth_guard",
]
