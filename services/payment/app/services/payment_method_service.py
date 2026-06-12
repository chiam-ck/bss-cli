"""PaymentMethodService — orchestration for payment method lifecycle."""

from __future__ import annotations

import structlog
from bss_clients import CRMClient
from bss_models import PaymentMethod
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.events import publisher
from app.policies import payment_method as pm_policies
from app.repositories.payment_method_repo import PaymentMethodRepository

log = structlog.get_logger()


class PaymentMethodService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        pm_repo: PaymentMethodRepository,
        crm_client: CRMClient,
        tokenizer=None,  # v0.16 — TokenizerAdapter; None = mock-only deployment
    ) -> None:
        self._session = session
        self._pm_repo = pm_repo
        self._crm_client = crm_client
        self._tokenizer = tokenizer

    async def register_method(
        self,
        *,
        customer_id: str,
        type_: str,
        tokenization_provider: str,
        provider_token: str,
        brand: str,
        last4: str,
        exp_month: int,
        exp_year: int,
        country: str | None = None,
    ) -> PaymentMethod:
        ctx = auth_context.current()

        # --- Policies ---
        customer = await pm_policies.check_customer_exists(
            customer_id, self._crm_client
        )
        pm_policies.check_customer_active_or_pending(customer)

        # v0.16: Stripe-mode pm_* don't carry exp_month/exp_year on the
        # portal payload — the data lives on Stripe's PaymentMethod
        # object. Skip the local expiry check for stripe rows; Stripe
        # blocks expired cards itself at charge time.
        if tokenization_provider != "stripe":
            pm_policies.check_card_not_expired(exp_month, exp_year)
        await pm_policies.check_at_most_n_methods(customer_id, self._pm_repo)

        # --- Create ---
        pm_id = await self._pm_repo.next_id()

        # First method for a customer becomes the default
        existing_count = await self._pm_repo.count_active_for_customer(customer_id)
        is_default = existing_count == 0

        # v0.16: Stripe path — ensure_customer (cached in
        # payment.customer) + attach the pm_* to the customer BEFORE
        # we persist the row. If Stripe rejects either call, the row
        # never lands and the portal's failure path can render cleanly.
        # The actual last4/brand are also fetched from Stripe and
        # written onto the row (the portal sent empty strings).
        token_provider = (
            "stripe" if tokenization_provider == "stripe" else "mock"
        )
        if token_provider == "stripe":
            if self._tokenizer is None:
                from app.policies.base import PolicyViolation
                raise PolicyViolation(
                    rule="payment.method.add.no_tokenizer",
                    message=(
                        "stripe payment_method registration requires a "
                        "configured TokenizerAdapter on the payment service "
                        "(BSS_PAYMENT_PROVIDER=stripe + the four stripe "
                        "env vars; see select_tokenizer)."
                    ),
                    context={"customer_id": customer_id},
                )
            customer_email = (
                customer.get("contactMedium", [{}])[0].get("emailAddress")
                if isinstance(customer.get("contactMedium"), list)
                else customer.get("email")
            ) or f"{customer_id}@bss-cli.local"
            cus_external_ref = await self._tokenizer.ensure_customer(
                bss_customer_id=customer_id,
                email=customer_email,
            )
            await self._tokenizer.attach_payment_method_to_customer(
                payment_method_id=provider_token,
                customer_id=cus_external_ref,
            )
            # v0.16 Track 2 redo — fetch the real card details from
            # Stripe so the BSS row carries the actual last4/brand
            # (instead of the bss-clients placeholder "0000"/"card").
            # The portal sends empty strings; we fetch authoritative
            # values from Stripe's PaymentMethod object here. Best-
            # effort: if Stripe is briefly unreachable, fall back to
            # safe defaults so the row still lands.
            try:
                card_details = (
                    await self._tokenizer.retrieve_payment_method_card(
                        provider_token
                    )
                )
                last4 = card_details.get("last4") or last4 or "stripe"
                brand = card_details.get("brand") or brand or "card"
                exp_month = (
                    card_details.get("exp_month") or exp_month or 12
                )
                exp_year = card_details.get("exp_year") or exp_year or 2099
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "payment_method.stripe_card_details_fetch_failed",
                    payment_method_id=provider_token,
                    error=str(exc),
                )
                # Fall back to placeholders so the row still lands.
                if not exp_month:
                    exp_month = 12
                if not exp_year:
                    exp_year = 2099
                if not last4:
                    last4 = "stripe"
                if not brand:
                    brand = "card"

        pm = PaymentMethod(
            id=pm_id,
            customer_id=customer_id,
            type=type_,
            token=provider_token,
            token_provider=token_provider,
            last4=last4,
            brand=brand,
            exp_month=exp_month,
            exp_year=exp_year,
            is_default=is_default,
            status="active",
            tenant_id=ctx.tenant,
        )
        await self._pm_repo.create(pm)

        # --- Event ---
        await publisher.publish(
            self._session,
            event_type="payment_method.added",
            aggregate_type="payment_method",
            aggregate_id=pm_id,
            payload={
                "customer_id": customer_id,
                "brand": brand,
                "last4": last4,
                "tokenization_provider": tokenization_provider,
                "token_provider": token_provider,
            },
        )

        await self._session.commit()
        log.info(
            "payment_method.registered",
            pm_id=pm_id,
            customer_id=customer_id,
            token_provider=token_provider,
        )
        return pm

    async def get_method(self, pm_id: str) -> PaymentMethod | None:
        return await self._pm_repo.get(pm_id)

    async def list_methods(
        self, customer_id: str, *, include_removed: bool = False
    ) -> list[PaymentMethod]:
        return await self._pm_repo.list_for_customer(
            customer_id, include_removed=include_removed
        )

    async def set_default_method(self, pm_id: str) -> PaymentMethod:
        """Mark ``pm_id`` as the customer's default payment method.

        v0.10 — used by the portal's COF "Set default" CTA. The service
        owns the "exactly one default per customer" invariant: it
        clears any existing default for the same customer and sets the
        new one in a single transaction. Idempotent on a method that's
        already the default. Removed methods can't be made default.
        """
        from app.policies.base import PolicyViolation

        pm = await self._pm_repo.get(pm_id)
        if pm is None:
            raise PolicyViolation(
                rule="payment_method.set_default.not_found",
                message=f"Payment method {pm_id} not found",
                context={"payment_method_id": pm_id},
            )
        if pm.status == "removed":
            raise PolicyViolation(
                rule="payment_method.set_default.removed",
                message=f"Payment method {pm_id} has been removed",
                context={"payment_method_id": pm_id},
            )

        await self._pm_repo.set_default(pm.customer_id, pm_id)
        # Refresh in-memory view post-update so the response carries the new flag.
        pm.is_default = True

        await publisher.publish(
            self._session,
            event_type="payment_method.default_changed",
            aggregate_type="payment_method",
            aggregate_id=pm_id,
            payload={"customer_id": pm.customer_id, "last4": pm.last4},
        )

        await self._session.commit()
        log.info(
            "payment_method.default_changed", pm_id=pm_id, customer_id=pm.customer_id
        )
        return pm

    async def cutover_invalidate_mock_tokens(
        self, *, dry_run: bool = False
    ) -> dict[str, int | list[str]]:
        """v0.16 cutover — mark every mock-token row as expired.

        The proactive half of the v0.16 cutover contract (the lazy-fail
        half lives in ``payment.charge.token_provider_matches_active``).
        An operator switching ``BSS_PAYMENT_PROVIDER=mock → stripe``
        runs this to invalidate every saved card so the customer's next
        attempt to use one fails immediately and the portal's "add a
        new card" flow recovers — instead of silently failing on the
        next renewal charge weeks later.

        Returns a structured result with the affected ids + counts.
        Emits a ``payment_method.cutover_invalidated`` domain event per
        row so the v0.14 Resend email-template flow can notify each
        customer ("please update your payment method").
        """
        from bss_models import PaymentMethod as PM
        from sqlalchemy import select

        rows = (
            await self._session.execute(
                select(PM).where(
                    PM.token_provider == "mock",
                    PM.status == "active",
                )
            )
        ).scalars().all()

        affected_ids = [pm.id for pm in rows]
        result = {
            "candidate_count": len(affected_ids),
            "candidate_ids": affected_ids,
            "invalidated_count": 0,
            "invalidated_ids": [],
        }
        if dry_run or not affected_ids:
            log.info(
                "payment_method.cutover_invalidated",
                dry_run=dry_run,
                count=len(affected_ids),
            )
            return result

        # Mark rows + emit one event per row (the email-template flow
        # joins customer_id → email; we don't try to do the join here
        # so this stays tenant-agnostic).
        for pm in rows:
            pm.status = "expired"
            await publisher.publish(
                self._session,
                event_type="payment_method.cutover_invalidated",
                aggregate_type="payment_method",
                aggregate_id=pm.id,
                payload={
                    "customer_id": pm.customer_id,
                    "last4": pm.last4,
                    "brand": pm.brand,
                    "token_provider": pm.token_provider,
                    "reason": "operator_cutover",
                },
            )

        await self._session.commit()
        result["invalidated_count"] = len(affected_ids)
        result["invalidated_ids"] = affected_ids
        log.info(
            "payment_method.cutover_invalidated",
            count=len(affected_ids),
            dry_run=False,
        )
        return result

    async def remove_method(self, pm_id: str) -> PaymentMethod:
        pm = await self._pm_repo.get(pm_id)
        if pm is None:
            from app.policies.base import PolicyViolation

            raise PolicyViolation(
                rule="payment_method.remove.not_found",
                message=f"Payment method {pm_id} not found",
                context={"payment_method_id": pm_id},
            )

        if pm.status == "removed":
            from app.policies.base import PolicyViolation

            raise PolicyViolation(
                rule="payment_method.remove.already_removed",
                message=f"Payment method {pm_id} is already removed",
                context={"payment_method_id": pm_id},
            )

        # --- Policy: not last if active subscription (STUB in Phase 5) ---
        await pm_policies.check_not_last_if_active_subscription(
            pm.customer_id, self._pm_repo
        )

        pm.status = "removed"
        await self._pm_repo.update(pm)

        # --- Event ---
        await publisher.publish(
            self._session,
            event_type="payment_method.removed",
            aggregate_type="payment_method",
            aggregate_id=pm_id,
            payload={"customer_id": pm.customer_id, "last4": pm.last4},
        )

        await self._session.commit()
        log.info("payment_method.removed", pm_id=pm_id)
        return pm
