"""Signup form + direct-write chain (v0.11+).

v0.4 ran the signup chain through the LLM orchestrator (one streaming
agent call drove every tool invocation through SSE). v0.11 replaces
that with a deterministic direct-API chain: each of the five steps is
its own route, each route makes exactly one ``bss-clients`` call, and
the progress page chains them via HTMX ``hx-post`` triggers. Wall time
drops from ~85s to under 10s because no LLM round-trips happen on the
signup path.

Step routes (one BSS write or zero per route):

* ``POST /signup``            — ``crm.create_customer`` (+ portal-auth
                                 ``link_to_customer`` to bind identity)
* ``POST /signup/step/kyc``   — ``crm.attest_kyc``
* ``POST /signup/step/cof``   — ``payment.create_payment_method``
                                 (PAN tokenized client-side, same
                                 sandbox tokenizer as the v0.10
                                 ``/payment-methods/add`` route)
* ``POST /signup/step/order`` — ``com.create_order`` + ``submit_order``
                                 (the orchestrator's ``order.create``
                                 tool wraps the same two calls; the
                                 service-side workflow is one
                                 conceptual write)
* ``GET /signup/step/poll``   — ``com.get_order`` (read-only). When
                                 ``state == completed`` and the
                                 subscription id is known, emits
                                 ``HX-Redirect`` to ``/confirmation``.

Doctrine (V0_11_0.md + CLAUDE.md ``(v0.11+ / chat only)``):

* No orchestrator imports. The signup routes are no longer in the
  carve-out whitelist for ``rg`` against the orchestrator entrypoint
  identifier under ``portals/self-serve/bss_self_serve/routes/``; the
  only orchestrator-mediated route is ``/chat`` (when it lands).
* ``request.state.identity`` is the only source of email and
  identity_id. The form never carries ``email`` server-side.
* ``portal_action`` audit row per write step (label from
  ``SIGNUP_ACTION_LABELS``); success and failure both recorded.
* Structured ``PolicyViolationFromServer`` errors render via the
  shared ``error_messages.render`` map.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import structlog
from bss_clients import PolicyViolationFromServer
from bss_portal_auth import IdentityView, link_to_customer, record_portal_action
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..clients import get_clients
from ..error_messages import is_known, render
from ..kyc import (
    KycCapExhausted,
    KycCorroborationTimeout,
    PrebakedKycAdapter,
)
from ..offerings import find_plan, flatten_offerings
from ..prompts import KYC_PREBAKED_ATTESTATION_ID, KYC_PREBAKED_SIGNATURE_TEMPLATE
from ..security import requires_verified_email
from ..session import SignupSession
from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


# ── GET /signup/{plan_id} ────────────────────────────────────────────────


@router.get("/signup/{plan_id}", response_class=HTMLResponse)
async def signup_form(
    request: Request,
    plan_id: str,
    msisdn: str = Query(default=..., pattern=r"^[0-9]{6,15}$"),
    identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    clients = get_clients()
    raw = await clients.catalog.list_offerings()
    plan = find_plan(flatten_offerings(raw), plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Unknown plan: {plan_id}")

    # v0.11 — second-line UX. If the verified identity is already
    # linked to a customer, pre-fill the name field from the CRM
    # individual record so the user doesn't retype "Ada Lovelace"
    # the second time they add a line. Best-effort read; falls back
    # to empty placeholder behaviour if the CRM call fails.
    prefill_name = ""
    is_returning = bool(identity.customer_id)
    if is_returning:
        try:
            cust = await clients.crm.get_customer(identity.customer_id)
            ind = cust.get("individual") if isinstance(cust, dict) else None
            if isinstance(ind, dict):
                given = ind.get("givenName") or ind.get("given_name") or ""
                family = ind.get("familyName") or ind.get("family_name") or ""
                prefill_name = f"{given} {family}".strip()
        except Exception:  # noqa: BLE001 — best-effort read
            prefill_name = ""

    # v1.1 — surface the customer's best applicable assigned (targeted) offer so
    # it shows pre-applied on the form with a remove toggle. Only a linked
    # customer can hold offers; a brand-new signup has none. Best-effort.
    assigned_offer = None
    if is_returning:
        try:
            res = await clients.catalog.resolve_eligible_promo(
                customer_id=identity.customer_id, offering=plan_id
            )
            if res.get("valid"):
                assigned_offer = res
        except Exception:  # noqa: BLE001 — non-critical display
            assigned_offer = None

    return templates.TemplateResponse(
        request,
        "signup.html",
        {
            "plan": plan,
            "msisdn": msisdn,
            "msisdn_display": _format_msisdn(msisdn),
            "kyc_attestation_id": KYC_PREBAKED_ATTESTATION_ID,
            "identity_email": identity.email,
            "prefill_name": prefill_name,
            "is_returning": is_returning,
            "assigned_offer": assigned_offer,
            # v0.16 — drives the form's mode-switch (mock card-number
            # input vs Stripe-Elements-deferred-to-COF-step). The
            # publishable key is later read at the COF step's iframe
            # mount.
            "payment_provider": getattr(
                request.app.state, "payment_provider", "mock"
            ),
        },
    )


def _format_msisdn(msisdn: str) -> str:
    if len(msisdn) == 8 and msisdn.isdigit():
        return f"+65 {msisdn[:4]} {msisdn[4:]}"
    return msisdn


# ── POST /signup — step 1: create customer + link identity ──────────────


@router.get("/signup/promo/preview", response_class=HTMLResponse)
async def signup_promo_preview(
    request: Request,
    offering: str = Query(...),
    code: str = Query(default=""),
    # The signup form's input is name="promo_code" (it must match the POST
    # /signup field), so HTMX's hx-include sends ``promo_code``, not ``code``.
    # Accept both; the form's param wins. (Without this the live preview never
    # fired for typed codes — caught when the endpoint test sent ``code`` but
    # the real browser sends ``promo_code``.)
    promo_code: str = Query(default=""),
    has_offer: str = Query(default=""),  # "1" when an assigned offer is pre-applied
    identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    """Live discounted-price preview for the signup form's promo field (v1.1).

    Reads catalog's ``/promo/preview`` (which does the loyalty lookup + composes
    the discount). Best-effort: an empty/invalid code or a catalog hiccup renders
    a benign note and never blocks signup. The portal holds no loyalty token —
    all promo knowledge comes through the catalog client.
    """
    code = (promo_code or code).strip()
    if not code:
        return HTMLResponse("")  # nothing entered yet → clear the preview slot
    try:
        result = await get_clients().catalog.preview_promo(
            code=code, offering=offering, customer_id=identity.customer_id
        )
    except Exception:  # noqa: BLE001 — preview must never break the funnel
        log.warning("signup.promo_preview.failed", offering=offering, exc_info=True)
        result = {"valid": False, "reason": None}
    return templates.TemplateResponse(
        request,
        "partials/promo_preview.html",
        {
            "valid": result.get("valid", False),
            "code": code,
            "label": result.get("label"),
            "base": result.get("base"),
            "effective": result.get("effective"),
            "reason": result.get("reason"),
            # when an offer is pre-applied: a valid code REPLACES it; an invalid
            # code leaves it in place (the backend falls back to the offer).
            "has_offer": bool(has_offer),
        },
    )


@router.post("/signup")
async def signup_submit(
    request: Request,
    plan: str = Form(...),
    name: str = Form(...),
    phone: str = Form(...),
    msisdn: str = Form(...),
    # v0.11 — ``card_pan`` is OPTIONAL on the route signature so the
    # second-line / returning-customer path works even when the form
    # omits the field entirely. Returning users skip the add-card
    # step server-side (existing default COF pays); new users still
    # need a valid PAN — the route enforces that below by branching
    # on identity.customer_id.
    card_pan: str = Form(default=""),
    promo_code: str = Form(default=""),  # v1.1 — optional typed promo code
    # v1.1 — the assigned-offer toggle. `offer_shown` is a hidden marker that the
    # form rendered an offer; `apply_offer` is the checkbox ("1" when ticked,
    # absent when unticked). skip = the offer was shown AND the customer unticked.
    offer_shown: str = Form(default=""),
    apply_offer: str = Form(default=""),
    identity: IdentityView = Depends(requires_verified_email),
) -> Response:
    """Run step 1 (``crm.create_customer``) and bind the verified
    identity to the new customer atomically.

    Email is read from ``identity.email`` (the verified session); the
    form never carries ``email`` to defeat the (identity, customer)
    binding invariant. Same posture as v0.8.

    On success: stash CUST-id on the in-memory signup session, advance
    ``session.step`` to ``"pending_kyc"``, redirect to the progress
    page. The HTMX timeline on that page fires the next step routes.
    """
    store = request.app.state.session_store
    factory = request.app.state.db_session_factory
    clients = get_clients()

    session = await store.create(
        plan=plan,
        name=name,
        email=identity.email,
        phone=phone,
        msisdn=msisdn,
        card_pan=card_pan,
        identity_id=identity.id,
        promo_code=promo_code,
        skip_assigned_offer=bool(offer_shown) and not bool(apply_offer),
    )

    # v0.11 — second-line support. If the verified identity is already
    # linked to a customer (typical case: visitor already signed up
    # once and is now adding another line), reuse that customer_id
    # instead of creating a fresh CRM customer. The chain then jumps
    # past create-customer + KYC (already attested on the prior
    # signup) and the COF step is gated on whether they want a new
    # card or to reuse their existing default. v0.4 / v0.8 / v0.10's
    # signup chain blindly called crm.create_customer every time and
    # left an orphan CUST in CRM if link_to_customer rejected the
    # second link — that's the bug a returning visitor reported.
    existing_customer_id = identity.customer_id

    # New customers MUST supply a PAN in mock mode — the COF step
    # will need it. Stripe mode collects the card inside the Elements
    # iframe at the COF step, so the PAN is intentionally absent here
    # (and the form omits the input entirely; PCI doctrine).
    payment_provider = getattr(request.app.state, "payment_provider", "mock")
    if (
        not existing_customer_id
        and payment_provider == "mock"
        and not card_pan.strip()
    ):
        await store.update(session)
        return await _render_failed(
            request, session, "policy.payment.method.invalid_card"
        )

    if existing_customer_id:
        session.customer_id = existing_customer_id
        # Skip create-customer (already exists), KYC (already attested
        # — CRM's ``document_hash_unique_per_tenant`` would reject a
        # duplicate anyway), AND COF (the customer's existing default
        # method on file pays for the new line; no need to make them
        # re-enter card details for a second line). Jump straight to
        # placing the order.
        session.step = "pending_order"
        await _record_step(
            factory,
            session,
            customer_id=existing_customer_id,
            action="signup_create_customer",
            route="/signup",
            success=True,
            error_rule="signup.create_customer.reused_linked_identity",
            request=request,
        )
        await store.update(session)
        return RedirectResponse(
            url=f"/signup/{plan}/progress?session={session.session_id}",
            status_code=303,
        )

    try:
        customer = await clients.crm.create_customer(
            name=name,
            email=identity.email,
            phone=phone,
        )
    except PolicyViolationFromServer as exc:
        await _record_step(
            factory,
            session,
            customer_id=None,
            action="signup_create_customer",
            route="/signup",
            success=False,
            error_rule=exc.rule,
            request=request,
        )
        if not is_known(exc.rule):
            log.info("portal.signup.unknown_policy_rule", rule=exc.rule)
        session.step = "failed"
        session.step_error = exc.rule
        await store.update(session)
        return await _render_failed(request, session, exc.rule)

    customer_id = customer.get("id")
    if not isinstance(customer_id, str):
        # Shouldn't happen — CRM contract is to return id on success.
        # Treat as a failed step with an unknown rule so the audit row
        # captures the anomaly.
        await _record_step(
            factory,
            session,
            customer_id=None,
            action="signup_create_customer",
            route="/signup",
            success=False,
            error_rule="signup.create_customer.no_id",
            request=request,
        )
        session.step = "failed"
        session.step_error = "signup.create_customer.no_id"
        await store.update(session)
        return await _render_failed(
            request, session, "signup.create_customer.no_id"
        )

    session.customer_id = customer_id

    # v0.8 — atomically bind the verified identity to the new customer.
    # Linking BEFORE the rest of the chain runs means a mid-flow abandon
    # still leaves the (identity, customer) pair intact, so a returning
    # visitor under the same email reuses their customer record.
    if factory is not None:
        try:
            async with factory() as db:
                await link_to_customer(
                    db,
                    identity_id=identity.id,
                    customer_id=customer_id,
                )
                await db.commit()
        except ValueError as exc:
            # Identity was already linked to a different customer. The
            # customer record now exists in CRM but the identity points
            # somewhere else; surface as a failed step. No portal-side
            # rollback for the orphan customer — see the v0.10 doctrine
            # carve-out: composition is service-side, not in the route.
            log.warning(
                "portal.signup.link_failed",
                identity_id=identity.id,
                customer_id=customer_id,
                error=str(exc),
            )

    await _record_step(
        factory,
        session,
        customer_id=customer_id,
        action="signup_create_customer",
        route="/signup",
        success=True,
        request=request,
    )

    session.step = "pending_kyc"
    await store.update(session)

    return RedirectResponse(
        url=f"/signup/{plan}/progress?session={session.session_id}",
        status_code=303,
    )


# ── GET /signup/{plan_id}/progress — the deterministic 5-step timeline ──


@router.get("/signup/{plan_id}/progress", response_class=HTMLResponse)
async def signup_progress(
    request: Request,
    plan_id: str,
    session: str,
    identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    store = request.app.state.session_store
    sig = await store.get(session)
    if sig is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session.")
    # Defence-in-depth: a logged-in user shouldn't be able to peek at
    # someone else's in-flight signup by guessing the session id.
    if sig.identity_id and sig.identity_id != identity.id:
        raise HTTPException(status_code=404, detail="Unknown or expired session.")

    return templates.TemplateResponse(
        request,
        "progress.html",
        {
            "session_id": session,
            "plan_id": plan_id,
            "signup": sig,
            # v0.16 — drives the {% if payment_provider == 'stripe' %}
            # block in progress.html that pre-loads Stripe.js eagerly,
            # so window.Stripe is defined before any HTMX-injected
            # Elements-mount fragment runs. Same field also flows into
            # the included signup_progress.html partial via _render_step_fragment.
            "payment_provider": getattr(
                request.app.state, "payment_provider", "mock"
            ),
            "stripe_publishable_key": getattr(
                request.app.state, "payment_stripe_publishable_key", ""
            ),
        },
    )


# ── POST /signup/step/kyc — step 2 ──────────────────────────────────────


@router.post("/signup/step/kyc", response_class=HTMLResponse)
async def signup_step_kyc(
    request: Request,
    session: str = Query(...),
    identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    """v0.15 — flow through ``app.state.kyc_adapter``.

    Prebaked mode runs the full attest synchronously (same end state as
    v0.14, just routed through the adapter). Didit mode renders a
    cross-device handoff page (URL + QR code) with HTMX polling — the
    customer can complete verification on the same device or scan to
    finish on a phone, and the desktop advances the moment the
    corroboration webhook arrives. No HX-Redirect; both devices stay
    on their own page.
    """
    store = request.app.state.session_store
    factory = request.app.state.db_session_factory
    adapter = request.app.state.kyc_adapter
    sig = await _resolve(store, session, identity)
    if sig.step != "pending_kyc":
        return _render_step_fragment(request, sig)

    public_url = request.app.state.settings.bss_portal_public_url.rstrip("/")
    return_url = f"{public_url}/signup/step/kyc/callback?session={session}"

    try:
        kyc_session = await adapter.initiate(
            email=sig.email, return_url=return_url
        )
    except KycCapExhausted:
        # Hard-block, no fallback. Templated retry page; ops gets the event.
        log.warning("portal.signup.kyc.cap_exhausted")
        sig.step = "failed"
        sig.step_error = "kyc.cap_exhausted"
        await store.update(sig)
        return _render_step_fragment(request, sig)

    if isinstance(adapter, PrebakedKycAdapter):
        # Synchronous: complete the attest in this request and advance.
        return await _complete_kyc_attest(
            request=request,
            store=store,
            factory=factory,
            adapter=adapter,
            sig=sig,
            kyc_session=kyc_session,
        )

    # Real provider (Didit) — set state to pending_kyc_handoff and
    # let the progress fragment render the URL + QR + poll inside the
    # progress card (so the timeline + plan heading stay visible).
    # The customer can verify on same device OR scan to phone; either
    # way the corroboration webhook arrival is what advances the
    # signup, NOT the post-verification redirect.
    from ..qrpng import qr_data_uri

    sig.kyc_provider_session_id = kyc_session.session_id
    sig.kyc_verify_url = kyc_session.redirect_url
    sig.kyc_verify_qr = qr_data_uri(kyc_session.redirect_url)
    sig.step = "pending_kyc_handoff"
    await store.update(sig)
    return _render_step_fragment(request, sig)


# ── GET /signup/step/kyc/poll — desktop poll for corroboration row ──────


@router.get("/signup/step/kyc/poll", response_class=HTMLResponse)
async def signup_step_kyc_poll(
    request: Request,
    session: str = Query(...),
    identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    """Desktop polls every 3s during the cross-device KYC handoff. When
    the corroboration row appears (HMAC-verified webhook landed), this
    endpoint completes the attest call and returns ``HX-Redirect`` to
    the signup progress page. Until then it returns a small "still
    waiting" fragment that re-arms the poll.
    """
    store = request.app.state.session_store
    factory = request.app.state.db_session_factory
    adapter = request.app.state.kyc_adapter
    sig = await _resolve(store, session, identity)

    # Already advanced or failed — render the current state fragment.
    # The fragment's natural trigger advances the chain from there.
    if sig.step != "pending_kyc_handoff":
        return _render_step_fragment(request, sig)

    provider_session_id = getattr(sig, "kyc_provider_session_id", None) or ""
    if not provider_session_id:
        # Initiate hasn't populated yet; render the same fragment so the
        # poll re-arms.
        return _render_step_fragment(request, sig)

    from bss_models.integrations import KycWebhookCorroboration
    from sqlalchemy import select

    factory_inner = request.app.state.db_session_factory
    async with factory_inner() as db:
        row = (
            await db.execute(
                select(KycWebhookCorroboration).where(
                    KycWebhookCorroboration.provider == "didit",
                    KycWebhookCorroboration.provider_session_id
                    == provider_session_id,
                )
            )
        ).scalar_one_or_none()

    # Didit's first webhook for a session arrives at session-creation
    # ("Not Started"); subsequent ones progress through "In Progress" /
    # "In Review" before landing on a terminal state (Approved /
    # Declined / Expired). The corroboration row is updated in place by
    # the webhook handler. Only act on a TERMINAL status.
    if row is None or row.decision_status in ("Not Started", "In Progress", "In Review"):
        return _render_step_fragment(request, sig)

    if row.decision_status in ("Declined", "Expired"):
        sig.step = "failed"
        sig.step_error = f"kyc.{row.decision_status.lower()}"
        await store.update(sig)
        return _render_step_fragment(request, sig)

    if row.decision_status != "Approved":
        # Unknown terminal status — keep waiting (forward-compat).
        return _render_step_fragment(request, sig)

    # Approved — complete the BSS attest. The completion advances state
    # to pending_cof and renders the next fragment, with its own
    # built-in trigger that fires the next step.
    return await _complete_kyc_attest(
        request=request,
        store=store,
        factory=factory,
        adapter=adapter,
        sig=sig,
        kyc_session=type("_KycSession", (), {"session_id": provider_session_id})(),
    )


# ── GET /signup/step/kyc/callback — return path from hosted UI ──────────


@router.get("/signup/step/kyc/callback", response_class=HTMLResponse)
async def signup_step_kyc_callback(
    request: Request,
    session: str = Query(...),
) -> HTMLResponse:
    """Public route — the customer's browser lands here after Didit's
    hosted UI completes. In the cross-device case the customer's PHONE
    arrives here without the desktop's portal session cookie, so this
    route MUST NOT require auth. It just renders a "verification
    complete, return to your computer" confirmation. The desktop's
    poll loop is what actually advances the signup; this page is
    purely a friendly UX endpoint for the verifying device.
    """
    return templates.TemplateResponse(
        request, "signup_kyc_confirmation.html", {"session_id": session}
    )


async def _complete_kyc_attest(
    *,
    request: Request,
    store,
    factory,
    adapter,
    sig: SignupSession,
    kyc_session,
    redirect_after: bool = False,
) -> HTMLResponse:
    """Shared finisher: fetch attestation, submit to BSS, advance signup.

    Used by both the synchronous prebaked path and the Didit return-callback.
    Honors the existing ``portal_action`` audit row contract.
    """
    try:
        attestation = await adapter.fetch_attestation(
            session_id=kyc_session.session_id
        )
    except KycCorroborationTimeout:
        log.info(
            "portal.signup.kyc.corroboration_timeout",
            provider_session=kyc_session.session_id,
        )
        sig.step_error = "kyc.corroboration_timeout"
        await store.update(sig)
        if redirect_after:
            return RedirectResponse(
                url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
                status_code=303,
            )
        return _render_step_fragment(request, sig)

    clients = get_clients()
    try:
        await clients.crm.attest_kyc(
            sig.customer_id,
            provider=attestation.provider,
            attestation_token=KYC_PREBAKED_SIGNATURE_TEMPLATE.format(email=sig.email),
            provider_reference=attestation.provider_reference,
            document_type=attestation.document_type,
            document_number_last4=attestation.document_number_last4,
            document_number_hash=attestation.document_number_hash,
            document_country=attestation.document_country,
            date_of_birth=attestation.date_of_birth.isoformat(),
            corroboration_id=str(attestation.corroboration_id) if attestation.corroboration_id else None,
        )
    except PolicyViolationFromServer as exc:
        await _record_step(
            factory,
            sig,
            customer_id=sig.customer_id,
            action="signup_attest_kyc",
            route="/signup/step/kyc",
            success=False,
            error_rule=exc.rule,
            request=request,
        )
        if not is_known(exc.rule):
            log.info("portal.signup.unknown_policy_rule", rule=exc.rule)
        sig.step = "failed"
        sig.step_error = exc.rule
        await store.update(sig)
        if redirect_after:
            return RedirectResponse(
                url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
                status_code=303,
            )
        return _render_step_fragment(request, sig)

    await _record_step(
        factory,
        sig,
        customer_id=sig.customer_id,
        action="signup_attest_kyc",
        route="/signup/step/kyc",
        success=True,
        request=request,
    )
    sig.step = "pending_cof"
    await store.update(sig)
    if redirect_after:
        return RedirectResponse(
            url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
            status_code=303,
        )
    return _render_step_fragment(request, sig)


# ── POST /signup/step/cof — step 3 ──────────────────────────────────────


@router.post("/signup/step/cof", response_class=HTMLResponse)
async def signup_step_cof(
    request: Request,
    session: str = Query(...),
    identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    """Add card on file. Mock and stripe paths share this entry point.

    - Mock mode: auto-fired by HTMX from `pending_cof`. Uses the
      `card_pan` we collected on the original signup form, runs it
      through the local mock tokenizer, calls
      `payment.create_payment_method(token_provider='mock')`, advances
      to `pending_order`.
    - Stripe mode: customer enters card inside the Stripe Elements
      iframe; on success Stripe.js POSTs the resulting `pm_*` id here
      via fetch (the form's hidden `payment_method_id` field carries
      it). We call `payment.create_payment_method(token_provider='stripe',
      provider_token=pm_id)` and advance.
    """
    store = request.app.state.session_store
    factory = request.app.state.db_session_factory
    sig = await _resolve(store, session, identity)

    # v0.16: detect whether this is an HTMX-AJAX call or a plain
    # browser navigation (form submit when JS failed to intercept).
    # HTMX sets HX-Request: true on every fetch; if it's missing
    # AND the user is hitting this from a real browser, redirect
    # back to the progress page so they don't see a bare partial.
    is_htmx = request.headers.get("HX-Request", "").lower() == "true"

    # Stripe mode: the customer's pm_* id is the only thing on the form.
    # The state may be `pending_cof` (immediate fetch from Elements
    # success) or `pending_cof_elements` (after the mount endpoint had
    # already pivoted the state); accept both.
    payment_provider = getattr(request.app.state, "payment_provider", "mock")
    if payment_provider == "stripe":
        if sig.step not in ("pending_cof", "pending_cof_elements"):
            if not is_htmx:
                return RedirectResponse(
                    url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
                    status_code=303,
                )
            return _render_step_fragment(request, sig)
        form = await request.form()
        pm_id = (form.get("payment_method_id") or "").strip()
        if not pm_id:
            # No pm_* yet — render the Elements iframe again. Polling
            # path lands here too. If this came from a non-HTMX form
            # submit (Safari ITP blocked the JS, browser POSTed the
            # bare form), redirect back to the progress page so the
            # customer sees the full styled page, not a bare partial.
            sig.step = "pending_cof_elements"
            await store.update(sig)
            if not is_htmx:
                return RedirectResponse(
                    url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
                    status_code=303,
                )
            return _render_step_fragment(request, sig)
        return await _signup_step_cof_stripe(
            request,
            store=store,
            factory=factory,
            sig=sig,
            payment_method_id=pm_id,
        )

    # Mock mode (v0.1-v0.15 default).
    if sig.step != "pending_cof":
        return _render_step_fragment(request, sig)
    return await _signup_step_cof_mock(
        request, store=store, factory=factory, sig=sig
    )


async def _signup_step_cof_mock(
    request: Request,
    *,
    store,
    factory,
    sig: SignupSession,
) -> HTMLResponse:
    clients = get_clients()
    try:
        tok = _local_tokenize(sig.card_pan)
    except ValueError:
        await _record_step(
            factory,
            sig,
            customer_id=sig.customer_id,
            action="signup_add_card",
            route="/signup/step/cof",
            success=False,
            error_rule="policy.payment.method.invalid_card",
            request=request,
        )
        sig.step = "failed"
        sig.step_error = "policy.payment.method.invalid_card"
        await store.update(sig)
        return _render_step_fragment(request, sig)

    try:
        method = await clients.payment.create_payment_method(
            customer_id=sig.customer_id,
            card_token=tok["cardToken"],
            last4=tok["last4"],
            brand=tok["brand"],
        )
    except PolicyViolationFromServer as exc:
        await _record_step(
            factory,
            sig,
            customer_id=sig.customer_id,
            action="signup_add_card",
            route="/signup/step/cof",
            success=False,
            error_rule=exc.rule,
            request=request,
        )
        if not is_known(exc.rule):
            log.info("portal.signup.unknown_policy_rule", rule=exc.rule)
        sig.step = "failed"
        sig.step_error = exc.rule
        await store.update(sig)
        return _render_step_fragment(request, sig)

    sig.payment_method_id = method.get("id") if isinstance(method, dict) else None
    # Card PAN cleared from memory the moment the tokenizer + add_card
    # succeed — same posture as v0.4. From here on the only artifact is
    # last-4, which the templates already use.
    sig.card_pan = ""
    await _record_step(
        factory,
        sig,
        customer_id=sig.customer_id,
        action="signup_add_card",
        route="/signup/step/cof",
        success=True,
        request=request,
    )
    sig.step = "pending_order"
    await store.update(sig)
    return _render_step_fragment(request, sig)


async def _signup_step_cof_stripe(
    request: Request,
    *,
    store,
    factory,
    sig: SignupSession,
    payment_method_id: str,
) -> HTMLResponse:
    """v0.16 stripe path — register the pm_* with the payment service.

    PaymentMethodService.register_method handles the
    ensure_customer + attach round-trips on the service side
    (DECISIONS 2026-05-03 — payment.customer caches per-(BSS customer,
    provider) external ref). The portal just hands over pm_id +
    customer_id and gets back the BSS-side PM-* id.
    """
    clients = get_clients()
    try:
        method = await clients.payment.create_payment_method(
            customer_id=sig.customer_id,
            card_token=payment_method_id,
            # v0.16 — last4/brand come back in the response (Stripe
            # exposes them on PaymentMethod.card). Submitting empty here
            # is fine; the service derives them from Stripe.
            last4="",
            brand="",
            tokenization_provider="stripe",
        )
    except PolicyViolationFromServer as exc:
        await _record_step(
            factory,
            sig,
            customer_id=sig.customer_id,
            action="signup_add_card",
            route="/signup/step/cof",
            success=False,
            error_rule=exc.rule,
            request=request,
        )
        if not is_known(exc.rule):
            log.info("portal.signup.unknown_policy_rule", rule=exc.rule)
        sig.step = "failed"
        sig.step_error = exc.rule
        await store.update(sig)
        return _render_step_fragment(request, sig)

    sig.payment_method_id = (
        method.get("id") if isinstance(method, dict) else None
    )
    await _record_step(
        factory,
        sig,
        customer_id=sig.customer_id,
        action="signup_add_card",
        route="/signup/step/cof",
        success=True,
        request=request,
    )
    sig.step = "pending_order"
    await store.update(sig)
    return _render_step_fragment(request, sig)


# ── v0.16 Stripe Checkout (replaces the Elements iframe path) ────────
#
# Spec amendment: Track 2 originally specified Stripe.js + Elements
# (DECISIONS 2026-05-03 PCI scope entry). In manual smoke testing the
# Elements iframe failed to render reliably across browsers (Safari ITP,
# strict cross-origin storage, HTMX swap script-execution issues). The
# pragmatic answer: switch to Stripe Checkout — full-page redirect to
# a Stripe-hosted card form. No iframe, no Stripe.js in the browser,
# no client-side state. Works in every browser.
#
# Two routes: init (POST → redirect to Stripe) and return (GET ← Stripe
# redirects back to here with the cs_id; we retrieve + register the pm_*).


@router.post("/signup/step/cof/checkout-init")
async def signup_step_cof_checkout_init(
    request: Request,
    session: str = Query(...),
    identity: IdentityView = Depends(requires_verified_email),
) -> Response:
    """Mint a Stripe CheckoutSession + 303 redirect to Stripe's hosted form.

    Flow:
      1. Look up the BSS customer's email via portal_auth.identity.
      2. Call payment-service ensure_customer to mint/cache the cus_*.
      3. Call stripe.checkout.Session.create(mode="setup", ...) with
         that customer attached, success/cancel URLs pointing back here.
      4. 303 redirect to session.url — the customer's browser navigates
         to the Stripe-hosted card form.
    """
    import stripe

    store = request.app.state.session_store
    sig = await _resolve(store, session, identity)
    if sig.step not in ("pending_cof", "pending_cof_elements"):
        return RedirectResponse(
            url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
            status_code=303,
        )

    settings = request.app.state.settings
    api_key = getattr(request.app.state, "payment_stripe_api_key", "")
    public_url = settings.bss_portal_public_url.rstrip("/")
    if not api_key:
        sig.step = "failed"
        sig.step_error = "policy.payment.checkout.stripe_unconfigured"
        await store.update(sig)
        return _render_step_fragment(request, sig)

    # Step 1+2: ensure the cus_* exists (cached after first call).
    clients = get_clients()
    try:
        ensure_resp = await clients.payment.ensure_customer(
            customer_id=sig.customer_id,
            email=identity.email,
        )
        cus_id = ensure_resp["customer_external_ref"]
    except PolicyViolationFromServer as exc:
        sig.step = "failed"
        sig.step_error = exc.rule
        await store.update(sig)
        return _render_step_fragment(request, sig)
    except Exception as exc:  # noqa: BLE001
        log.warning("portal.signup.ensure_customer_failed", error=str(exc))
        sig.step = "failed"
        sig.step_error = "policy.payment.checkout.ensure_customer_failed"
        await store.update(sig)
        return _render_step_fragment(request, sig)

    # Step 3: create the Stripe CheckoutSession.
    return_url = (
        f"{public_url}/signup/step/cof/checkout-return"
        f"?session={sig.session_id}"
        # Stripe's {CHECKOUT_SESSION_ID} placeholder is filled by Stripe
        # at redirect time, so we know which session to retrieve.
        f"&cs_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = f"{public_url}/signup/{sig.plan}/progress?session={sig.session_id}"
    try:
        cs = await asyncio.to_thread(
            stripe.checkout.Session.create,
            api_key=api_key,
            mode="setup",
            payment_method_types=["card"],
            customer=cus_id,
            success_url=return_url,
            cancel_url=cancel_url,
            metadata={
                "bss_customer_id": sig.customer_id,
                "bss_signup_session": sig.session_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "portal.signup.checkout_session_create_failed",
            customer_id=sig.customer_id,
            error=str(exc),
        )
        sig.step = "failed"
        sig.step_error = "policy.payment.checkout.session_create_failed"
        await store.update(sig)
        return _render_step_fragment(request, sig)

    cs_url = cs.get("url") if isinstance(cs, dict) else cs.url
    sig.step = "pending_cof_elements"  # repurposed: "checkout in flight"
    await store.update(sig)
    log.info(
        "portal.signup.checkout_redirect",
        signup_session=sig.session_id,
        customer_id=sig.customer_id,
    )
    return RedirectResponse(url=cs_url, status_code=303)


@router.get("/signup/step/cof/checkout-return")
async def signup_step_cof_checkout_return(
    request: Request,
    session: str = Query(...),
    cs_id: str = Query(...),
    identity: IdentityView = Depends(requires_verified_email),
) -> Response:
    """Stripe redirects the customer back here after completing Checkout.

    Retrieves the CheckoutSession, extracts the resulting pm_*, and
    registers it via bss-clients (which calls PaymentMethodService.register_method
    → token_provider='stripe' → cus_* attach is a no-op since Checkout
    pre-attached). Then advances state to pending_order.
    """
    import stripe

    store = request.app.state.session_store
    factory = request.app.state.db_session_factory
    sig = await _resolve(store, session, identity)

    if sig.step not in ("pending_cof", "pending_cof_elements"):
        # Already advanced — bounce back to progress.
        return RedirectResponse(
            url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
            status_code=303,
        )

    api_key = getattr(request.app.state, "payment_stripe_api_key", "")
    if not api_key or not cs_id.startswith("cs_"):
        sig.step = "failed"
        sig.step_error = "policy.payment.checkout.bad_return"
        await store.update(sig)
        return RedirectResponse(
            url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
            status_code=303,
        )

    try:
        cs = await asyncio.to_thread(
            stripe.checkout.Session.retrieve,
            cs_id,
            api_key=api_key,
            expand=["setup_intent"],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "portal.signup.checkout_session_retrieve_failed",
            cs_id=cs_id,
            error=str(exc),
        )
        sig.step = "failed"
        sig.step_error = "policy.payment.checkout.retrieve_failed"
        await store.update(sig)
        return RedirectResponse(
            url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
            status_code=303,
        )

    # Defence in depth: refuse if metadata doesn't match this signup.
    cs_dict = cs.to_dict() if hasattr(cs, "to_dict") else dict(cs)
    meta = cs_dict.get("metadata") or {}
    if meta.get("bss_signup_session") != sig.session_id:
        log.warning(
            "portal.signup.checkout_session_metadata_mismatch",
            cs_id=cs_id,
            signup_session=sig.session_id,
            cs_signup_session=meta.get("bss_signup_session"),
        )
        sig.step = "failed"
        sig.step_error = "policy.payment.checkout.metadata_mismatch"
        await store.update(sig)
        return RedirectResponse(
            url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
            status_code=303,
        )

    # Extract the pm_* — for mode="setup", lives at setup_intent.payment_method.
    si = cs_dict.get("setup_intent") or {}
    if isinstance(si, str):
        # Not expanded (shouldn't happen since we passed expand=); refetch.
        si_obj = await asyncio.to_thread(
            stripe.SetupIntent.retrieve, si, api_key=api_key
        )
        si = si_obj.to_dict() if hasattr(si_obj, "to_dict") else dict(si_obj)
    pm_id = si.get("payment_method") if isinstance(si, dict) else None
    if not pm_id or not str(pm_id).startswith("pm_"):
        log.warning(
            "portal.signup.checkout_no_pm",
            cs_id=cs_id,
            setup_intent_status=si.get("status") if isinstance(si, dict) else None,
        )
        sig.step = "failed"
        sig.step_error = "policy.payment.checkout.no_payment_method"
        await store.update(sig)
        return RedirectResponse(
            url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
            status_code=303,
        )

    # Register via the existing stripe-mode payment-method flow.
    clients = get_clients()
    try:
        method = await clients.payment.create_payment_method(
            customer_id=sig.customer_id,
            card_token=pm_id,
            last4="",
            brand="",
            tokenization_provider="stripe",
        )
    except PolicyViolationFromServer as exc:
        await _record_step(
            factory,
            sig,
            customer_id=sig.customer_id,
            action="signup_add_card",
            route="/signup/step/cof/checkout-return",
            success=False,
            error_rule=exc.rule,
            request=request,
        )
        if not is_known(exc.rule):
            log.info("portal.signup.unknown_policy_rule", rule=exc.rule)
        sig.step = "failed"
        sig.step_error = exc.rule
        await store.update(sig)
        return RedirectResponse(
            url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
            status_code=303,
        )

    sig.payment_method_id = (
        method.get("id") if isinstance(method, dict) else None
    )
    await _record_step(
        factory,
        sig,
        customer_id=sig.customer_id,
        action="signup_add_card",
        route="/signup/step/cof/checkout-return",
        success=True,
        request=request,
    )
    sig.step = "pending_order"
    await store.update(sig)
    return RedirectResponse(
        url=f"/signup/{sig.plan}/progress?session={sig.session_id}",
        status_code=303,
    )


# ── POST /signup/step/order — step 4 ────────────────────────────────────


@router.post("/signup/step/order", response_class=HTMLResponse)
async def signup_step_order(
    request: Request,
    session: str = Query(...),
    identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    store = request.app.state.session_store
    factory = request.app.state.db_session_factory
    sig = await _resolve(store, session, identity)
    if sig.step != "pending_order":
        return _render_step_fragment(request, sig)

    clients = get_clients()
    try:
        # The orchestrator's ``order.create`` tool wraps create_order +
        # submit_order. The service-side workflow is one conceptual
        # write; we mirror the same composition here so the activation
        # state machine kicks off without a separate user-visible step.
        created = await clients.com.create_order(
            customer_id=sig.customer_id,
            offering_id=sig.plan,
            msisdn_preference=sig.msisdn,
            # v1.1 — typed promo code (empty → none). COM validates via catalog;
            # an invalid code is ignored (order proceeds at full price). When no
            # code is typed, COM auto-applies the cheapest assigned offer unless
            # the customer unticked it (skip_assigned_offer).
            discount_code=sig.promo_code or None,
            skip_assigned_offer=sig.skip_assigned_offer,
        )
        order_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(order_id, str):
            raise PolicyViolationFromServer(
                rule="signup.create_order.no_id",
                message="Order create did not return an id.",
            )
        await clients.com.submit_order(order_id)
    except PolicyViolationFromServer as exc:
        await _record_step(
            factory,
            sig,
            customer_id=sig.customer_id,
            action="signup_create_order",
            route="/signup/step/order",
            success=False,
            error_rule=exc.rule,
            request=request,
        )
        if not is_known(exc.rule):
            log.info("portal.signup.unknown_policy_rule", rule=exc.rule)
        sig.step = "failed"
        sig.step_error = exc.rule
        await store.update(sig)
        return _render_step_fragment(request, sig)

    sig.order_id = order_id
    await _record_step(
        factory,
        sig,
        customer_id=sig.customer_id,
        action="signup_create_order",
        route="/signup/step/order",
        success=True,
        request=request,
    )
    sig.step = "pending_activation"
    await store.update(sig)
    return _render_step_fragment(request, sig)


# ── GET /signup/step/poll — step 5 (read-only) ──────────────────────────


@router.get("/signup/step/poll", response_class=HTMLResponse)
async def signup_step_poll(
    request: Request,
    session: str = Query(...),
    identity: IdentityView = Depends(requires_verified_email),
) -> Response:
    """Poll ``com.get_order`` until ``state == completed``.

    No write — the order activation runs server-side via the SOM /
    provisioning workflow. When the order resolves we extract the
    subscription id and emit ``HX-Redirect`` so HTMX swaps the whole
    page to ``/confirmation``.
    """
    store = request.app.state.session_store
    sig = await _resolve(store, session, identity)

    # If the chain already reached completion on a prior poll visit
    # AND the celebration dwell has elapsed (the delayed re-trigger
    # fired this call), emit HX-Redirect now. The first detection
    # below sets ``redirect_armed`` and returns the celebration
    # fragment with a 1.5s delayed re-trigger; the second call lands
    # here and navigates.
    if sig.step == "completed" and sig.redirect_armed and sig.subscription_id:
        resp = HTMLResponse(content="")
        resp.headers["HX-Redirect"] = (
            f"/confirmation/{sig.subscription_id}?session={sig.session_id}"
        )
        return resp

    if sig.step in ("completed", "failed"):
        return _render_step_fragment(request, sig)

    if sig.step != "pending_activation" or not sig.order_id:
        # Still earlier in the chain — render the timeline; the next
        # step's ``hx-trigger="load"`` will fire from the fragment.
        return _render_step_fragment(request, sig)

    clients = get_clients()
    try:
        order = await clients.com.get_order(sig.order_id)
    except Exception:  # noqa: BLE001 — best-effort poll, retry next tick
        return _render_step_fragment(request, sig)

    state = order.get("state") if isinstance(order, dict) else None
    if state == "completed":
        sub_id = _extract_subscription_id(order)
        if not sub_id:
            # Order is "completed" on the COM side but the
            # ``targetSubscriptionId`` hasn't been stamped onto the
            # order item yet — the SOM service emits the activation
            # event before the COM event handler updates the order
            # row, so there's a small race window. Treat as still in
            # progress and re-trigger the poll on the next tick. The
            # next call will see the stamped id.
            return _render_step_fragment(request, sig)
        sig.subscription_id = sub_id
        sig.activation_code = _extract_activation_code(order)
        sig.step = "completed"
        sig.done = True
        sig.redirect_armed = True
        await store.update(sig)
        # First detection — render the "all 5 ticks ✓" celebration
        # fragment. The partial's completed-branch carries a 1.5s
        # delayed re-trigger to /signup/step/poll; that next visit
        # falls through the early ``redirect_armed`` branch above and
        # emits HX-Redirect to /confirmation. The dwell time is
        # deliberate so the user sees the chain finish.
        return _render_step_fragment(request, sig)

    if state in ("failed", "cancelled"):
        sig.step = "failed"
        sig.step_error = f"order.{state}"
        await store.update(sig)
        return _render_step_fragment(request, sig)

    # Still in progress — re-render the fragment, which carries the
    # ``hx-trigger`` to fire again after a short delay.
    return _render_step_fragment(request, sig)


# ── Helpers ─────────────────────────────────────────────────────────────


async def _resolve(
    store: Any,
    session_id: str,
    identity: IdentityView,
) -> SignupSession:
    sig = await store.get(session_id)
    if sig is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session.")
    if sig.identity_id and sig.identity_id != identity.id:
        raise HTTPException(status_code=404, detail="Unknown or expired session.")
    return sig


async def _record_step(
    factory: Any,
    sig: SignupSession,
    *,
    customer_id: str | None,
    action: str,
    route: str,
    success: bool,
    error_rule: str | None = None,
    request: Request,
) -> None:
    """Append one ``portal_action`` row per step (success or failure).

    ``factory`` may be ``None`` in unit-test app construction without
    a DB; the failure branch logs and continues so the in-memory
    chain still completes. The structured-error rendering doesn't
    depend on the audit row.
    """
    if factory is None:
        return
    iid = sig.identity_id
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    try:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action=action,
                route=route,
                method="POST",
                success=success,
                error_rule=error_rule,
                step_up_consumed=False,
                ip=ip,
                user_agent=ua,
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — audit best-effort
        log.warning(
            "portal.signup.audit_failed",
            action=action,
            error=str(exc),
        )


def _render_step_fragment(request: Request, sig: SignupSession) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/signup_progress.html",
        {
            "signup": sig,
            "session_id": sig.session_id,
            "plan_id": sig.plan,
            "step_error_message": (
                render(sig.step_error) if sig.step_error else None
            ),
            # v0.16 — read by the COF branch to decide between the
            # mock auto-tokenize trigger and the Stripe.js + Elements
            # mount + pm_id submission flow.
            "payment_provider": getattr(
                request.app.state, "payment_provider", "mock"
            ),
            "stripe_publishable_key": getattr(
                request.app.state, "payment_stripe_publishable_key", ""
            ),
        },
    )


async def _render_failed(
    request: Request, sig: SignupSession, rule: str
) -> HTMLResponse:
    """POST /signup failure path — re-render the form with a structured error."""
    plan: dict[str, Any] | None = None
    try:
        raw = await get_clients().catalog.list_offerings()
        plan = find_plan(flatten_offerings(raw), sig.plan)
    except Exception:  # noqa: BLE001
        plan = None
    return templates.TemplateResponse(
        request,
        "signup.html",
        {
            "plan": plan or {"id": sig.plan, "name": sig.plan},
            "msisdn": sig.msisdn,
            "msisdn_display": _format_msisdn(sig.msisdn),
            "kyc_attestation_id": KYC_PREBAKED_ATTESTATION_ID,
            "identity_email": sig.email,
            "error": render(rule),
        },
        status_code=422,
    )


def _local_tokenize(card_number: str) -> dict[str, str]:
    """Sandbox client-side tokenizer.

    Mirrors ``routes/payment_methods.py::_local_tokenize`` and the
    orchestrator's ``payment._local_tokenize`` so the v1.0 cutover to
    real Stripe.js / Adyen tokenization doesn't change the wire shape
    from portal → payment service. Embeds ``FAIL`` / ``DECLINE`` in
    the token so the payment service mock can simulate declines.
    """
    digits = card_number.replace(" ", "").replace("-", "")
    if not digits.isdigit() or len(digits) < 12:
        raise ValueError("Card number is invalid.")
    last4 = digits[-4:]
    bin2 = digits[:2]
    if digits[0] == "4":
        brand = "visa"
    elif 51 <= int(bin2) <= 55:
        brand = "mastercard"
    elif bin2 in ("34", "37"):
        brand = "amex"
    else:
        brand = "unknown"
    uid = str(uuid4())
    upper = card_number.upper()
    if "FAIL" in upper:
        token = f"tok_FAIL_{uid}"
    elif "DECLINE" in upper:
        token = f"tok_DECLINE_{uid}"
    else:
        token = f"tok_{uid}"
    return {"cardToken": token, "last4": last4, "brand": brand}


_SUB_ID_KEYS = (
    "targetSubscriptionId",  # COM's TMF622 order-item envelope (real shape)
    "target_subscription_id",
    "subscriptionId",
    "subscription_id",
)


def _extract_subscription_id(order: dict[str, Any]) -> str | None:
    """Pull the SUB-* id off a completed order payload.

    COM's TMF622 order envelope carries the resulting subscription id
    in ``items[*].targetSubscriptionId`` once SOM activation completes
    (verified against ``GET /tmf-api/productOrderingManagement/v4/productOrder/{id}``).
    Scan every item; the first SUB-* wins. Falls back to a few legacy
    aliases (subscriptionId / subscription_id, top-level keys) so
    older envelope shapes and the unit-test fakes both resolve.
    Returns ``None`` if the field isn't there yet — the poll route
    treats that as "still in progress" and retriggers.
    """
    if not isinstance(order, dict):
        return None
    for key in _SUB_ID_KEYS:
        v = order.get(key)
        if isinstance(v, str) and v.startswith("SUB-"):
            return v
    items = order.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in _SUB_ID_KEYS:
                v = item.get(key)
                if isinstance(v, str) and v.startswith("SUB-"):
                    return v
    return None


def _extract_activation_code(order: dict[str, Any]) -> str | None:
    """Optional best-effort lift of the LPA activation code off the order."""
    if not isinstance(order, dict):
        return None
    for key in ("activationCode", "activation_code", "lpa"):
        v = order.get(key)
        if isinstance(v, str):
            return v
    items = order.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("activationCode", "activation_code", "lpa"):
                v = item.get(key)
                if isinstance(v, str):
                    return v
    return None
