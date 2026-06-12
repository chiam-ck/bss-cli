"""PaymentService — orchestration for charge attempts."""

from __future__ import annotations

from decimal import Decimal

import structlog
from bss_clock import now as clock_now
from bss_models import PaymentAttempt
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.domain.tokenizer import TokenizerAdapter
from app.events import publisher
from app.policies import payment as pay_policies
from app.policies.base import PolicyViolation
from app.repositories.payment_attempt_repo import PaymentAttemptRepository
from app.repositories.payment_method_repo import PaymentMethodRepository

log = structlog.get_logger()


class PaymentService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        attempt_repo: PaymentAttemptRepository,
        pm_repo: PaymentMethodRepository,
        tokenizer: TokenizerAdapter,
    ) -> None:
        self._session = session
        self._attempt_repo = attempt_repo
        self._pm_repo = pm_repo
        self._tokenizer = tokenizer

    async def charge(
        self,
        *,
        customer_id: str,
        payment_method_id: str,
        amount: Decimal,
        currency: str = "SGD",
        purpose: str,
    ) -> PaymentAttempt:
        ctx = auth_context.current()

        # --- Load method ---
        method = await self._pm_repo.get(payment_method_id)
        if method is None:
            raise PolicyViolation(
                rule="payment.charge.method_not_found",
                message=f"Payment method {payment_method_id} not found",
                context={"payment_method_id": payment_method_id},
            )

        # --- Policies ---
        pay_policies.check_method_active(method)
        pay_policies.check_positive_amount(amount)
        pay_policies.check_customer_matches_method(customer_id, method)
        # v0.16: lazy-fail cutover guard. A payment_method.token minted
        # under BSS_PAYMENT_PROVIDER=mock is unusable when the active
        # adapter is Stripe (and vice versa). Track 4's `bss payment
        # cutover` CLI is the proactive path; here we fail the charge
        # cleanly so the customer can re-add their card via the portal.
        pay_policies.check_token_provider_matches_active(
            method, type(self._tokenizer).__name__
        )

        # --- Execute charge via injected tokenizer adapter ---
        attempt_id = await self._attempt_repo.next_id()
        now = clock_now()

        # v0.16: per-attempt idempotency key. Same key on a BSS-restart
        # retry of the same attempt → Stripe dedupes; new attempt rows
        # get fresh keys (Track 4 tightens semantics).
        idempotency_key = f"ATT-{attempt_id}-r0"

        # Resolve provider-side customer ref from the payment.customer
        # cache. The cache is populated when the customer first adds a
        # card (Track 2's portal Elements flow calls ensure_customer
        # there with the real captured email). PaymentService at charge
        # time just reads. None for mock-mode customers; cus_* for
        # Stripe-mode customers who've added a card via Elements.
        customer_external_ref = await self._lookup_customer_external_ref(
            customer_id
        )

        charge_result = await self._tokenizer.charge(
            method.token,
            amount,
            currency,
            idempotency_key=idempotency_key,
            purpose=purpose,
            customer_external_ref=customer_external_ref,
        )

        attempt = PaymentAttempt(
            id=attempt_id,
            customer_id=customer_id,
            payment_method_id=payment_method_id,
            amount=amount,
            currency=currency,
            purpose=purpose,
            status=charge_result.status,
            gateway_ref=charge_result.gateway_ref,
            decline_reason=charge_result.reason,
            provider_call_id=charge_result.provider_call_id,
            decline_code=charge_result.decline_code,
            idempotency_key=idempotency_key,
            attempted_at=now,
            tenant_id=ctx.tenant,
        )
        await self._attempt_repo.create(attempt)

        # --- Event ---
        event_type = {
            "approved": "payment.charged",
            "declined": "payment.declined",
        }.get(charge_result.status, "payment.errored")

        # v0.16: payload carries provider_call_id + decline_code so
        # downstream consumers (renewal-flow listener, ops cockpit) can
        # join to integrations.external_call without a second lookup.
        await publisher.publish(
            self._session,
            event_type=event_type,
            aggregate_type="payment_attempt",
            aggregate_id=attempt_id,
            payload={
                "customer_id": customer_id,
                "payment_method_id": payment_method_id,
                "amount": str(amount),
                "currency": currency,
                "purpose": purpose,
                "status": charge_result.status,
                "gateway_ref": charge_result.gateway_ref,
                "provider_call_id": charge_result.provider_call_id,
                "decline_code": charge_result.decline_code,
            },
        )

        await self._session.commit()
        log.info(
            f"payment.{charge_result.status}",
            attempt_id=attempt_id,
            amount=str(amount),
            purpose=purpose,
        )
        return attempt

    async def _lookup_customer_external_ref(
        self, customer_id: str
    ) -> str | None:
        """Read cached cus_* (or equivalent) from payment.customer.

        Returns ``None`` if the BSS customer has never had a
        provider-side customer ref ensured. For mock-mode this is
        always None (mock charges accept None). For Stripe-mode the
        portal Elements flow (Track 2) populates the cache when the
        customer adds their first card; until then, charges against
        Stripe-mode raise cleanly via the adapter's missing-ref guard.
        """
        from bss_models import PaymentCustomer
        from sqlalchemy import select

        row = await self._session.execute(
            select(PaymentCustomer).where(PaymentCustomer.id == customer_id)
        )
        cached = row.scalar_one_or_none()
        return cached.customer_external_ref if cached else None

    async def get_attempt(self, attempt_id: str) -> PaymentAttempt | None:
        return await self._attempt_repo.get(attempt_id)

    async def list_attempts(
        self,
        customer_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[PaymentAttempt]:
        return await self._attempt_repo.list_for_customer(
            customer_id, limit=limit, offset=offset
        )

    async def count_attempts(self, customer_id: str) -> int:
        return await self._attempt_repo.count_for_customer(customer_id)
