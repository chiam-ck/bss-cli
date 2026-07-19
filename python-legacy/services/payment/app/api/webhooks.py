"""``POST /webhooks/stripe`` — Stripe webhook receiver (v0.16 Track 3).

Doctrine (per ``phases/V0_16_0.md`` §3 + the v0.15 retrospective):

- **Webhook is secondary source of truth** for ``payment_attempt.status``.
  The synchronous response from ``StripeTokenizerAdapter.charge`` is
  primary; webhooks reconcile and detect drift. A webhook saying
  success when the row says failed (or vice versa) is a
  ``payment.attempt_state_drift`` ops alert, NOT a state overwrite.
- **Diagnostic logging on signature_invalid from day 1** —
  ``candidate_headers`` (header keys + short values, never the secret)
  AND ``body_preview`` (first ~500 chars). v0.15 had to bolt this on
  reactively after three Didit deliveries failed silently with
  ``malformed_header``; v0.16 ships it ready.
- **Multi-webhook dedup-and-update.** Stripe emits multiple events per
  logical state transition (``charge.created`` → ``payment_intent.processing``
  → ``charge.succeeded``). The handler updates the receiving row on
  every webhook, last-write-wins. The v0.15 commit ``a649bba`` bug
  pattern (``if inserted and decision_status``) is explicitly avoided.
- **Chargebacks (``charge.dispute.created``) and out-of-band refunds
  (``charge.refunded``) are RECORD-ONLY.** Motto #1: no dunning, no
  collections, no auto-action on payment-collection-adjacent events.
  Operator handles via case workflow if they choose.
- **Webhook routes are exempt from ``BSSApiTokenMiddleware``** (v0.14
  added ``/webhooks/`` to ``WEBHOOK_EXEMPT_PATHS``). Auth is provider
  signature only.
"""

from __future__ import annotations

import json as _json

import structlog
from bss_clock import now as clock_now
from bss_models import PaymentAttempt
from bss_webhooks.signatures import WebhookSignatureError, verify_signature
from bss_webhooks.store import WebhookEventStore
from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import publisher

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

PROVIDER_STRIPE = "stripe"

# v0.16 spec §3.2 — terminal events the receiver explicitly routes.
# `payment_intent.created` / `processing`, `charge.created`, etc. are
# intermediate; we persist them (forensic) but do not act on them.
_TERMINAL_CHARGE_EVENTS = frozenset(
    {
        "charge.succeeded",
        "charge.failed",
        "payment_intent.payment_failed",
    }
)
_REFUND_EVENT = "charge.refunded"
_DISPUTE_EVENT = "charge.dispute.created"


# ── Diagnostic logging for signature_invalid (Track 3 day-1 requirement) ──


def _candidate_headers(headers: dict[str, str]) -> dict[str, str]:
    """Header keys + short values for diagnostic logs.

    NEVER includes the webhook secret. Truncates each value to 80 chars
    so the log line stays usable in tail / structlog / Loki.
    """
    safe: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if "secret" in lk or "token" in lk or "authorization" in lk:
            safe[k] = "[redacted]"
        else:
            safe[k] = v[:80] + ("…" if len(v) > 80 else "")
    return safe


def _body_preview(body: bytes, *, limit: int = 500) -> str:
    try:
        s = body.decode("utf-8", errors="replace")
    except Exception:
        return f"<{len(body)} bytes; not utf-8>"
    return s[:limit] + ("…" if len(s) > limit else "")


# ── /webhooks/stripe ─────────────────────────────────────────────────


@router.post("/stripe")
async def webhook_stripe(request: Request) -> Response:
    """Receive Stripe webhook deliveries.

    Verifies via ``bss_webhooks.signatures(scheme="stripe", …)``
    (Track 0 confirmed this matches Stripe's wire format exactly).
    Persists every accepted event to ``integrations.webhook_event``
    (idempotent on ``(provider, event_id)``). Routes terminal events
    onto ``payment_attempt`` reconciliation + domain event emission.
    """
    body = await request.body()
    settings = request.app.state.settings
    secret = getattr(settings, "payment_stripe_webhook_secret", "")

    if not secret:
        # Mock-mode or misconfig — refuse with diagnostic log. select_tokenizer
        # already fails-fast at startup when stripe-mode is selected
        # without this secret, so reaching here means BSS_PAYMENT_PROVIDER=mock
        # got a Stripe webhook (probably a leftover dashboard endpoint).
        log.warning(
            "payment.webhook.misconfigured",
            provider=PROVIDER_STRIPE,
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
            scheme="stripe",
        )
    except WebhookSignatureError as exc:
        # v0.16 day-1 doctrine: diagnostic logs on every reject so we
        # don't have to bolt this on after a wave of silent 401s.
        log.warning(
            "payment.webhook.signature_invalid",
            provider=PROVIDER_STRIPE,
            reason=exc.code,
            detail=str(exc),
            candidate_headers=_candidate_headers(headers),
            body_preview=_body_preview(body),
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
            "payment.webhook.malformed_body",
            provider=PROVIDER_STRIPE,
            error=str(exc),
            body_preview=_body_preview(body),
        )
        return Response(
            content='{"code":"malformed_body"}',
            status_code=400,
            media_type="application/json",
        )

    # Stripe envelope: id (per-delivery, unique), type, data.object,
    # created, livemode, request. Verified against Track 0 fixture.
    event_id = payload.get("id")
    event_type = payload.get("type") or "unknown"
    data_object = (payload.get("data") or {}).get("object") or {}

    if not event_id:
        log.warning(
            "payment.webhook.missing_event_id",
            provider=PROVIDER_STRIPE,
            event_type=event_type,
        )
        return Response(
            content='{"code":"missing_event_id"}',
            status_code=400,
            media_type="application/json",
        )

    session_factory = request.app.state.session_factory
    store = WebhookEventStore()

    async with session_factory() as session:
        inserted = await store.persist(
            session,
            provider=PROVIDER_STRIPE,
            event_id=event_id,
            event_type=event_type,
            body=payload,
            signature_valid=True,
        )

        outcome: str
        domain_event_emitted: str | None = None

        if not inserted:
            # Provider retry of the same event — already persisted.
            # Idempotent ack; no re-routing.
            outcome = "deduped"
            await session.commit()
            log.info(
                "payment.webhook.duplicate",
                provider=PROVIDER_STRIPE,
                event_id=event_id,
                event_type=event_type,
            )
            return Response(
                content='{"received":true,"deduped":true}',
                status_code=200,
                media_type="application/json",
            )

        # ── Routing ──────────────────────────────────────────────
        if event_type in _TERMINAL_CHARGE_EVENTS:
            outcome, domain_event_emitted = await _route_terminal_charge(
                session,
                event_type=event_type,
                data_object=data_object,
            )
        elif event_type == _REFUND_EVENT:
            outcome, domain_event_emitted = await _route_refund(
                session,
                data_object=data_object,
            )
        elif event_type == _DISPUTE_EVENT:
            outcome, domain_event_emitted = await _route_dispute(
                session,
                data_object=data_object,
            )
        else:
            # Intermediate / unknown event — persist + ack, no action.
            # Stripe's multi-event cascade per logical transition lands
            # plenty of these (payment_intent.created, charge.created,
            # …); they're forensic value only.
            outcome = "noop"

        await store.mark_processed(
            session,
            provider=PROVIDER_STRIPE,
            event_id=event_id,
            outcome=outcome,
            processed_at=clock_now(),
        )
        await session.commit()

    log.info(
        "payment.webhook.received",
        provider=PROVIDER_STRIPE,
        event_id=event_id,
        event_type=event_type,
        outcome=outcome,
        domain_event=domain_event_emitted,
    )
    return Response(
        content='{"received":true}',
        status_code=200,
        media_type="application/json",
    )


# ── Routers ──────────────────────────────────────────────────────────


async def _route_terminal_charge(
    session: AsyncSession,
    *,
    event_type: str,
    data_object: dict,
) -> tuple[str, str | None]:
    """Reconcile payment_attempt against the Stripe terminal event.

    Webhook is secondary source of truth — if the row already matches,
    record noop. If the row contradicts, emit drift event but do NOT
    overwrite (the synchronous charge() response wins).
    """
    # data.object on charge.* is a Charge; on payment_intent.payment_failed
    # it's a PaymentIntent. Both carry the pi_* — Charge.payment_intent
    # and PaymentIntent.id. We stored the pi_* as provider_call_id on the
    # attempt row.
    pi_id = data_object.get("payment_intent") or data_object.get("id")
    if not pi_id:
        return ("noop", None)

    expected_status = (
        "approved" if event_type == "charge.succeeded" else "declined"
    )

    result = await session.execute(
        select(PaymentAttempt).where(PaymentAttempt.provider_call_id == pi_id)
    )
    attempt = result.scalar_one_or_none()
    if attempt is None:
        # Webhook arrived for an attempt BSS doesn't have a row for —
        # could happen if the row was created against a different
        # tenant or the synchronous create raced a webhook. Record as
        # noop; ops can join via integrations.webhook_event.
        return ("noop", None)

    if attempt.status == expected_status:
        return ("reconciled", None)

    # Drift: webhook says X but row says Y. Don't overwrite — emit an
    # ops event so someone can investigate. The synchronous charge()
    # response is canonical.
    await publisher.publish(
        session,
        event_type="payment.attempt_state_drift",
        aggregate_type="payment_attempt",
        aggregate_id=attempt.id,
        payload={
            "row_status": attempt.status,
            "webhook_status": expected_status,
            "stripe_event_type": event_type,
            "provider_call_id": pi_id,
        },
    )
    return ("drift", "payment.attempt_state_drift")


async def _route_refund(
    session: AsyncSession,
    *,
    data_object: dict,
) -> tuple[str, str | None]:
    """Out-of-band refund (Stripe dashboard). Record-only; motto #1.

    Emit ``payment.refunded`` with refund amount + reason. NO automatic
    balance adjustment — bundled-prepaid posture preserved.
    """
    pi_id = data_object.get("payment_intent") or data_object.get("id")
    amount_refunded = data_object.get("amount_refunded") or 0
    if not pi_id:
        return ("noop", None)

    attempt_id = None
    if pi_id:
        row = await session.execute(
            select(PaymentAttempt).where(
                PaymentAttempt.provider_call_id == pi_id
            )
        )
        a = row.scalar_one_or_none()
        if a is not None:
            attempt_id = a.id

    await publisher.publish(
        session,
        event_type="payment.refunded",
        aggregate_type="payment_attempt",
        aggregate_id=attempt_id or pi_id,
        payload={
            "provider_call_id": pi_id,
            # Stripe uses smallest currency unit (cents); BSS keeps it as-is
            # in the event so the cockpit / reporting layer can divide.
            "amount_refunded_minor": amount_refunded,
            "currency": data_object.get("currency"),
            "reason": data_object.get("refunds", {}).get("data", [{}])[0].get(
                "reason"
            ) if isinstance(data_object.get("refunds"), dict) else None,
        },
    )
    return ("reconciled", "payment.refunded")


async def _route_dispute(
    session: AsyncSession,
    *,
    data_object: dict,
) -> tuple[str, str | None]:
    """Chargeback. Record-only; motto #1.

    Emit ``payment.dispute_opened`` for the cockpit to surface. NO
    automatic case creation, NO service block — operator handles via
    case workflow if they choose. v0.16 trap #4.
    """
    charge_id = data_object.get("charge")
    pi_id = data_object.get("payment_intent")
    dispute_id = data_object.get("id")

    attempt_id = None
    if pi_id:
        row = await session.execute(
            select(PaymentAttempt).where(
                PaymentAttempt.provider_call_id == pi_id
            )
        )
        a = row.scalar_one_or_none()
        if a is not None:
            attempt_id = a.id

    await publisher.publish(
        session,
        event_type="payment.dispute_opened",
        aggregate_type="payment_attempt",
        aggregate_id=attempt_id or dispute_id or charge_id or "unknown",
        payload={
            "stripe_dispute_id": dispute_id,
            "stripe_charge_id": charge_id,
            "provider_call_id": pi_id,
            "amount_minor": data_object.get("amount"),
            "currency": data_object.get("currency"),
            "reason": data_object.get("reason"),
            "status": data_object.get("status"),
        },
    )
    return ("reconciled", "payment.dispute_opened")
