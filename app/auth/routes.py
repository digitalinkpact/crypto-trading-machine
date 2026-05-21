"""HTML auth routes — login, logout, signup (bootstrap), verify, forgot, reset,
change password, audit log."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.storage import storage

from . import service
from .service import (
    AuthError,
    EmailNotVerified,
    InvalidCredentials,
    LockedOut,
    SignupClosed,
    client_ip,
    current_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Page chrome (kept self-contained so the dashboard CSS isn't required) ──
_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0b1020; color: #e6e8ef; min-height: 100vh;
       display: flex; align-items: center; justify-content: center; padding: 1.5rem; }
.card { background: #121831; border: 1px solid #1d2540; border-radius: 12px;
        padding: 1.75rem; max-width: 420px; width: 100%; }
h1 { margin: 0 0 0.25rem; font-size: 1.15rem; }
p.muted { color: #8a93b2; font-size: 0.9rem; margin: 0 0 1rem; }
label { display: block; font-size: 0.85rem; color: #8a93b2; margin-top: 0.85rem; }
input[type=text], input[type=email], input[type=password] {
  width: 100%; padding: 0.65rem 0.75rem; background: #0b1020;
  border: 1px solid #1d2540; border-radius: 6px; color: #e6e8ef;
  font-size: 0.95rem; margin-top: 0.25rem; }
input[type=checkbox] { margin-right: 0.4rem; }
button { display: block; width: 100%; padding: 0.85rem;
         font-size: 1rem; font-weight: 600; border: 0; border-radius: 8px;
         margin-top: 1.25rem; background: #0e7a3f; color: white; cursor: pointer; }
button:hover { background: #109a4e; }
.banner { padding: 0.75rem 0.9rem; border-radius: 8px; margin-bottom: 1rem;
          font-size: 0.9rem; }
.banner.warn { background: #3b2a0e; color: #f0c45b; border: 1px solid #5a4214; }
.banner.ok { background: #0e3b27; color: #5be29a; border: 1px solid #14593a; }
.banner.danger { background: #3b0e1a; color: #ff7a8a; border: 1px solid #5a1424; }
a { color: #7aa6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.row-links { margin-top: 1.1rem; font-size: 0.85rem;
             display: flex; justify-content: space-between; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'/>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
        f"<title>{title}</title><style>{_CSS}</style></head>"
        f"<body><div class='card'>{body}</div></body></html>"
    )


def _banner(msg: str, kind: str = "warn") -> str:
    return f"<div class='banner {kind}'>{msg}</div>"


# ── Cookie helpers ────────────────────────────────────────────────────
def _set_session_cookie(response, *, token: str, expires) -> None:
    s = get_settings()
    response.set_cookie(
        key="session",
        value=token,
        expires=expires,
        httponly=True,
        secure=s.force_https,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie("session", path="/")


# ── Login ─────────────────────────────────────────────────────────────
@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/", error: str = "", ok: str = "") -> str:
    if current_user(request):
        return RedirectResponse(url=next or "/", status_code=303)
    if service.signup_open():
        return RedirectResponse(url="/auth/signup", status_code=303)

    banner = ""
    if error:
        banner = _banner(error, "danger")
    if ok:
        banner = _banner(ok, "ok")

    body = f"""
    <h1>Sign in</h1>
    <p class='muted'>Owner login required to access the trading machine.</p>
    {banner}
    <form method='post' action='/auth/login'>
      <input type='hidden' name='next' value='{next}'/>
      <label>Email
        <input type='email' name='email' autocomplete='username' required autofocus/>
      </label>
      <label>Password
        <input type='password' name='password' autocomplete='current-password' required/>
      </label>
      <label style='display:flex;align-items:center;margin-top:1rem;'>
        <input type='checkbox' name='remember' value='1'/> Remember me
      </label>
      <button>Sign in</button>
    </form>
    <div class='row-links'>
      <a href='/auth/forgot'>Forgot password?</a>
      <a href='/healthz'>Status</a>
    </div>
    """
    return _page("Sign in", body)


@router.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    remember: str = Form(""),
):
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    try:
        sess = await service.login(
            email=email,
            password=password,
            remember=bool(remember),
            ip=ip,
            user_agent=ua,
        )
    except LockedOut as exc:
        url = f"/auth/login?error=Account+locked+until+{exc.locked_until}"
        return RedirectResponse(url=url, status_code=303)
    except EmailNotVerified:
        url = "/auth/login?error=Email+not+verified.+Check+your+inbox."
        return RedirectResponse(url=url, status_code=303)
    except InvalidCredentials:
        return RedirectResponse(
            url="/auth/login?error=Invalid+email+or+password", status_code=303
        )

    # Treat `next` as a safe path-only redirect.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(url=target, status_code=303)
    _set_session_cookie(resp, token=sess.raw_token, expires=sess.expires_at)
    return resp


@router.post("/logout", include_in_schema=False)
@router.get("/logout", include_in_schema=False)
async def logout_route(request: Request):
    service.logout(request)
    resp = RedirectResponse(url="/auth/login?ok=Signed+out", status_code=303)
    _clear_session_cookie(resp)
    return resp


# ── Signup (bootstrap, single owner) ──────────────────────────────────
@router.get("/signup", response_class=HTMLResponse, include_in_schema=False)
async def signup_page(error: str = "", ok: str = "") -> str:
    if not service.signup_open():
        return RedirectResponse(url="/auth/login", status_code=303)

    banner = ""
    if error:
        banner = _banner(error, "danger")
    if ok:
        banner = _banner(ok, "ok")

    body = f"""
    <h1>Create owner account</h1>
    <p class='muted'>One-time setup. After this is created, signup is closed.</p>
    {banner}
    <form method='post' action='/auth/signup'>
      <label>Email
        <input type='email' name='email' required autofocus/>
      </label>
      <label>Password
        <input type='password' name='password' required minlength='12'/>
      </label>
      <p class='muted' style='margin-top:0.5rem;font-size:0.8rem;'>
        Minimum 12 characters with mixed upper, lower, and digit.
      </p>
      <button>Create account</button>
    </form>
    """
    return _page("Create account", body)


@router.post("/signup", include_in_schema=False)
async def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    ip = client_ip(request)
    try:
        await service.signup(email=email, password=password, ip=ip)
    except SignupClosed:
        return RedirectResponse(url="/auth/login", status_code=303)
    except AuthError as exc:
        return RedirectResponse(
            url=f"/auth/signup?error={str(exc).replace(' ', '+')}", status_code=303
        )

    return RedirectResponse(
        url="/auth/login?ok=Account+created.+Check+your+email+to+verify.",
        status_code=303,
    )


# ── Verify email ──────────────────────────────────────────────────────
@router.get("/verify", response_class=HTMLResponse, include_in_schema=False)
async def verify_route(request: Request, token: str = "") -> str:
    if not token:
        return _page("Verify", _banner("Missing token", "danger"))
    ip = client_ip(request)
    user = service.verify_email_token(token, ip=ip)
    if not user:
        body = (
            _banner("This verification link is invalid or expired.", "danger")
            + "<a href='/auth/resend-verify'>Request a new link</a>"
        )
        return _page("Verify", body)
    body = (
        "<h1>Email verified</h1>"
        "<p class='muted'>You can now sign in.</p>"
        "<a href='/auth/login'>Go to login &rarr;</a>"
    )
    return _page("Verified", body)


@router.get("/resend-verify", response_class=HTMLResponse, include_in_schema=False)
async def resend_verify_page() -> str:
    body = """
    <h1>Resend verification</h1>
    <p class='muted'>Enter the account email to receive a new verification link.</p>
    <form method='post' action='/auth/resend-verify'>
      <label>Email <input type='email' name='email' required/></label>
      <button>Send link</button>
    </form>
    """
    return _page("Resend verification", body)


@router.post("/resend-verify", include_in_schema=False)
async def resend_verify_submit(email: str = Form(...)):
    await service.resend_verification(email)
    return RedirectResponse(
        url="/auth/login?ok=If+the+account+exists,+a+verification+link+has+been+sent.",
        status_code=303,
    )


# ── Password reset (forgot → reset) ───────────────────────────────────
@router.get("/forgot", response_class=HTMLResponse, include_in_schema=False)
async def forgot_page(ok: str = "") -> str:
    banner = _banner(ok, "ok") if ok else ""
    body = f"""
    <h1>Reset password</h1>
    <p class='muted'>We'll email a reset link if an account exists.</p>
    {banner}
    <form method='post' action='/auth/forgot'>
      <label>Email <input type='email' name='email' required autofocus/></label>
      <button>Send reset link</button>
    </form>
    <div class='row-links'>
      <a href='/auth/login'>Back to login</a>
    </div>
    """
    return _page("Forgot password", body)


@router.post("/forgot", include_in_schema=False)
async def forgot_submit(request: Request, email: str = Form(...)):
    ip = client_ip(request)
    await service.request_password_reset(email=email, ip=ip)
    return RedirectResponse(
        url="/auth/forgot?ok=If+the+account+exists,+a+reset+link+has+been+sent.",
        status_code=303,
    )


@router.get("/reset", response_class=HTMLResponse, include_in_schema=False)
async def reset_page(token: str = "", error: str = "") -> str:
    if not token:
        return _page("Reset", _banner("Missing token", "danger"))
    banner = _banner(error, "danger") if error else ""
    body = f"""
    <h1>Set a new password</h1>
    {banner}
    <form method='post' action='/auth/reset'>
      <input type='hidden' name='token' value='{token}'/>
      <label>New password
        <input type='password' name='password' required minlength='12' autofocus/>
      </label>
      <p class='muted' style='margin-top:0.5rem;font-size:0.8rem;'>
        Minimum 12 characters with mixed upper, lower, and digit.
      </p>
      <button>Update password</button>
    </form>
    """
    return _page("Reset password", body)


@router.post("/reset", include_in_schema=False)
async def reset_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
):
    ip = client_ip(request)
    try:
        user = service.consume_password_reset(
            raw_token=token, new_password=password, ip=ip
        )
    except AuthError as exc:
        return RedirectResponse(
            url=f"/auth/reset?token={token}&error={str(exc).replace(' ', '+')}",
            status_code=303,
        )
    if not user:
        return RedirectResponse(
            url="/auth/forgot?ok=Link+expired.+Request+a+new+reset+link.",
            status_code=303,
        )
    return RedirectResponse(
        url="/auth/login?ok=Password+updated.+Sign+in+with+the+new+password.",
        status_code=303,
    )


# ── Change password (logged in) ───────────────────────────────────────
@router.get("/password", response_class=HTMLResponse, include_in_schema=False)
async def change_password_page(request: Request, error: str = "", ok: str = "") -> str:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login?next=/auth/password", status_code=303)
    banner = ""
    if error:
        banner = _banner(error, "danger")
    if ok:
        banner = _banner(ok, "ok")
    body = f"""
    <h1>Change password</h1>
    <p class='muted'>Signed in as {user['email']}.</p>
    {banner}
    <form method='post' action='/auth/password'>
      <label>Current password
        <input type='password' name='current' required/>
      </label>
      <label>New password
        <input type='password' name='new' required minlength='12'/>
      </label>
      <button>Update password</button>
    </form>
    <div class='row-links'>
      <a href='/'>Back to dashboard</a>
      <a href='/auth/logout'>Sign out</a>
    </div>
    """
    return _page("Change password", body)


@router.post("/password", include_in_schema=False)
async def change_password_submit(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    ip = client_ip(request)
    try:
        service.change_password(
            user_id=int(user["id"]),
            current_password=current,
            new_password=new,
            ip=ip,
        )
    except AuthError as exc:
        return RedirectResponse(
            url=f"/auth/password?error={str(exc).replace(' ', '+')}",
            status_code=303,
        )
    return RedirectResponse(url="/auth/password?ok=Password+updated", status_code=303)


# ── Audit log ─────────────────────────────────────────────────────────
@router.get("/audit", response_class=HTMLResponse, include_in_schema=False)
async def audit_page(request: Request) -> str:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login?next=/auth/audit", status_code=303)
    events = storage.recent_audit(limit=200)
    rows = "".join(
        f"<tr><td>{e['ts'][:19].replace('T', ' ')}</td>"
        f"<td>{e['action']}</td>"
        f"<td>{e.get('email') or '&mdash;'}</td>"
        f"<td>{e.get('ip') or '&mdash;'}</td>"
        f"<td>{(e.get('detail') or '')[:80]}</td></tr>"
        for e in events
    )
    body = f"""
    <h1 style='margin-bottom:0.75rem;'>Audit log</h1>
    <p class='muted'>Last 200 security-relevant events.</p>
    <table style='width:100%;border-collapse:collapse;font-size:0.85rem;'>
      <thead>
        <tr style='color:#8a93b2;text-align:left;'>
          <th style='padding:0.4rem 0.5rem;border-bottom:1px solid #1d2540;'>Time</th>
          <th style='padding:0.4rem 0.5rem;border-bottom:1px solid #1d2540;'>Action</th>
          <th style='padding:0.4rem 0.5rem;border-bottom:1px solid #1d2540;'>User</th>
          <th style='padding:0.4rem 0.5rem;border-bottom:1px solid #1d2540;'>IP</th>
          <th style='padding:0.4rem 0.5rem;border-bottom:1px solid #1d2540;'>Detail</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <div class='row-links'>
      <a href='/'>Back to dashboard</a>
    </div>
    """
    # Audit page wants a wider card; reuse _page but override max width.
    html = _page("Audit", body).replace("max-width: 420px;", "max-width: 960px;")
    return html
