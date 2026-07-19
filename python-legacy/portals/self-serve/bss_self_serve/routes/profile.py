"""``/profile/contact`` — contact medium update + email-change two-step (v0.10 PR 8).

V0_10_0.md Track 7 + Track 10. Three writeable contact-medium types,
two flows:

* **Phone + address** go through one direct
  ``customer.update_contact_medium`` call per submission, gated by
  step-up (``phone_update`` / ``address_update``). One route → one
  BSS write.
* **Email** is identity-bound; changes require re-verification of
  the new email. The flow is two-step:
    1. POST ``/profile/contact/email/change`` (step-up=``email_change``)
       — uniqueness-checks the new email, persists a pending row,
       sends a 6-digit OTP to the *new* address. The portal stays
       on the old email until verification.
    2. POST ``/profile/contact/email/verify`` — atomically commits
       the change across ``crm.contact_medium`` AND
       ``portal_auth.identity.email`` AND the pending row, in a
       single Postgres transaction. The cross-schema atomic write
       lives in ``bss_portal_auth.email_change`` (DECISIONS
       2026-04-27 v0.10 PR 8 documents the doctrine bend).

Doctrine reminders:

* ``customer_id`` from ``request.state.customer_id``;
  ``cm_id`` from form is checked against the customer's active
  mediums before any write. Cross-medium attempts → 403.
* The email PATH (start, verify, cancel) bypasses the v0.10
  ``customer.update_contact_medium`` direct route — that's enforced
  on the CRM service side too (PolicyViolation:
  ``customer.contact_medium.email_must_use_change_flow``).
* Atomicity test for the verify path is non-negotiable. See
  ``test_routes_profile.py::test_email_change_verify_rolls_back_on_partial_failure``.
"""

from __future__ import annotations

from typing import Any

import structlog
from bss_clients import PolicyViolationFromServer
from bss_portal_auth import (
    EmailChangeApplied,
    EmailChangeFailed,
    EmailChangeStarted,
    IdentityView,
    cancel_pending_email_change,
    record_portal_action,
    start_email_change,
    verify_email_change,
)
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..clients import get_clients
from ..error_messages import is_known, render
from ..security import (
    requires_linked_customer,
    requires_session,
    requires_step_up,
)
from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


_OWNERSHIP_RULE = "policy.customer.contact_medium.unknown"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _client_ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _identity(request: Request) -> IdentityView:
    """Pull the verified identity off request.state. Always present
    after ``requires_linked_customer`` resolves."""
    identity: IdentityView | None = getattr(request.state, "identity", None)
    if identity is None:  # pragma: no cover — gating dep already enforced
        raise RuntimeError("requires_linked_customer didn't populate identity")
    return identity


async def _list_active_mediums(
    clients: Any, customer_id: str
) -> list[dict[str, Any]]:
    return await clients.crm.list_contact_mediums(customer_id)


async def _get_individual(
    clients: Any, customer_id: str
) -> dict[str, Any]:
    """Pull the customer's display name off the TMF629 response.

    v0.10 — name lives on ``crm.individual``, not on
    ``contact_medium``. The /profile/contact page renders both
    surfaces in one view; this helper keeps the route handler
    free of TMF-shape extraction logic.
    """
    cust = await clients.crm.get_customer(customer_id)
    ind = cust.get("individual") or {}
    return {
        "given_name": ind.get("givenName") or "",
        "family_name": ind.get("familyName") or "",
    }


async def _check_medium_owned(
    clients: Any, cm_id: str, customer_id: str
) -> dict[str, Any] | None:
    mediums = await _list_active_mediums(clients, customer_id)
    for cm in mediums:
        if cm.get("id") == cm_id:
            return cm
    return None


# ── GET /profile/contact — list ─────────────────────────────────────────


@router.get("/profile/contact", response_class=HTMLResponse)
async def contact_view(
    request: Request,
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    clients = get_clients()
    mediums = await _list_active_mediums(clients, customer_id)
    individual = await _get_individual(clients, customer_id)

    factory = request.app.state.db_session_factory
    pending = None
    if factory is not None:
        from bss_models import EmailChangePending
        from sqlalchemy import select

        identity = _identity(request)
        async with factory() as db:
            row = (
                await db.execute(
                    select(EmailChangePending).where(
                        EmailChangePending.identity_id == identity.id,
                        EmailChangePending.status == "pending",
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                pending = {
                    "new_email": row.new_email,
                    "expires_at": row.expires_at.isoformat(),
                }

    return templates.TemplateResponse(
        request,
        "profile_contact.html",
        {
            "mediums": mediums,
            "individual": individual,
            "pending_email_change": pending,
            "error": None,
            "flash": None,
        },
    )


# ── POST /profile/contact/name/update ────────────────────────────────────


@router.post("/profile/contact/name/update")
async def name_update(
    request: Request,
    given_name: str = Form(..., min_length=1, max_length=100),
    family_name: str = Form(..., min_length=1, max_length=100),
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("name_update")),
) -> Response:
    """Update the customer's display name (Party.individual.given_name / family_name).

    Captured at signup time and persisted on the customer record;
    the /profile/contact page surfaces it for view + edit. v0.11's
    signup-direct migration will pre-fill these on the second-line
    signup form so the customer doesn't re-enter them.
    """
    clients = get_clients()
    factory = request.app.state.db_session_factory
    iid = _identity(request).id

    try:
        await clients.crm.update_individual(
            customer_id,
            given_name=given_name.strip(),
            family_name=family_name.strip(),
        )
    except PolicyViolationFromServer as exc:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="name_update",
                route="/profile/contact/name/update",
                method="POST",
                success=False,
                error_rule=exc.rule,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        if not is_known(exc.rule):
            log.info("portal.profile.unknown_policy_rule", rule=exc.rule, action="name_update")
        mediums = await _list_active_mediums(clients, customer_id)
        individual = await _get_individual(clients, customer_id)
        return templates.TemplateResponse(
            request,
            "profile_contact.html",
            {
                "mediums": mediums,
                "individual": individual,
                "pending_email_change": None,
                "error": render(exc.rule),
                "flash": None,
            },
            status_code=422,
        )

    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=iid,
            action="name_update",
            route="/profile/contact/name/update",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return RedirectResponse(
        url="/profile/contact?flash=name_update", status_code=303
    )


# ── POST /profile/contact/phone/update ───────────────────────────────────


@router.post("/profile/contact/phone/update")
async def phone_update(
    request: Request,
    cm_id: str = Form(..., min_length=1),
    value: str = Form(..., min_length=1),
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("phone_update")),
) -> Response:
    return await _direct_medium_update(
        request,
        customer_id=customer_id,
        cm_id=cm_id,
        value=value.strip(),
        action="phone_update",
        expected_type="mobile",
    )


# ── POST /profile/contact/address/update ─────────────────────────────────


@router.post("/profile/contact/address/update")
async def address_update(
    request: Request,
    cm_id: str = Form(..., min_length=1),
    value: str = Form(..., min_length=1),
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("address_update")),
) -> Response:
    return await _direct_medium_update(
        request,
        customer_id=customer_id,
        cm_id=cm_id,
        value=value.strip(),
        action="address_update",
        expected_type="postal",
    )


async def _direct_medium_update(
    request: Request,
    *,
    customer_id: str,
    cm_id: str,
    value: str,
    action: str,
    expected_type: str,
) -> Response:
    """Shared body for phone + address updates.

    Both flow through ``customer.update_contact_medium`` server-side;
    the only differences are the step-up label, the audit ``action``,
    and the ``expected_type`` (used to refuse a phone-form submission
    that targets an email row, etc.).
    """
    clients = get_clients()
    factory = request.app.state.db_session_factory
    iid = _identity(request).id

    owned = await _check_medium_owned(clients, cm_id, customer_id)
    if owned is None or owned.get("mediumType") != expected_type:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action=action,
                route=request.url.path,
                method="POST",
                success=False,
                error_rule=_OWNERSHIP_RULE,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        return templates.TemplateResponse(
            request,
            "profile_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    try:
        await clients.crm.update_contact_medium(customer_id, cm_id, value=value)
    except PolicyViolationFromServer as exc:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action=action,
                route=request.url.path,
                method="POST",
                success=False,
                error_rule=exc.rule,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        if not is_known(exc.rule):
            log.info("portal.profile.unknown_policy_rule", rule=exc.rule, action=action)
        mediums = await _list_active_mediums(clients, customer_id)
        return templates.TemplateResponse(
            request,
            "profile_contact.html",
            {
                "mediums": mediums,
                "pending_email_change": None,
                "error": render(exc.rule),
                "flash": None,
            },
            status_code=422,
        )

    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=iid,
            action=action,
            route=request.url.path,
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return RedirectResponse(
        url=f"/profile/contact?flash={action}", status_code=303
    )


# ── POST /profile/contact/email/change — start ───────────────────────────


@router.post("/profile/contact/email/change")
async def email_change_start(
    request: Request,
    new_email: str = Form(..., min_length=3),
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("email_change")),
) -> Response:
    """Start the email-change flow.

    Atomicity strategy: the cross-schema commit happens in the *verify*
    step. The *start* step writes only to ``portal_auth.email_change_pending``
    (single schema, single transaction) and triggers the email send
    side-effect. If anything goes wrong here, no CRM row was touched.
    """
    factory = request.app.state.db_session_factory
    adapter = request.app.state.email_adapter
    identity = _identity(request)

    async with factory() as db:
        result = await start_email_change(
            db,
            identity_id=identity.id,
            new_email=new_email,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
            email_adapter=adapter,
        )
        if isinstance(result, EmailChangeFailed):
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=identity.id,
                action="email_change",
                route="/profile/contact/email/change",
                method="POST",
                success=False,
                error_rule=f"policy.customer.contact_medium.{result.reason}",
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
            mediums = await get_clients().crm.list_contact_mediums(customer_id)
            return templates.TemplateResponse(
                request,
                "profile_contact.html",
                {
                    "mediums": mediums,
                    "pending_email_change": None,
                    "error": render(
                        f"policy.customer.contact_medium.{result.reason}"
                    ),
                    "flash": None,
                },
                status_code=422,
            )

        # success — record + commit
        assert isinstance(result, EmailChangeStarted)
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=identity.id,
            action="email_change",
            route="/profile/contact/email/change",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return templates.TemplateResponse(
        request,
        "profile_email_pending.html",
        {"new_email": result.new_email},
        status_code=200,
    )


# ── GET /profile/contact/email/verify — form ─────────────────────────────


@router.get("/profile/contact/email/verify", response_class=HTMLResponse)
async def email_change_verify_form(
    request: Request,
    _customer_id: str = Depends(requires_linked_customer),
) -> Response:
    return templates.TemplateResponse(
        request,
        "profile_email_verify.html",
        {"error": None},
    )


# ── POST /profile/contact/email/verify — atomic commit ───────────────────


@router.post("/profile/contact/email/verify")
async def email_change_verify_submit(
    request: Request,
    code: str = Form(..., min_length=4),
    customer_id: str = Depends(requires_linked_customer),
    # Note: NO requires_step_up here — the OTP itself is the step-up.
    # Requiring a separate session-level step-up grant on top would be
    # both redundant (OTP proves access to the new mailbox) and a
    # UX trap (customer can't request a step-up for a label that's
    # part of an in-flight flow).
    _session=Depends(requires_session),
) -> Response:
    """Verify the OTP and commit the cross-schema email change atomically.

    Single ``AsyncSession`` transaction — both ``crm.contact_medium``
    and ``portal_auth.identity.email`` updates land in the same
    Postgres transaction; ``await db.commit()`` is the only commit
    point. If the verify_email_change function fails partway, the
    session is rolled back and no row anywhere has been touched.
    """
    factory = request.app.state.db_session_factory
    identity = _identity(request)

    async with factory() as db:
        try:
            result = await verify_email_change(
                db, identity_id=identity.id, code=code
            )
        except Exception:
            # The cross-schema write blew up halfway. Rollback is
            # automatic on context-manager exit; we just need to make
            # sure NO commit happens.
            await db.rollback()
            await _audit_verify_failure(
                request, customer_id, identity.id, "policy.customer.contact_medium.unknown"
            )
            return templates.TemplateResponse(
                request,
                "profile_email_verify.html",
                {"error": render("policy.customer.contact_medium.unknown")},
                status_code=500,
            )

        if isinstance(result, EmailChangeFailed):
            # No write happened. Audit the failure on a separate session
            # so the rollback above doesn't drop the audit row.
            await db.rollback()
            await _audit_verify_failure(
                request,
                customer_id,
                identity.id,
                f"policy.customer.contact_medium.{result.reason}",
            )
            return templates.TemplateResponse(
                request,
                "profile_email_verify.html",
                {
                    "error": render(
                        f"policy.customer.contact_medium.{result.reason}"
                    )
                },
                status_code=400,
            )

        # Success — commit the cross-schema transaction.
        assert isinstance(result, EmailChangeApplied)
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=identity.id,
            action="email_change",
            route="/profile/contact/email/verify",
            method="POST",
            success=True,
            step_up_consumed=False,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return RedirectResponse(
        url="/profile/contact?flash=email_change", status_code=303
    )


async def _audit_verify_failure(
    request: Request,
    customer_id: str,
    identity_id: str,
    error_rule: str,
) -> None:
    """Write a portal_action row for verify failures on a fresh session.

    The verify route's main session is rolled back on failure (so no
    half-commit lands), but we still want the audit. Open a separate
    session, write, commit.
    """
    factory = request.app.state.db_session_factory
    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=identity_id,
            action="email_change",
            route="/profile/contact/email/verify",
            method="POST",
            success=False,
            error_rule=error_rule,
            step_up_consumed=False,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()


# ── POST /profile/contact/email/cancel — cancel pending ──────────────────


@router.post("/profile/contact/email/cancel")
async def email_change_cancel(
    request: Request,
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    """Cancel an in-flight pending email change.

    No step-up — cancelling is non-destructive (the customer's current
    email keeps working) and we'd rather make this easy than not.
    """
    factory = request.app.state.db_session_factory
    identity = _identity(request)
    async with factory() as db:
        cancelled = await cancel_pending_email_change(db, identity_id=identity.id)
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=identity.id,
            action="email_change",
            route="/profile/contact/email/cancel",
            method="POST",
            success=cancelled,
            error_rule=None if cancelled else "policy.customer.contact_medium.no_active_pending",
            step_up_consumed=False,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()
    return RedirectResponse(
        url="/profile/contact?flash=email_change_cancelled", status_code=303
    )
