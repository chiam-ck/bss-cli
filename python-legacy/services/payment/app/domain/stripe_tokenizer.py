"""StripeTokenizerAdapter — real-provider implementation (v0.16).

Implements ``TokenizerAdapter`` against Stripe's REST API via the official
``stripe`` Python SDK. The SDK is sync, so every call wraps in
``asyncio.to_thread`` to avoid blocking the event loop.

Trust + privacy model:

- **PAN never touches BSS in production.** ``tokenize`` raises
  ``NotImplementedError`` loudly. Card numbers go directly from the
  customer's browser to Stripe via Stripe.js + Elements; BSS only ever
  sees the resulting ``pm_*`` id.
- **Webhook is secondary source of truth.** This adapter's synchronous
  charge response is the primary truth for ``payment_attempt.status``;
  the Track 3 webhook receiver reconciles afterward. A drift between
  the two emits ``payment.attempt_state_drift`` (Track 3 work).
- **Customer-attach is required.** Off-session ``confirm=True`` charges
  fail without an attached customer; ``charge`` raises ``ValueError``
  if ``customer_external_ref`` is missing.

Forensics:

- Every Stripe call records to ``integrations.external_call`` via the
  injected ``session_factory``. ``redacted_payload`` strips email +
  billing PII via ``bss_webhooks.redaction.redact_provider_payload``.
- ``provider_call_id`` is Stripe's own primary key (``pi_*`` for
  charges, ``cus_*`` for customers, ``pm_*`` for attaches). The
  ``ChargeResult.provider_call_id`` propagates this onto the
  ``payment_attempt`` row so ``bss external-calls`` can join.

Free-tier / sandbox affordance:

- ``BSS_PAYMENT_ALLOW_TEST_CARD_REUSE=true`` lets the same Stripe test
  ``pm_*`` re-attach to a different BSS customer (Stripe's test cards
  trip ``payment_method_already_attached`` on second-and-later attach
  in the same account). ``select_tokenizer`` enforces this only ever
  pairs with ``sk_test_*``; setting it against ``sk_live_*`` is refused
  at startup.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import stripe
import structlog
from bss_webhooks.redaction import redact_provider_payload
from sqlalchemy import select

from app.domain.tokenizer import ChargeResult, TokenizeResult

log = structlog.get_logger(__name__)

PROVIDER = "stripe"


@dataclass(frozen=True)
class StripeConfig:
    api_key: str
    publishable_key: str
    webhook_secret: str
    allow_test_card_reuse: bool = False


class StripeTokenizerAdapter:
    """Stripe-backed implementation of ``TokenizerAdapter``.

    ``session_factory`` is the per-request async sessionmaker that gives
    the adapter a DB session for caching ``cus_*`` refs and writing
    ``integrations.external_call`` rows. Mirrors the v0.15 DiditKycAdapter
    pattern.
    """

    def __init__(
        self,
        *,
        config: StripeConfig,
        session_factory,  # async sessionmaker[AsyncSession]
    ) -> None:
        self._cfg = config
        self._session_factory = session_factory

    # ── charge ────────────────────────────────────────────────────────

    async def charge(
        self,
        token: str,
        amount: Decimal,
        currency: str,
        *,
        idempotency_key: str,
        purpose: str,
        customer_external_ref: str | None,
    ) -> ChargeResult:
        if amount <= 0:
            raise ValueError("amount must be positive")
        if not customer_external_ref:
            raise ValueError(
                "StripeTokenizerAdapter.charge requires customer_external_ref; "
                "off-session confirm=True needs an attached Stripe customer"
            )

        # Stripe expects the smallest-currency-unit integer (cents for
        # SGD/USD). Decimal('10.00') → 1000.
        amount_minor = int((amount * 100).to_integral_value())

        started = time.monotonic()
        try:
            pi = await asyncio.to_thread(
                stripe.PaymentIntent.create,
                api_key=self._cfg.api_key,
                amount=amount_minor,
                currency=currency.lower(),
                customer=customer_external_ref,
                payment_method=token,
                off_session=True,
                confirm=True,
                idempotency_key=idempotency_key,
                metadata={"bss_purpose": purpose},
            )
        except stripe.error.CardError as exc:
            # Card was declined at the issuer — recorded as a normal
            # decline, not an error. Stripe attaches the failed
            # PaymentIntent + Charge to the exception payload so we
            # can capture provider_call_id + decline_code.
            latency_ms = int((time.monotonic() - started) * 1000)
            payment_intent = (
                getattr(exc, "payment_intent", None)
                or (exc.error and getattr(exc.error, "payment_intent", None))
                or {}
            )
            pi_id = (
                payment_intent.get("id") if isinstance(payment_intent, dict)
                else getattr(payment_intent, "id", None)
            ) or "pi_unknown"
            decline_code = (
                getattr(exc, "code", None)
                or (exc.error and getattr(exc.error, "code", None))
                or "card_declined"
            )
            reason = (
                getattr(exc, "user_message", None)
                or str(exc)
                or "card declined"
            )
            await self._record_external_call(
                operation="charge",
                aggregate_id=idempotency_key,
                success=False,
                latency_ms=latency_ms,
                provider_call_id=pi_id,
                error_code=decline_code,
                error_message=reason,
                redacted_payload={"declined": True, "decline_code": decline_code},
            )
            log.info(
                "stripe.charge.declined",
                payment_intent=pi_id,
                decline_code=decline_code,
                idempotency_key=idempotency_key,
                latency_ms=latency_ms,
            )
            return ChargeResult(
                status="declined",
                gateway_ref=pi_id,
                reason=reason,
                provider_call_id=pi_id,
                decline_code=decline_code,
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        pi_dict = pi.to_dict() if hasattr(pi, "to_dict") else dict(pi)
        await self._record_external_call(
            operation="charge",
            aggregate_id=idempotency_key,
            success=True,
            latency_ms=latency_ms,
            provider_call_id=pi_dict.get("id"),
            redacted_payload=redact_provider_payload(
                provider=PROVIDER, body=pi_dict
            ),
        )
        log.info(
            "stripe.charge.succeeded",
            payment_intent=pi_dict.get("id"),
            status=pi_dict.get("status"),
            idempotency_key=idempotency_key,
            latency_ms=latency_ms,
        )
        # PaymentIntent.status='succeeded' = charge committed; anything
        # else under a synchronous confirm=True is unexpected (would
        # mean 3DS or other action is required, which off-session can't
        # handle).
        if pi_dict.get("status") == "succeeded":
            return ChargeResult(
                status="approved",
                gateway_ref=pi_dict["id"],
                reason=None,
                provider_call_id=pi_dict["id"],
                decline_code=None,
            )
        # Non-succeeded sync result → treat as errored so the
        # PaymentService keeps the row in a recoverable state. The
        # Track 3 webhook will reconcile if the PI ever lands.
        return ChargeResult(
            status="errored",
            gateway_ref=pi_dict["id"],
            reason=f"unexpected sync status {pi_dict.get('status')!r} on off-session confirm",
            provider_call_id=pi_dict["id"],
            decline_code=None,
        )

    # ── tokenize (forbidden in prod) ──────────────────────────────────

    async def tokenize(
        self,
        card_number: str,
        exp_month: int,
        exp_year: int,
        cvv: str,
    ) -> TokenizeResult:
        raise NotImplementedError(
            "server-side tokenization is forbidden in production; "
            "the portal uses Stripe.js + Elements client-side and "
            "BSS only receives the resulting pm_* id"
        )

    # ── attach ────────────────────────────────────────────────────────

    async def attach_payment_method_to_customer(
        self,
        *,
        payment_method_id: str,
        customer_id: str,
    ) -> None:
        started = time.monotonic()
        try:
            await asyncio.to_thread(
                stripe.PaymentMethod.attach,
                payment_method_id,
                api_key=self._cfg.api_key,
                customer=customer_id,
            )
        except stripe.error.InvalidRequestError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            # Sandbox-test affordance: when Stripe says "already
            # attached to a different customer" and the operator opted
            # in to test-card reuse, detach from the prior customer
            # then re-attach to the requested one. select_tokenizer has
            # already enforced this only pairs with sk_test_*.
            code = (exc.error and getattr(exc.error, "code", None)) or ""
            if (
                self._cfg.allow_test_card_reuse
                and code == "payment_method_already_attached"
            ):
                log.warning(
                    "stripe.test_card_relink",
                    payment_method=payment_method_id,
                    target_customer=customer_id,
                )
                await asyncio.to_thread(
                    stripe.PaymentMethod.detach,
                    payment_method_id,
                    api_key=self._cfg.api_key,
                )
                await asyncio.to_thread(
                    stripe.PaymentMethod.attach,
                    payment_method_id,
                    api_key=self._cfg.api_key,
                    customer=customer_id,
                )
                latency_ms = int((time.monotonic() - started) * 1000)
                await self._record_external_call(
                    operation="attach_test_relink",
                    aggregate_id=payment_method_id,
                    success=True,
                    latency_ms=latency_ms,
                    provider_call_id=payment_method_id,
                )
                return
            await self._record_external_call(
                operation="attach",
                aggregate_id=payment_method_id,
                success=False,
                latency_ms=latency_ms,
                provider_call_id=payment_method_id,
                error_code=code,
                error_message=str(exc),
            )
            raise

        latency_ms = int((time.monotonic() - started) * 1000)
        await self._record_external_call(
            operation="attach",
            aggregate_id=payment_method_id,
            success=True,
            latency_ms=latency_ms,
            provider_call_id=payment_method_id,
        )

    # ── retrieve_payment_method (v0.16 — for last4/brand fetch) ──────

    async def retrieve_payment_method_card(
        self, payment_method_id: str
    ) -> dict[str, str | int | None]:
        """Fetch card details (last4 / brand / exp) for a pm_*.

        Used by PaymentMethodService.register_method when the portal
        sent placeholder values (Track 2 redo: portal sends pm_id only,
        the canonical card metadata stays in Stripe). Returns a dict
        with keys: last4, brand, exp_month, exp_year. Raises
        stripe.StripeError on transport failure.
        """
        pm = await asyncio.to_thread(
            stripe.PaymentMethod.retrieve,
            payment_method_id,
            api_key=self._cfg.api_key,
        )
        pm_dict = pm.to_dict() if hasattr(pm, "to_dict") else dict(pm)
        card = pm_dict.get("card") or {}
        return {
            "last4": card.get("last4") or "",
            "brand": card.get("brand") or "card",
            "exp_month": card.get("exp_month"),
            "exp_year": card.get("exp_year"),
        }

    # ── ensure_customer ───────────────────────────────────────────────

    async def ensure_customer(
        self,
        *,
        bss_customer_id: str,
        email: str,
    ) -> str:
        # Cache check: if we already have a cus_* for this BSS customer
        # in payment.customer, return it without hitting Stripe.
        from bss_models.payment import PaymentCustomer  # type: ignore[attr-defined]

        async with self._session_factory() as db:
            row = await db.execute(
                select(PaymentCustomer).where(
                    PaymentCustomer.id == bss_customer_id,
                    PaymentCustomer.customer_external_ref_provider == PROVIDER,
                )
            )
            existing = row.scalar_one_or_none()
            if existing and existing.customer_external_ref:
                return existing.customer_external_ref

        started = time.monotonic()
        cust = await asyncio.to_thread(
            stripe.Customer.create,
            api_key=self._cfg.api_key,
            email=email,
            metadata={"bss_customer_id": bss_customer_id},
            idempotency_key=f"ensure_customer_{bss_customer_id}",
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        cus_dict = cust.to_dict() if hasattr(cust, "to_dict") else dict(cust)
        cus_id = cus_dict["id"]

        await self._record_external_call(
            operation="ensure_customer",
            aggregate_id=bss_customer_id,
            success=True,
            latency_ms=latency_ms,
            provider_call_id=cus_id,
            redacted_payload=redact_provider_payload(
                provider=PROVIDER, body=cus_dict
            ),
        )

        # Persist the cache row.
        async with self._session_factory() as db:
            db.add(
                PaymentCustomer(
                    id=bss_customer_id,
                    customer_external_ref=cus_id,
                    customer_external_ref_provider=PROVIDER,
                )
            )
            await db.commit()

        log.info(
            "stripe.ensure_customer",
            bss_customer_id=bss_customer_id,
            customer_external_ref=cus_id,
            latency_ms=latency_ms,
        )
        return cus_id

    # ── private ───────────────────────────────────────────────────────

    async def _record_external_call(
        self,
        *,
        operation: str,
        aggregate_id: str | None,
        success: bool,
        latency_ms: int,
        provider_call_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        redacted_payload: dict[str, Any] | None = None,
    ) -> None:
        from bss_models.integrations import ExternalCall

        async with self._session_factory() as db:
            db.add(
                ExternalCall(
                    provider=PROVIDER,
                    operation=operation,
                    aggregate_type=(
                        "payment_attempt" if operation == "charge"
                        else "payment_method" if operation in ("attach", "attach_test_relink")
                        else "customer" if operation == "ensure_customer"
                        else None
                    ),
                    aggregate_id=aggregate_id,
                    success=success,
                    latency_ms=latency_ms,
                    provider_call_id=provider_call_id,
                    error_code=error_code,
                    error_message=error_message,
                    redacted_payload=redacted_payload,
                )
            )
            await db.commit()
