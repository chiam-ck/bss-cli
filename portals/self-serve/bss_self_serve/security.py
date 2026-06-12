"""Route gating helpers — FastAPI dependencies + public-route allowlist.

The ``PortalSessionMiddleware`` already attached
``request.state.session`` / ``.identity`` / ``.customer_id`` (or None
on miss). These dependencies *enforce* the gates: each one raises a
``RedirectToLogin`` exception, caught by an exception handler and
turned into a 303 redirect.

Doctrine (V0_8_0.md §5):

* ``/welcome``, ``/plans``, ``/auth/*`` are the only paths reachable
  without a session. Adding a public route requires an entry in
  ``PUBLIC_PATH_PREFIXES`` + a test.
* ``request.state.customer_id`` is the only acceptable source of the
  customer id post-login. Routes never accept ``customer_id`` from
  query string or form input.
* Step-up tokens are forwarded as ``X-BSS-StepUp-Token`` header or
  ``step_up_token`` form field; ``requires_step_up`` consumes them
  via ``bss_portal_auth.consume_step_up_token``.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Final
from urllib.parse import quote, urlencode, urlparse

from bss_portal_auth import (
    IdentityView,
    SessionView,
    consume_step_up_token,
    stash_pending_action,
)
from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

# ── Public-route allowlist ───────────────────────────────────────────────


PUBLIC_EXACT_PATHS: Final[frozenset[str]] = frozenset({
    "/health",
    "/health/ready",
    "/health/live",
    "/welcome",
    "/plans",
    # v0.12 PR20 — legal pages public by design.
    "/terms",
    "/privacy",
    # v0.15 — Didit hosted UI redirects the customer's verifying device
    # back to this URL after liveness completes. In the cross-device flow
    # that device is a phone with no portal session cookie. The page is
    # a static "verification complete, return to your computer"
    # confirmation; it performs no BSS write. The desktop's poll endpoint
    # is what actually advances the signup.
    "/signup/step/kyc/callback",
    # Convenience: `/` redirects to /welcome for anonymous visitors,
    # which the dashboard route does itself by raising RedirectToLogin.
})

PUBLIC_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "/auth/",
    "/static/",
    "/portal-ui/static/",
    "/plans/",  # /plans/{id} detail page if/when added
    # v0.14 — inbound provider webhooks. Auth is provider signature
    # (Svix/HMAC) inside the route handler, not session cookies.
    "/webhooks/",
)


# ── Step-up action catalogue (V0_10_0.md Track 10.2) ─────────────────────

# Greppable source of truth for the sensitive-action list. Every label
# names a single post-login self-serve write that must be gated by
# step-up auth. A test asserts (a) every label appears in at least one
# ``requires_step_up(...)`` call site under ``routes/`` and (b) every
# call site uses a label from this set. Adding a new sensitive route
# requires extending this set first.
SENSITIVE_ACTION_LABELS: Final[frozenset[str]] = frozenset({
    "vas_purchase",
    "payment_method_add",
    "payment_method_remove",
    "payment_method_set_default",
    "subscription_terminate",
    "email_change",
    "phone_update",  # weak gate; still required for any contact-medium write
    "address_update",
    "name_update",   # display name (Party.individual.given_name/family_name)
    "plan_change_schedule",
    "plan_change_cancel",
})


# v0.11 — signup chain audit labels (V0_11_0.md Track 2.6).
#
# Signup writes happen BEFORE the customer has a linked-customer session,
# so ``requires_step_up`` does not apply. The labels are still used as
# the ``action`` field on ``portal_action`` audit rows so a forensic
# query can replay a signup attempt step-by-step. We keep them in a
# separate set from ``SENSITIVE_ACTION_LABELS`` so the step-up cross-
# check test (``test_step_up_label_cross_check``) doesn't conflate
# "every label in this set must appear under a ``requires_step_up``
# call site" with "signup audit labels exist but never gate via step-up".
SIGNUP_ACTION_LABELS: Final[frozenset[str]] = frozenset({
    "signup_create_customer",
    "signup_attest_kyc",
    "signup_add_card",
    "signup_create_order",
})


def is_public_path(path: str) -> bool:
    """True iff ``path`` is reachable without a session.

    Doctrine: this list is the only allowlist. The route-gating
    dependencies live separately because every non-public route
    explicitly opts in via ``Depends(requires_session)``.
    """
    if path in PUBLIC_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


def safe_next_path(raw: str | None, *, default: str = "/") -> str:
    """Validate a ``?next=`` redirect target against an internal-only allowlist.

    Open-redirect defence: never trust ``next`` from a query string at face
    value. Only accept absolute internal paths (start with ``/``, no ``//``,
    no scheme, no fragment-injected host). Reject anything that doesn't
    decompose into a path our portal owns.
    """
    if not raw:
        return default
    candidate = raw.strip()
    if not candidate.startswith("/"):
        return default
    if candidate.startswith("//") or candidate.startswith("/\\"):
        return default
    # Forbid embedded hosts/schemes/CRLF.
    forbidden = ("://", "\r", "\n", "\\")
    if any(token in candidate for token in forbidden):
        return default
    return candidate


# ── Redirect exception + handler ─────────────────────────────────────────


class RedirectToLogin(HTTPException):
    """Raised by a gating dep when no/invalid session is present."""

    def __init__(self, *, next_path: str | None = None) -> None:
        loc = "/auth/login"
        if next_path:
            loc = f"/auth/login?{urlencode({'next': next_path})}"
        super().__init__(status_code=status.HTTP_303_SEE_OTHER, detail=loc)
        self.location = loc


class StepUpRequired(HTTPException):
    """Raised by ``requires_step_up`` when no valid grant is present."""

    def __init__(self, *, action_label: str, next_path: str) -> None:
        params = urlencode({"action": action_label, "next": next_path})
        loc = f"/auth/step-up?{params}"
        super().__init__(status_code=status.HTTP_303_SEE_OTHER, detail=loc)
        self.location = loc


def install_redirect_handlers(app) -> None:  # type: ignore[no-untyped-def]
    """Install exception handlers that turn the redirects into 303 responses."""

    @app.exception_handler(RedirectToLogin)
    async def _login(_req: Request, exc: RedirectToLogin) -> RedirectResponse:
        return RedirectResponse(url=exc.location, status_code=303)

    @app.exception_handler(StepUpRequired)
    async def _stepup(_req: Request, exc: StepUpRequired) -> RedirectResponse:
        return RedirectResponse(url=exc.location, status_code=303)


# ── Dependencies ─────────────────────────────────────────────────────────


def _next_for(request: Request) -> str:
    """Build a `next=` parameter that the post-login flow can bounce to.

    Internal-paths only — never echo the absolute URL or query strings
    that contain user input. The /auth/login route validates this
    against the same is_public / known-prefix list before honouring it.

    For POST requests we prefer the Referer's path: a step-up bounce
    arrives back via 303 (forced GET), so landing on the POST URL
    yields 405 unless the route also has a GET handler. The form
    page in the Referer is GET-able and re-renders the form so the
    customer can re-submit with the step-up cookie carrying the grant.
    Falls back to the current URL when Referer is missing or external.
    """
    if request.method == "POST":
        referer_path = _safe_referer_path(request)
        if referer_path is not None:
            return quote(referer_path, safe="/?=&")
    path = request.url.path
    qs = request.url.query
    return quote(f"{path}?{qs}", safe="/?=&") if qs else quote(path, safe="/")


def _safe_referer_path(request: Request) -> str | None:
    """Extract a safe internal path+query from the Referer header.

    Returns None if the header is missing, not parseable, points at
    another origin, or doesn't survive ``safe_next_path`` validation.
    """
    raw = request.headers.get("referer")
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    # Reject if Referer carries a different host. Same-origin = either
    # no netloc (relative) or netloc matching the request's Host header.
    if parsed.netloc:
        host = request.headers.get("host", "")
        if parsed.netloc != host:
            return None
    if not parsed.path:
        return None
    candidate = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    if safe_next_path(candidate, default="") != candidate:
        return None
    return candidate


def requires_session(request: Request) -> SessionView:
    """Gate: must have a live session. Raises RedirectToLogin if not."""
    sess = getattr(request.state, "session", None)
    if sess is None:
        raise RedirectToLogin(next_path=_next_for(request))
    return sess


def requires_verified_email(request: Request) -> IdentityView:
    """Gate: session present AND identity.email_verified_at set.

    The login flow stamps `email_verified_at` on first successful
    verify, so in practice this is equivalent to ``requires_session``
    today. Future flows (OAuth handoff, admin-impersonated session)
    may produce sessions with unverified identities; this dep keeps
    that distinction explicit.
    """
    requires_session(request)  # raises RedirectToLogin when absent
    identity: IdentityView | None = getattr(request.state, "identity", None)
    if identity is None or identity.email_verified_at is None:
        raise RedirectToLogin(next_path=_next_for(request))
    return identity


def requires_linked_customer(request: Request) -> str:
    """Gate: session + verified email + identity.customer_id is set.

    Returns the customer_id. Used by every post-first-order page.
    Verified-but-unlinked identities (created an account but bailed
    before completing signup) hit ``/`` and see the empty dashboard;
    they don't hit any route protected by this dep.
    """
    requires_verified_email(request)
    customer_id: str | None = getattr(request.state, "customer_id", None)
    if not customer_id:
        # Bounce to the dashboard, which is empty-state for unlinked.
        # We don't redirect to /plans because that would conflate
        # "unlinked identity wants to start signup" with "linked
        # customer asked for a feature that needs the customer_id".
        raise RedirectToLogin(next_path="/")
    return customer_id


def requires_step_up(action_label: str) -> Callable[[Request], Awaitable[None]]:
    """Dependency factory — consumes a one-shot step-up grant.

    Reads ``X-BSS-StepUp-Token`` header first, then ``step_up_token``
    form field (consumed during request parsing if applicable). On
    success: marks the grant consumed and returns. On failure: raises
    StepUpRequired so the route bounces to ``/auth/step-up``.

    The ``action_label`` MUST be a member of ``SENSITIVE_ACTION_LABELS``
    — the dependency raises at import time (well, at first invocation)
    if a typo or unregistered label slips in. This keeps the catalogue
    greppable: there is exactly one place to add a new sensitive
    action, and it's not deep inside a route file.
    """
    if action_label not in SENSITIVE_ACTION_LABELS:
        raise ValueError(
            f"requires_step_up: action_label {action_label!r} is not in "
            "SENSITIVE_ACTION_LABELS. Add it to the catalogue in "
            "bss_self_serve.security before using it on a route."
        )

    async def _dep(request: Request) -> None:
        sess = requires_session(request)

        # Order: explicit form/header beats the convenience cookie. The
        # cookie path is what the /auth/step-up redirect uses to thread
        # a one-shot grant through a GET-bound bounce; explicit forms /
        # headers let API-style callers carry the grant themselves.
        token = request.headers.get("x-bss-stepup-token") or request.headers.get(
            "x-bss-step-up-token"
        )
        form_payload: dict[str, str] | None = None
        if request.method == "POST":
            try:
                form = await request.form()
                if token is None:
                    token = form.get("step_up_token")  # type: ignore[assignment]
                # Capture the rest for stash-on-bounce. We only stash if
                # we ultimately raise StepUpRequired, but the form is
                # cached by Starlette so reading here is free.
                form_payload = {
                    k: v for k, v in form.multi_items()
                    if isinstance(v, str)
                }
            except Exception:
                form_payload = None
        if token is None:
            # Read the grant cookie via the Starlette wrapper (no raw
            # cookie-header parsing in route code). This is part of the
            # auth flow proper, not a route-side cookie rummage.
            token = request.cookies.get("bss_portal_step_up")

        factory = getattr(request.app.state, "db_session_factory", None)
        if factory is None:
            raise RuntimeError(
                "app.state.db_session_factory not set — wire it in lifespan"
            )

        if not token:
            await _stash_for_replay(
                factory,
                session_id=sess.id,
                action_label=action_label,
                request=request,
                payload=form_payload,
            )
            raise StepUpRequired(
                action_label=action_label, next_path=_next_for(request)
            )

        async with factory() as db:
            ok = await consume_step_up_token(
                db,
                session_id=sess.id,
                token=str(token),
                action_label=action_label,
            )
            await db.commit()

        if not ok:
            await _stash_for_replay(
                factory,
                session_id=sess.id,
                action_label=action_label,
                request=request,
                payload=form_payload,
            )
            raise StepUpRequired(
                action_label=action_label, next_path=_next_for(request)
            )

    return _dep


async def _stash_for_replay(
    factory: Callable[[], object],
    *,
    session_id: str,
    action_label: str,
    request: Request,
    payload: dict[str, str] | None,
) -> None:
    """Stash the original POST so /auth/step-up can replay it.

    No-op for non-POST requests. Empty-body POSTs (e.g. the
    "Continue to Stripe" button or the "Remove card" form, where
    the URL path carries the action and the body is empty) ARE
    stashed — the replay just POSTs to the URL with no fields,
    which is exactly what the original click did.

    Pre-v0.16 the gate also excluded empty payloads, which broke
    the replay for any URL-path-driven action (POST /payment-methods/add,
    POST /payment-methods/<id>/remove, POST /payment-methods/<id>/set-default).
    Customers had to click the action button TWICE — once to trigger
    the OTP challenge, then again after OTP verification.

    Failures are swallowed: the stash is a UX optimisation, not a
    correctness gate, and the StepUpRequired bounce must still happen
    even if the stash insert fails.
    """
    if request.method != "POST":
        return
    # Preserve the query string. Routes like ``POST /top-up`` use
    # ``?subscription=SUB-X`` to identify the target subscription;
    # stripping it makes the post-step-up replay 422 because the
    # required Query param is missing. ``safe_next_path`` validates
    # the same path+query shape so we trust the inbound URL here.
    target_url = request.url.path
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"
    try:
        async with factory() as db:  # type: ignore[misc]
            await stash_pending_action(
                db,
                session_id=session_id,
                action_label=action_label,
                target_url=target_url,
                payload=payload or {},
            )
            await db.commit()  # type: ignore[attr-defined]
    except Exception:
        # Stash is best-effort. Swallow so StepUpRequired still fires.
        return
