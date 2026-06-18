"""Session-based auth middleware. Replaces the old HTTP Basic guard.

Rules:
- /healthz is always public.
- All /auth/* pages are public (login, signup-while-open, verify, forgot, reset).
- /static/* (if ever added) is public.
- IPs in `auth_ip_allowlist` skip the wall entirely.
- Everything else requires a valid session cookie. HTML requests get a 303
  redirect to /auth/login; API/JSON requests get a 401.
- When `force_https` is on, plain-HTTP requests are bounced to https://.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings

from .service import client_ip, current_user, ip_is_allowlisted

_PUBLIC_PREFIXES = ("/auth/", "/static/")
_PUBLIC_EXACT = {"/healthz", "/favicon.ico"}


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or request.method == "GET" and "application/json" not in accept


async def auth_guard(
    request: Request,
    call_next: Callable[[Request], Awaitable],
):
    s = get_settings()

    # HTTPS enforcement (assumes a reverse proxy terminates TLS).
    if s.force_https:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        if proto != "https":
            target = request.url.replace(scheme="https")
            return RedirectResponse(url=str(target), status_code=308)

    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    if ip_is_allowlisted(client_ip(request)):
        return await call_next(request)

    if current_user(request) is not None:
        return await call_next(request)

    if _wants_html(request):
        # Only round-trip the user back to GET-able pages. A POST action
        # endpoint (e.g. /autopilot/stop) captured as `next` would be re-issued
        # as a GET after login — hitting a POST-only route and returning 405.
        # Send blocked non-GET actions to the dashboard instead.
        if request.method == "GET":
            nxt = request.url.path
            if request.url.query:
                nxt = f"{nxt}?{request.url.query}"
            return RedirectResponse(url=f"/auth/login?next={nxt}", status_code=303)
        return RedirectResponse(url="/auth/login", status_code=303)

    return JSONResponse(
        status_code=401,
        content={"detail": "authentication required"},
    )
