"""``/auth/*`` routes — login, check-email, verify, logout, step-up.

V0_8_0.md §3.1. Every route here is in the public allowlist
(``/auth/`` prefix in ``security.PUBLIC_PATH_PREFIXES``); session
gating doesn't apply because these routes ARE the gate.

DB writes go through ``bss_portal_auth.*`` only — no direct identity
or session-row creation. Email delivery goes through
``request.app.state.email_adapter`` (selected by env at startup).

Doctrine reminders:

* No passwords. Magic-link or OTP only.
* The customer-facing failure copy is intentionally generic. The
  internal ``LoginFailed.reason`` is used for audit / structured
  logs, not the rendered template.
* Cookie writes go through ``build_session_cookie`` — never raw
  ``response.set_cookie`` calls. Rotation on success defangs
  session fixation.
"""

from __future__ import annotations

from urllib.parse import urlencode

import structlog
from bss_portal_auth import (
    LoginFailed,
    SessionView,
    StepUpFailed,
    StepUpToken,
    consume_pending_action,
    revoke_session,
    start_email_login,
    start_step_up,
    verify_email_login,
    verify_step_up,
)
from bss_portal_auth.types import RateLimitExceeded
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..middleware.session import (
    build_clear_cookie,
    build_session_cookie,
)
from ..security import requires_session, safe_next_path
from ..templating import templates

router = APIRouter(prefix="/auth")

log = structlog.get_logger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _mask_email(email: str) -> str:
    """Render ``ada@example.sg`` as ``a***@example.sg`` for the interstitial.

    Keeps the recipient identifiable to the right person without echoing
    the full address back over the wire on a public-ish page.
    """
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if not local:
        return email
    head = local[0]
    return f"{head}{'*' * max(len(local) - 1, 1)}@{domain}"


# ── /auth/login (email entry) ────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    next: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the email-entry form. ``?next=`` carried through to POST."""
    next_path = safe_next_path(next)
    return templates.TemplateResponse(
        request,
        "auth_login.html",
        {"next_path": next_path, "error": None, "email": ""},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    next: str = Form(default="/"),
) -> Response:
    """Issue OTP + magic-link for ``email``; redirect to /auth/check-email."""
    next_path = safe_next_path(next)
    email = email.strip().lower()

    # Cheap shape validation — keep policy here, not in bss-portal-auth which
    # treats email as opaque.
    if "@" not in email or len(email) < 3 or " " in email:
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {
                "next_path": next_path,
                "error": "That doesn't look like an email address.",
                "email": email,
            },
            status_code=400,
        )

    factory = request.app.state.db_session_factory
    adapter = request.app.state.email_adapter
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")

    async with factory() as db:
        try:
            await start_email_login(
                db,
                email=email,
                ip=ip,
                user_agent=ua,
                email_adapter=adapter,
            )
            await db.commit()
        except RateLimitExceeded:
            await db.rollback()
            log.info("portal_auth.login.rate_limited", scope="login_start")
            return templates.TemplateResponse(
                request,
                "auth_login.html",
                {
                    "next_path": next_path,
                    "error": "Too many attempts. Try again in a few minutes.",
                    "email": email,
                },
                status_code=429,
            )

    # URL-encode the query string. f-string interpolation would put a
    # raw '+' in the URL, which the next route's Query(...) decodes as
    # a space (urlencoded-form convention) — breaks Gmail '+addressing'.
    qs = urlencode({"email": email, "next": next_path})
    return RedirectResponse(
        url=f"/auth/check-email?{qs}",
        status_code=303,
    )


# ── /auth/check-email (interstitial + OTP entry) ─────────────────────────


@router.get("/check-email", response_class=HTMLResponse)
async def check_email_form(
    request: Request,
    email: str = Query(...),
    next: str | None = Query(default=None),
) -> HTMLResponse:
    next_path = safe_next_path(next)
    return templates.TemplateResponse(
        request,
        "auth_check_email.html",
        {
            "email": email,
            "email_masked": _mask_email(email),
            "next_path": next_path,
            "error": None,
        },
    )


@router.post("/check-email")
async def check_email_submit(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    next: str = Form(default="/"),
) -> Response:
    """Verify the OTP. On success: set cookie, redirect to next."""
    next_path = safe_next_path(next)
    email = email.strip().lower()
    code = code.strip()

    factory = request.app.state.db_session_factory
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")

    async with factory() as db:
        try:
            result = await verify_email_login(
                db, email=email, code=code, ip=ip, user_agent=ua
            )
            await db.commit()
        except RateLimitExceeded:
            await db.rollback()
            return templates.TemplateResponse(
                request,
                "auth_check_email.html",
                {
                    "email": email,
                    "email_masked": _mask_email(email),
                    "next_path": next_path,
                    "error": "Too many attempts. Try again in a few minutes.",
                },
                status_code=429,
            )

    if isinstance(result, LoginFailed):
        # Customer-facing copy is generic; reason ('wrong_code'/'expired'/
        # 'no_active_token'/'no_such_identity') is in the audit log.
        log.info("portal_auth.login.failed", reason=result.reason)
        return templates.TemplateResponse(
            request,
            "auth_check_email.html",
            {
                "email": email,
                "email_masked": _mask_email(email),
                "next_path": next_path,
                "error": "Incorrect or expired code. Try again or request a new one.",
            },
            status_code=400,
        )

    return _redirect_with_session_cookie(next_path, result)


# ── /auth/verify (magic-link landing) ────────────────────────────────────


@router.get("/verify")
async def verify_magic_link(
    request: Request,
    email: str = Query(...),
    token: str = Query(...),
    next: str | None = Query(default=None),
) -> Response:
    """Magic-link click-through. Same verify path as the OTP form."""
    next_path = safe_next_path(next)
    factory = request.app.state.db_session_factory
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")

    async with factory() as db:
        try:
            result = await verify_email_login(
                db, email=email.strip().lower(), code=token, ip=ip, user_agent=ua
            )
            await db.commit()
        except RateLimitExceeded:
            await db.rollback()
            return RedirectResponse(
                url=f"/auth/login?next={next_path}", status_code=303
            )

    if isinstance(result, LoginFailed):
        # Same event name as the OTP path; the audit row in
        # portal_auth.login_attempt distinguishes which kind via stage.
        log.info("portal_auth.verify.failed", reason=result.reason, via="link")
        return RedirectResponse(
            url=f"/auth/login?next={next_path}", status_code=303
        )

    return _redirect_with_session_cookie(next_path, result)


# ── /auth/logout ─────────────────────────────────────────────────────────


@router.post("/logout")
async def logout(request: Request) -> Response:
    """Revoke the current session, clear the cookie, bounce to /welcome."""
    sess: SessionView | None = getattr(request.state, "session", None)
    if sess is not None:
        factory = request.app.state.db_session_factory
        async with factory() as db:
            await revoke_session(db, sess.id)
            await db.commit()

    response = RedirectResponse(url="/welcome", status_code=303)
    # Replace whatever the middleware may have written this turn.
    response.headers["set-cookie"] = build_clear_cookie()
    return response


# ── /auth/step-up ────────────────────────────────────────────────────────


@router.get("/step-up", response_class=HTMLResponse)
async def step_up_form(
    request: Request,
    action: str = Query(...),
    next: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the step-up entry form. Visitor must already be logged in;
    session-required is enforced by the dep below at /start."""
    next_path = safe_next_path(next)
    return templates.TemplateResponse(
        request,
        "auth_step_up.html",
        {
            "action_label": action,
            "next_path": next_path,
            "issued": False,
            "error": None,
        },
    )


@router.post("/step-up/start")
async def step_up_start(
    request: Request,
    action: str = Form(...),
    next: str = Form(default="/"),
) -> Response:
    """Mint + send a step-up OTP, re-render the form with issued=True."""
    sess = requires_session(request)
    next_path = safe_next_path(next)

    factory = request.app.state.db_session_factory
    adapter = request.app.state.email_adapter

    async with factory() as db:
        try:
            await start_step_up(
                db,
                session_id=sess.id,
                action_label=action,
                ip=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                email_adapter=adapter,
            )
            await db.commit()
        except RateLimitExceeded:
            await db.rollback()
            return templates.TemplateResponse(
                request,
                "auth_step_up.html",
                {
                    "action_label": action,
                    "next_path": next_path,
                    "issued": False,
                    "error": "Too many attempts. Try again later.",
                },
                status_code=429,
            )

    return templates.TemplateResponse(
        request,
        "auth_step_up.html",
        {
            "action_label": action,
            "next_path": next_path,
            "issued": True,
            "error": None,
        },
    )


@router.post("/step-up")
async def step_up_verify(
    request: Request,
    code: str = Form(...),
    action: str = Form(...),
    next: str = Form(default="/"),
) -> Response:
    """Verify the step-up OTP. On success, set a short-lived
    HttpOnly cookie carrying the one-shot grant, then redirect to
    ``next``. The downstream sensitive route is gated by
    ``Depends(requires_step_up(...))`` which reads the grant from
    (in order) ``X-BSS-StepUp-Token`` header, ``step_up_token``
    form field, or the ``bss_portal_step_up`` cookie. The cookie
    path is what makes the GET-bound bounce-to-target work without
    asking the target page to know about step-up at all.
    """
    sess = requires_session(request)
    next_path = safe_next_path(next)

    factory = request.app.state.db_session_factory
    async with factory() as db:
        result = await verify_step_up(
            db, session_id=sess.id, code=code.strip(), action_label=action
        )
        await db.commit()

    if isinstance(result, StepUpFailed):
        log.info("portal_auth.step_up.failed", reason=result.reason, action=action)
        return templates.TemplateResponse(
            request,
            "auth_step_up.html",
            {
                "action_label": action,
                "next_path": next_path,
                "issued": True,
                "error": "Incorrect or expired code. Request a new one above.",
            },
            status_code=400,
        )

    assert isinstance(result, StepUpToken)

    # If the original POST that triggered this step-up stashed its
    # form body, replay it so the customer doesn't re-type. The
    # browser carries the cookie we're about to set on the replay
    # POST, so the second-pass requires_step_up consumes the grant.
    pending = None
    async with factory() as db:
        pending = await consume_pending_action(
            db, session_id=sess.id, action_label=action
        )
        await db.commit()

    if pending is not None:
        response = templates.TemplateResponse(
            request,
            "auth_step_up_replay.html",
            {
                "target_url": pending.target_url,
                "payload": pending.payload,
                "action_label": action,
            },
        )
    else:
        response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        key="bss_portal_step_up",
        value=result.token,
        max_age=60,
        httponly=True,
        samesite="lax",
        path="/",
        secure=_secure_cookie_default(),
    )
    return response


# ── helpers (private) ────────────────────────────────────────────────────


def _redirect_with_session_cookie(next_path: str, sess: SessionView) -> Response:
    """Build a 303 to ``next_path`` with the new session cookie set.

    Replaces any Set-Cookie the middleware may have queued for this
    response (the middleware only rotates EXISTING sessions; a freshly-
    minted one needs an explicit set).
    """
    response = RedirectResponse(url=next_path, status_code=303)
    response.headers["set-cookie"] = build_session_cookie(sess.id)
    return response


def _secure_cookie_default() -> bool:
    from bss_portal_auth import Settings as PortalAuthSettings

    return not PortalAuthSettings().BSS_PORTAL_DEV_INSECURE_COOKIE
