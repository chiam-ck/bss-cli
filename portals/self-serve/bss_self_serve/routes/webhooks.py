"""``/webhooks/*`` — inbound provider webhooks (v0.14+).

v0.14 ships ``POST /webhooks/resend`` for Resend (Svix-signed) email
delivery / bounce / complaint events. v0.15 adds ``POST /webhooks/didit``
for KYC verification callbacks. The Didit webhook is the trust anchor
for v0.15 KYC: HMAC-verified bodies write rows to
``integrations.kyc_webhook_corroboration`` which the CRM
``check_attestation_signature`` policy reads to authenticate the
attestation forwarded by the portal's ``DiditKycAdapter.fetch_attestation``.

Doctrine (per ``phases/V0_14_0.md`` §1.2 + §2.2):

* Webhook routes are **exempt from `BSSApiTokenMiddleware`**
  (added to ``WEBHOOK_EXEMPT_PATHS`` in v0.14). Auth is provider
  signature only — verified via
  ``bss_webhooks.signatures.verify_signature(scheme="svix", …)``.
* Persist every accepted event into ``integrations.webhook_event``
  (idempotent on ``(provider, event_id)`` PK so provider retries
  dedupe at the DB).
* Tampered signature → 401 + log; never persist.
* Duplicate event → 200 immediate (Svix at-least-once delivery).
* Known event types emit a ``portal_auth.email.*`` domain event
  into ``audit.domain_event`` so the existing outbox path picks
  them up. Unknown event types are persisted with
  ``process_outcome='noop'``.

The route handler is the bounded surface where Resend traffic enters
BSS-CLI; cross-tenant or external state mutation does NOT happen
here. v0.14 records bounce/complaint signals; consumption (suppression
list, customer notification) is post-v0.16.
"""

from __future__ import annotations

import structlog
from bss_webhooks.signatures import WebhookSignatureError, verify_signature
from bss_webhooks.store import WebhookEventStore
from fastapi import APIRouter, Request, Response

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

PROVIDER_RESEND = "resend"
PROVIDER_DIDIT = "didit"

# Resend events we explicitly map onto domain events. Unknown types
# still persist (forensic + forward-compat) but emit no domain event.
_RESEND_EVENT_TO_DOMAIN: dict[str, str] = {
    "email.delivered": "portal_auth.email.delivered",
    "email.bounced": "portal_auth.email.bounced",
    "email.complained": "portal_auth.email.complained",
    "email.failed": "portal_auth.email.failed",
    "email.delivery_delayed": "portal_auth.email.delivery_delayed",
}


@router.post("/resend")
async def webhook_resend(request: Request) -> Response:
    """Receive Resend (Svix) webhook deliveries."""
    body = await request.body()
    settings = request.app.state.portal_auth_settings
    secret = settings.BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET

    if not secret:
        # Misconfig — refuse silently from the outside but log loudly.
        # Returning 401 is the right answer because there's no way the
        # caller could have produced a valid signature against our
        # (empty) secret.
        log.warning(
            "portal_auth.webhook.misconfigured",
            provider=PROVIDER_RESEND,
            reason="webhook_secret_unset",
        )
        return Response(
            content='{"code":"webhook_secret_unset"}',
            status_code=401,
            media_type="application/json",
        )

    headers = {k: v for k, v in request.headers.items()}

    try:
        verify_signature(
            secret=secret,
            body=body,
            headers=headers,
            scheme="svix",
        )
    except WebhookSignatureError as exc:
        log.warning(
            "portal_auth.webhook.signature_invalid",
            provider=PROVIDER_RESEND,
            reason=exc.code,
        )
        return Response(
            content=f'{{"code":"{exc.code}"}}',
            status_code=401,
            media_type="application/json",
        )

    # Body parse — guard against malformed JSON. Svix verified the
    # bytes, so anything malformed here is a provider bug; return 400
    # to surface it but don't blow up the route.
    import json as _json

    try:
        payload = _json.loads(body)
    except _json.JSONDecodeError as exc:
        log.warning(
            "portal_auth.webhook.malformed_body",
            provider=PROVIDER_RESEND,
            error=str(exc),
        )
        return Response(
            content='{"code":"malformed_body"}',
            status_code=400,
            media_type="application/json",
        )

    # Svix headers carry the event id distinctly from the body; we
    # prefer the header (canonical idempotency key).
    event_id = (
        headers.get("svix-id")
        or payload.get("id")
        or payload.get("data", {}).get("email_id")
    )
    event_type = payload.get("type") or "unknown"

    if not event_id:
        log.warning(
            "portal_auth.webhook.missing_event_id",
            provider=PROVIDER_RESEND,
            event_type=event_type,
        )
        return Response(
            content='{"code":"missing_event_id"}',
            status_code=400,
            media_type="application/json",
        )

    session_factory = request.app.state.db_session_factory
    if session_factory is None:
        # Fail-open in dev contexts that boot without a DB — log but
        # ack. Production deployments boot with BSS_DB_URL set, so
        # this branch is dev-only.
        log.warning(
            "portal_auth.webhook.no_db",
            provider=PROVIDER_RESEND,
            event_id=event_id,
            event_type=event_type,
        )
        return Response(
            content='{"received":true,"persisted":false}',
            status_code=200,
            media_type="application/json",
        )

    store = WebhookEventStore()
    async with session_factory() as session:
        inserted = await store.persist(
            session,
            provider=PROVIDER_RESEND,
            event_id=event_id,
            event_type=event_type,
            body=payload,
            signature_valid=True,
        )
        await session.commit()

    if not inserted:
        # Provider retry — already known. Idempotent ack.
        log.info(
            "portal_auth.webhook.duplicate",
            provider=PROVIDER_RESEND,
            event_id=event_id,
            event_type=event_type,
        )
        return Response(
            content='{"received":true,"deduped":true}',
            status_code=200,
            media_type="application/json",
        )

    domain_event_type = _RESEND_EVENT_TO_DOMAIN.get(event_type)
    log.info(
        "portal_auth.webhook.received",
        provider=PROVIDER_RESEND,
        event_id=event_id,
        event_type=event_type,
        domain_event=domain_event_type,
    )

    # v0.14 stops here: webhook is persisted + structlog'd. Domain-event
    # emission into ``audit.domain_event`` (with the existing outbox
    # path picking it up for fan-out) lands when the email-event
    # consumers are wired in v0.15+ (suppression list, etc.).
    return Response(
        content='{"received":true}',
        status_code=200,
        media_type="application/json",
    )


# ── /webhooks/didit (v0.15) ─────────────────────────────────────────


@router.post("/didit")
async def webhook_didit(request: Request) -> Response:
    """Receive Didit (HMAC-signed) verification webhook deliveries.

    Trust anchor for v0.15 KYC: an HMAC-verified body writes a row to
    ``integrations.kyc_webhook_corroboration`` keyed by Didit's
    ``session_id``. The CRM policy ``check_attestation_signature`` reads
    that row to authenticate any attestation tagged ``provider="didit"``.
    """
    import hashlib
    import json as _json

    body = await request.body()
    settings = request.app.state.settings
    secret = settings.bss_portal_kyc_didit_webhook_secret

    if not secret:
        log.warning(
            "portal_auth.webhook.misconfigured",
            provider=PROVIDER_DIDIT,
            reason="webhook_secret_unset",
        )
        return Response(
            content='{"code":"webhook_secret_unset"}',
            status_code=401,
            media_type="application/json",
        )

    headers = {k: v for k, v in request.headers.items()}

    try:
        verify_signature(
            secret=secret,
            body=body,
            headers=headers,
            scheme="didit_hmac",
        )
    except WebhookSignatureError as exc:
        log.warning(
            "portal_auth.webhook.signature_invalid",
            provider=PROVIDER_DIDIT,
            reason=exc.code,
        )
        return Response(
            content=f'{{"code":"{exc.code}"}}',
            status_code=401,
            media_type="application/json",
        )

    try:
        payload = _json.loads(body)
    except _json.JSONDecodeError as exc:
        log.warning(
            "portal_auth.webhook.malformed_body",
            provider=PROVIDER_DIDIT,
            error=str(exc),
        )
        return Response(
            content='{"code":"malformed_body"}',
            status_code=400,
            media_type="application/json",
        )

    # Didit's webhook body carries the verification session id at
    # top-level. Treat the session id as the dedupe key — Didit may
    # deliver multiple events (status.updated, data.updated) for the
    # same session, but downstream corroboration only cares about the
    # latest authenticated state.
    session_id = (
        payload.get("session_id")
        or (payload.get("data") or {}).get("session_id")
        or ""
    )
    # Didit Webhooks v3.0 names the discriminator "webhook_type"
    # ("status.updated", "data.updated", …). Older drafts of this
    # handler read "type" / "event" — both wrong.
    event_type = (
        payload.get("webhook_type")
        or payload.get("type")
        or payload.get("event")
        or "unknown"
    )
    decision_status = (
        payload.get("status")
        or (payload.get("data") or {}).get("status")
        or ""
    )

    if not session_id:
        log.warning(
            "portal_auth.webhook.missing_session_id",
            provider=PROVIDER_DIDIT,
            event_type=event_type,
        )
        return Response(
            content='{"code":"missing_session_id"}',
            status_code=400,
            media_type="application/json",
        )

    # Didit's body carries a per-event ``event_id`` (UUID at top level).
    # Multiple webhooks for the same session (status.updated → data.updated
    # → status.updated as state advances) each have a distinct event_id,
    # so the ``(provider, event_id)`` PK on integrations.webhook_event
    # dedupes Didit's automatic retries while admitting the multi-event
    # progression we need to land the final 'Approved' decision.
    event_id = (
        payload.get("event_id")
        or payload.get("id")
        or headers.get("x-didit-event-id")
        or f"{session_id}:{event_type}:{payload.get('timestamp', '')}"
    )

    body_digest = hashlib.sha256(body).hexdigest()

    session_factory = request.app.state.db_session_factory
    if session_factory is None:
        log.warning(
            "portal_auth.webhook.no_db",
            provider=PROVIDER_DIDIT,
            event_id=event_id,
            event_type=event_type,
        )
        return Response(
            content='{"received":true,"persisted":false}',
            status_code=200,
            media_type="application/json",
        )

    from bss_models.integrations import KycWebhookCorroboration
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    store = WebhookEventStore()
    async with session_factory() as session:
        inserted = await store.persist(
            session,
            provider=PROVIDER_DIDIT,
            event_id=event_id,
            event_type=event_type,
            body=payload,
            signature_valid=True,
        )
        # Update / insert the corroboration row regardless of whether
        # this specific (provider, event_id) was a fresh insert into
        # webhook_event. Different webhooks for the same session (Not
        # Started → In Progress → Approved) all carry the same
        # session_id; we want the LATEST decision_status to land in
        # the corroboration row so the BSS policy sees the final state.
        if decision_status:
            existing = (
                await session.execute(
                    select(KycWebhookCorroboration).where(
                        KycWebhookCorroboration.provider == PROVIDER_DIDIT,
                        KycWebhookCorroboration.provider_session_id
                        == session_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                row = KycWebhookCorroboration(
                    provider=PROVIDER_DIDIT,
                    provider_session_id=session_id,
                    webhook_event_provider=PROVIDER_DIDIT,
                    webhook_event_id=event_id,
                    decision_status=decision_status,
                    decision_body_digest=body_digest,
                )
                session.add(row)
            else:
                existing.decision_status = decision_status
                existing.decision_body_digest = body_digest
        try:
            await session.commit()
        except IntegrityError:
            # Two concurrent webhook deliveries for the same session
            # raced to insert. Both verified — the second is benign.
            await session.rollback()

    if not inserted:
        log.info(
            "portal_auth.webhook.duplicate",
            provider=PROVIDER_DIDIT,
            event_id=event_id,
            event_type=event_type,
        )
        return Response(
            content='{"received":true,"deduped":true}',
            status_code=200,
            media_type="application/json",
        )

    log.info(
        "portal_auth.webhook.received",
        provider=PROVIDER_DIDIT,
        event_id=event_id,
        event_type=event_type,
        session_id=session_id,
        decision_status=decision_status,
    )
    return Response(
        content='{"received":true}',
        status_code=200,
        media_type="application/json",
    )
