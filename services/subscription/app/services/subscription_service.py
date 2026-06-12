"""Subscription service — orchestration layer.

Router → Service → Policies → Repository → Event publisher.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import aio_pika
import structlog
from bss_clients import CatalogClient, CRMClient, InventoryClient, PaymentClient
from bss_clock import now as clock_now
from bss_models import apply_discount
from bss_models.subscription import (
    BundleBalance,
    Subscription,
    SubscriptionStateHistory,
    VasPurchase,
)
from bss_telemetry import semconv, tracer
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.bundle import (
    AllowanceSpec,
    BalanceSnapshot,
    add_allowance,
    consume,
    is_exhausted,
    reset_for_new_period,
)
from app.domain.state_machine import get_next_state, is_valid_transition
from app.events import publisher
from app.policies.base import PolicyViolation
from app.policies.plan_change import (
    check_admin_role,
    check_no_pending_change,
    check_not_same_offering,
    check_offering_sellable_now,
    check_subscription_active_or_pending_renewal,
    fetch_active_price_for_target,
)
from app.policies.subscription import (
    check_customer_exists,
    check_msisdn_and_esim_reserved,
    check_renew_allowed,
)
from app.policies.usage import check_roaming_balance_required
from app.policies.vas import check_not_terminated, check_vas_offering_sellable
from app.repositories.subscription_repo import SubscriptionRepository
from app.repositories.vas_repo import VasPurchaseRepository

log = structlog.get_logger()


def _initial_discount_remaining(
    discount_type: str | None, discount_periods_total: int | None
) -> int:
    """Discounted-periods counter AFTER the activation charge (period 1).

    No discount → 0. Perpetual (total = -1) → -1 (never decrements). Otherwise
    total - 1: single (total 1) → 0, multi N → N-1. Matches the plan's worked
    example ($25/20%/3 → remaining 2 after create).
    """
    if not discount_type or discount_periods_total is None:
        return 0
    if discount_periods_total < 0:
        return -1
    return max(discount_periods_total - 1, 0)


class SubscriptionService:
    def __init__(
        self,
        session: AsyncSession,
        repo: SubscriptionRepository,
        vas_repo: VasPurchaseRepository,
        crm_client: CRMClient,
        payment_client: PaymentClient,
        catalog_client: CatalogClient,
        inventory_client: InventoryClient,
    ):
        self._session = session
        self._repo = repo
        self._vas_repo = vas_repo
        self._crm = crm_client
        self._payment = payment_client
        self._catalog = catalog_client
        self._inventory = inventory_client

    async def create(
        self,
        *,
        customer_id: str,
        offering_id: str,
        msisdn: str,
        iccid: str,
        payment_method_id: str,
        price_snapshot: dict | None = None,
        commercial_order_id: str | None = None,
    ) -> Subscription:
        # v1.2 — idempotency: if a subscription already exists for this
        # commercial order, return it WITHOUT charging the card-on-file again.
        # This is the guard against MQ redelivery double-charging an activation
        # (the unique index on commercial_order_id is the backstop). Done before
        # any policy check or charge so a replay is a cheap read.
        if commercial_order_id is not None:
            existing = await self._repo.get_by_commercial_order(commercial_order_id)
            if existing is not None:
                log.info(
                    "subscription.create.idempotent_hit",
                    commercial_order_id=commercial_order_id,
                    subscription_id=existing.id,
                )
                return existing

        # Policy: customer exists and is active/pending
        await check_customer_exists(customer_id, self._crm)

        # Policy: MSISDN and eSIM are reserved
        await check_msisdn_and_esim_reserved(msisdn, iccid, self._inventory)

        # Fetch offering from catalog for allowances. Price snapshot is
        # provided by the COM/SOM event flow (v0.7); we still fetch the
        # offering for bundle allowances.
        offering = await self._catalog.get_offering(offering_id)

        if price_snapshot is not None:
            amount = Decimal(str(price_snapshot["priceAmount"]))
            currency = price_snapshot["priceCurrency"]
            offering_price_id = price_snapshot["priceOfferingPriceId"]
        else:
            # Legacy / direct create path — read price off the offering payload.
            prices = offering.get("productOfferingPrice", [])
            recurring = next((p for p in prices if p.get("priceType") == "recurring"), None)
            amount = (
                Decimal(str(recurring["price"]["taxIncludedAmount"]["value"]))
                if recurring
                else Decimal("0")
            )
            currency = (
                recurring["price"]["taxIncludedAmount"].get("unit", "SGD")
                if recurring
                else "SGD"
            )
            offering_price_id = recurring.get("id") if recurring else None
            if not offering_price_id:
                raise PolicyViolation(
                    rule="subscription.create.requires_active_price",
                    message=f"Offering {offering_id} has no recurring price row to snapshot",
                    context={"offering_id": offering_id},
                )

        # v1.1 — promo discount carried on the snapshot. `amount` stays the FULL
        # base (persisted as price_amount); the activation charge is the
        # effective price for period 1.
        discount_type = price_snapshot.get("discountType") if price_snapshot else None
        discount_value = (
            Decimal(str(price_snapshot["discountValue"]))
            if price_snapshot and price_snapshot.get("discountValue") is not None
            else None
        )
        discount_periods_total = (
            price_snapshot.get("discountPeriodsTotal") if price_snapshot else None
        )
        promo_code = price_snapshot.get("promoCode") if price_snapshot else None
        promo_offer_definition_id = (
            price_snapshot.get("promoOfferDefinitionId") if price_snapshot else None
        )
        charge_amount = (
            apply_discount(discount_type, discount_value, amount)
            if discount_type and discount_value is not None
            else amount
        )

        # Policy: payment must succeed
        payment_result = await self._payment.charge(
            customer_id=customer_id,
            payment_method_id=payment_method_id,
            amount=charge_amount,
            currency="SGD",
            purpose="activation",
        )
        if payment_result.get("status") != "approved":
            # Release inventory on payment failure
            try:
                await self._inventory.release_msisdn(msisdn)
            except Exception:
                log.warning("inventory.release_msisdn.failed", msisdn=msisdn)
            try:
                await self._inventory.recycle_esim(iccid)
            except Exception:
                log.warning("inventory.recycle_esim.failed", iccid=iccid[-4:])
            raise PolicyViolation(
                rule="subscription.create.requires_payment_success",
                message="Activation payment was declined",
                context={
                    "payment_status": payment_result.get("status"),
                    "decline_reason": payment_result.get("declineReason"),
                },
            )

        # Create subscription
        now = clock_now()
        period_end = now + timedelta(days=30)
        sub_id = await self._repo.next_id()

        sub = Subscription(
            id=sub_id,
            customer_id=customer_id,
            commercial_order_id=commercial_order_id,
            offering_id=offering_id,
            msisdn=msisdn,
            iccid=iccid,
            state="pending",
            price_amount=amount,
            price_currency=currency,
            price_offering_price_id=offering_price_id,
            # v1.1 — discount snapshot. remaining is set AFTER the period-1 charge.
            discount_type=discount_type,
            discount_value=discount_value,
            discount_periods_remaining=_initial_discount_remaining(
                discount_type, discount_periods_total
            ),
            promo_code=promo_code,
            promo_offer_definition_id=promo_offer_definition_id,
        )
        await self._repo.create(sub)

        # Initialize bundle balances from catalog allowances
        allowances = offering.get("bundleAllowance", [])
        for a in allowances:
            bal_id = f"{sub_id}-{a['allowanceType'].upper()}"
            balance = BundleBalance(
                id=bal_id,
                subscription_id=sub_id,
                allowance_type=a["allowanceType"],
                total=a["quantity"],
                consumed=0,
                unit=a["unit"],
                period_start=now,
                period_end=period_end,
            )
            await self._repo.add_balance(balance)

        # Assign inventory
        try:
            await self._inventory.assign_msisdn(msisdn)
        except Exception:
            log.warning("inventory.assign_msisdn.failed", msisdn=msisdn)
        try:
            await self._inventory.assign_msisdn_to_esim(iccid, msisdn)
        except Exception:
            log.warning("inventory.assign_esim.failed", iccid=iccid[-4:])

        # Transition: pending → active
        await self._transition(sub, "activate", reason="activation_payment_approved")
        sub.activated_at = now
        sub.current_period_start = now
        sub.current_period_end = period_end
        sub.next_renewal_at = period_end

        await publisher.publish(
            self._session,
            event_type="subscription.activated",
            aggregate_type="subscription",
            aggregate_id=sub_id,
            payload={
                "subscriptionId": sub_id,
                "customerId": customer_id,
                "offeringId": offering_id,
                "msisdn": msisdn,
                "iccid": iccid,
                "paymentAttemptId": payment_result.get("id", ""),
                "periodStart": now.isoformat(),
                "periodEnd": period_end.isoformat(),
                "amountCharged": str(charge_amount),
                "promoCode": promo_code,
            },
        )

        await self._session.commit()
        log.info(
            "subscription.created",
            subscription_id=sub_id,
            state="active",
            amount_charged=str(charge_amount),
            discount_type=discount_type,
        )

        # Reload to get balances
        sub = await self._repo.get(sub_id)
        return sub

    async def get(self, sub_id: str) -> Subscription | None:
        return await self._repo.get(sub_id)

    async def get_by_msisdn(self, msisdn: str) -> Subscription | None:
        return await self._repo.get_by_msisdn(msisdn)

    async def list_for_customer(self, customer_id: str) -> list[Subscription]:
        return await self._repo.list_for_customer(customer_id)

    async def get_balances(self, sub_id: str) -> list[BundleBalance]:
        return await self._repo.get_balances(sub_id)

    async def handle_usage_rated(
        self,
        *,
        subscription_id: str,
        allowance_type: str,
        consumed_quantity: int,
        usage_event_id: str,
        exchange: aio_pika.abc.AbstractExchange | None = None,
    ) -> None:
        """Process a `usage.rated` event.

        Concurrency: pessimistic lock via `SELECT ... FOR UPDATE` on the
        balance row so concurrent events for the same (subscription,
        allowance) serialize. See DECISIONS.md Phase 8 for why Option A
        was picked over optimistic locking or MQ partitioning.
        """
        sub = await self._repo.get(subscription_id)
        if not sub:
            log.warning(
                "usage.rated.subscription_not_found",
                subscription_id=subscription_id,
                usage_event_id=usage_event_id,
            )
            return

        # Belt-and-braces: Mediation rejects at ingress, but replays / races
        # can still deliver events for a non-active subscription. Drop them.
        if sub.state != "active":
            log.warning(
                "usage.rated.subscription_not_active",
                subscription_id=subscription_id,
                state=sub.state,
                usage_event_id=usage_event_id,
            )
            return

        target = await self._repo.get_balance_for_update(subscription_id, allowance_type)

        # v0.17 — roaming usage is policy-gated independently from home
        # data. A missing balance row OR an exhausted roaming balance
        # rejects the decrement; the subscription stays `active` (home
        # data unaffected). For non-roaming allowance types, the
        # existing "missing row" branch is a benign warning + skip.
        if allowance_type == "data_roaming":
            try:
                check_roaming_balance_required(
                    subscription_id=subscription_id,
                    balance=target,
                    consumed_quantity=consumed_quantity,
                )
            except PolicyViolation as exc:
                await publisher.publish(
                    self._session,
                    event_type="usage.rejected",
                    aggregate_type="subscription",
                    aggregate_id=subscription_id,
                    payload={
                        "subscriptionId": subscription_id,
                        "allowanceType": allowance_type,
                        "usageEventId": usage_event_id,
                        "reason": exc.rule,
                        **exc.context,
                    },
                    exchange=exchange,
                )
                await self._session.commit()
                log.warning(
                    "usage.rejected.roaming_balance_required",
                    subscription_id=subscription_id,
                    usage_event_id=usage_event_id,
                )
                return

        if target is None:
            log.warning(
                "usage.rated.allowance_not_on_subscription",
                subscription_id=subscription_id,
                allowance_type=allowance_type,
            )
            return

        snap = BalanceSnapshot(
            allowance_type=target.allowance_type,
            total=target.total,
            consumed=target.consumed,
            unit=target.unit,
        )
        result = consume(snap, consumed_quantity)
        target.consumed = result.consumed
        await self._repo.update_balance(target)

        all_balances = await self._repo.get_balances(subscription_id)
        snapshots = [
            BalanceSnapshot(
                allowance_type=b.allowance_type, total=b.total,
                consumed=b.consumed, unit=b.unit,
            )
            for b in all_balances
        ]

        if is_exhausted(snapshots):
            await publisher.publish(
                self._session,
                event_type="subscription.exhausted",
                aggregate_type="subscription",
                aggregate_id=subscription_id,
                payload={
                    "subscriptionId": subscription_id,
                    "allowanceType": allowance_type,
                    "consumed": result.consumed,
                    "total": result.total,
                    "triggeringUsageEventId": usage_event_id,
                },
                exchange=exchange,
            )
            await self._transition(sub, "exhaust", reason="primary_allowance_exhausted")
            await publisher.publish(
                self._session,
                event_type="subscription.blocked",
                aggregate_type="subscription",
                aggregate_id=subscription_id,
                payload={"subscriptionId": subscription_id, "reason": "exhausted"},
                exchange=exchange,
            )

        await self._session.commit()
        log.info(
            "usage.rated.applied",
            subscription_id=subscription_id,
            allowance_type=allowance_type,
            consumed_quantity=consumed_quantity,
            usage_event_id=usage_event_id,
            new_consumed=result.consumed,
            final_state=sub.state,
        )

    async def purchase_vas(
        self, sub_id: str, vas_offering_id: str
    ) -> Subscription:
        with tracer("bss-subscription").start_as_current_span(
            "subscription.purchase_vas"
        ) as span:
            span.set_attribute(semconv.BSS_SUBSCRIPTION_ID, sub_id)
            span.set_attribute(semconv.BSS_VAS_OFFERING_ID, vas_offering_id)

            sub = await self._repo.get(sub_id)
            if not sub:
                raise PolicyViolation(
                    rule="subscription.not_found",
                    message=f"Subscription {sub_id} not found",
                    context={"subscription_id": sub_id},
                )
            span.set_attribute(semconv.BSS_CUSTOMER_ID, sub.customer_id)
            span.set_attribute(semconv.BSS_SUBSCRIPTION_STATE, sub.state)

            # Policies
            check_not_terminated(sub.state)

            vas = await check_vas_offering_sellable(vas_offering_id, self._catalog)

            # Charge payment
            # Find default payment method for customer
            methods = await self._payment.list_methods(sub.customer_id)
            default_method = next((m for m in methods if m.get("isDefault")), None)
            if not default_method:
                default_method = methods[0] if methods else None
            if not default_method:
                raise PolicyViolation(
                    rule="subscription.vas_purchase.requires_active_cof",
                    message="No active payment method found",
                    context={"customer_id": sub.customer_id},
                )

            amount = Decimal(str(vas.get("priceAmount", 0)))
            payment_result = await self._payment.charge(
                customer_id=sub.customer_id,
                payment_method_id=default_method["id"],
                amount=amount,
                currency=vas.get("currency", "SGD"),
                purpose="vas",
            )
            if payment_result.get("status") != "approved":
                raise PolicyViolation(
                    rule="subscription.vas_purchase.requires_active_cof",
                    message="VAS payment was declined",
                    context={
                        "payment_status": payment_result.get("status"),
                        "decline_reason": payment_result.get("declineReason"),
                    },
                )

            # Record VAS purchase
            vas_id = f"{sub_id}-VAS-{uuid4().hex[:8].upper()}"
            now = clock_now()
            expiry_hours = vas.get("expiryHours")
            expires_at = now + timedelta(hours=expiry_hours) if expiry_hours else None
            allowance_qty = vas.get("allowanceQuantity", 0)
            allowance_type = vas.get("allowanceType", "data")

            purchase = VasPurchase(
                id=vas_id,
                subscription_id=sub_id,
                vas_offering_id=vas_offering_id,
                payment_attempt_id=payment_result.get("id"),
                applied_at=now,
                expires_at=expires_at,
                allowance_added=allowance_qty,
                allowance_type=allowance_type,
            )
            await self._vas_repo.create(purchase)

            # Add to balance. v0.17 — when no balance row exists for this
            # allowance_type yet, materialize one from the VAS spec. This
            # matters for `data_roaming` top-ups against subscriptions whose
            # plan has zero roaming included (BA_S_ROAM=0 still creates a
            # row, but pre-v0.17 subscriptions or future allowance types
            # may have no row at all). Generally correct fix: any future
            # allowance type works without a separate code path.
            balances = await self._repo.get_balances(sub_id)
            target = next((b for b in balances if b.allowance_type == allowance_type), None)
            if target is None and allowance_qty > 0:
                target = BundleBalance(
                    id=f"{sub_id}-{allowance_type.upper()}",
                    subscription_id=sub_id,
                    allowance_type=allowance_type,
                    total=allowance_qty,
                    consumed=0,
                    unit=vas.get("allowanceUnit", "mb"),
                    period_start=now,
                    period_end=sub.current_period_end,
                )
                await self._repo.add_balance(target)
            elif target and allowance_qty > 0 and target.total != -1:
                snap = BalanceSnapshot(
                    allowance_type=target.allowance_type,
                    total=target.total, consumed=target.consumed, unit=target.unit,
                )
                result = add_allowance(snap, allowance_qty)
                target.total = result.total
                await self._repo.update_balance(target)

            previous_state = sub.state

            await publisher.publish(
                self._session,
                event_type="subscription.vas_purchased",
                aggregate_type="subscription",
                aggregate_id=sub_id,
                payload={
                    "subscriptionId": sub_id,
                    "vasOfferingId": vas_offering_id,
                    "paymentAttemptId": payment_result.get("id", ""),
                    "allowanceType": allowance_type,
                    "allowanceAdded": allowance_qty,
                    "previousState": previous_state,
                },
            )

            # If blocked and top-up adds data → unblock
            if sub.state == "blocked":
                await self._transition(sub, "top_up", reason="vas_top_up")
                await publisher.publish(
                    self._session,
                    event_type="subscription.unblocked",
                    aggregate_type="subscription",
                    aggregate_id=sub_id,
                    payload={"subscriptionId": sub_id, "reason": "vas_top_up"},
                )
            elif sub.state == "active":
                # top_up on active → still active (self-transition)
                await self._transition(sub, "top_up", reason="vas_top_up")

            await self._session.commit()
            return await self._repo.get(sub_id)

    async def renew(self, sub_id: str) -> Subscription:
        """Renew a subscription, charging the price snapshot stored on the row.

        v0.7 doctrine — renewal **never** reads the catalog price. The
        snapshot (`subscription.price_amount` / `price_currency`) is the
        contract; catalog changes don't disturb existing customers. The
        catalog is still consulted for *bundle allowances* because we don't
        version those in v0.7 — see DECISIONS.md.
        """
        sub = await self._repo.get(sub_id)
        if not sub:
            raise PolicyViolation(
                rule="subscription.not_found",
                message=f"Subscription {sub_id} not found",
                context={"subscription_id": sub_id},
            )

        check_renew_allowed(sub.state)

        # ── Plan-change / price-migration pivot ────────────────────────
        # If pending fields are set and the effective date has arrived, the
        # customer is renewing onto a different plan (or a new price for the
        # same plan). The renewal flow below applies the switch atomically:
        # the new snapshot drives the charge, the new offering's allowances
        # reset the bundle, and on success the pending fields are cleared.
        now = clock_now()
        applying_pending = (
            sub.pending_offering_id is not None
            and sub.pending_effective_at is not None
            and sub.pending_effective_at <= now
        )

        if applying_pending:
            # Resolve the snapshot row directly — no time filter, the
            # snapshot remembers history even if the row is now retired.
            new_price = await self._catalog.get_offering_price(
                sub.pending_offering_price_id
            )
            amount = Decimal(str(new_price["price"]["taxIncludedAmount"]["value"]))
            currency = new_price["price"]["taxIncludedAmount"].get("unit", "SGD")
            offering_price_id = sub.pending_offering_price_id
            target_offering_id = sub.pending_offering_id
        else:
            # Snapshot drives the charge — never the catalog.
            amount = sub.price_amount
            currency = sub.price_currency
            offering_price_id = sub.price_offering_price_id
            target_offering_id = sub.offering_id

        # Allowances come from the *target* offering — for plan changes that's
        # the new plan, for vanilla renewals that's the current plan.
        offering = await self._catalog.get_offering(target_offering_id)

        # v1.1 — apply the promo discount to this renewal while the per-sub
        # counter is live (>0, or -1 perpetual). A pending plan change ends the
        # promo, so it always charges the full new price (cleared on success).
        applying_discount = (
            not applying_pending
            and sub.discount_type is not None
            and sub.discount_value is not None
            and sub.discount_periods_remaining != 0
        )
        charge_amount = (
            apply_discount(sub.discount_type, sub.discount_value, amount)
            if applying_discount
            else amount
        )

        # Find payment method
        methods = await self._payment.list_methods(sub.customer_id)
        default_method = next((m for m in methods if m.get("isDefault")), None)
        if not default_method:
            default_method = methods[0] if methods else None
        if not default_method:
            raise PolicyViolation(
                rule="subscription.renew.no_payment_method",
                message="No active payment method found for renewal",
                context={"customer_id": sub.customer_id},
            )

        payment_result = await self._payment.charge(
            customer_id=sub.customer_id,
            payment_method_id=default_method["id"],
            amount=charge_amount,
            currency=currency,
            purpose="renewal",
        )

        if payment_result.get("status") != "approved":
            # Renewal failed → block. If we were attempting a pending plan
            # change, the pending fields are intentionally **not cleared** —
            # operator could retry via top-up + manual renewal.
            await self._transition(sub, "renew_fail", reason="renewal_payment_declined")
            await publisher.publish(
                self._session,
                event_type="subscription.renew_failed",
                aggregate_type="subscription",
                aggregate_id=sub_id,
                payload={
                    "subscriptionId": sub_id,
                    "reason": "payment_declined",
                    "paymentAttemptId": payment_result.get("id", ""),
                },
            )
            if applying_pending:
                await publisher.publish(
                    self._session,
                    event_type="subscription.plan_change_payment_failed",
                    aggregate_type="subscription",
                    aggregate_id=sub_id,
                    payload={
                        "subscriptionId": sub_id,
                        "currentOfferingId": sub.offering_id,
                        "pendingOfferingId": sub.pending_offering_id,
                        "paymentAttemptId": payment_result.get("id", ""),
                    },
                )
            await publisher.publish(
                self._session,
                event_type="subscription.blocked",
                aggregate_type="subscription",
                aggregate_id=sub_id,
                payload={"subscriptionId": sub_id, "reason": "renew_failed"},
            )
            await self._session.commit()
            return await self._repo.get(sub_id)

        # Renewal succeeded — reset balances
        period_end = now + timedelta(days=30)
        allowances = offering.get("bundleAllowance", [])
        specs = [
            AllowanceSpec(a["allowanceType"], a["quantity"], a["unit"])
            for a in allowances
        ]
        new_snapshots = reset_for_new_period(specs)

        # Update existing balances. For plan changes, replace the row set
        # with whatever the new offering specifies — old allowance types
        # the new plan doesn't have are zeroed out so they no longer count.
        balances = await self._repo.get_balances(sub_id)
        existing_types = {b.allowance_type for b in balances}

        for bal in balances:
            matching = next(
                (s for s in new_snapshots if s.allowance_type == bal.allowance_type),
                None,
            )
            if matching:
                bal.total = matching.total
                bal.consumed = 0
                bal.period_start = now
                bal.period_end = period_end
                await self._repo.update_balance(bal)
            elif applying_pending:
                # Allowance type from old plan that's not in new plan: zero it.
                bal.total = 0
                bal.consumed = 0
                bal.period_start = now
                bal.period_end = period_end
                await self._repo.update_balance(bal)

        if applying_pending:
            for spec in new_snapshots:
                if spec.allowance_type in existing_types:
                    continue
                # New allowance type the previous plan didn't have — add it.
                bal_id = f"{sub_id}-{spec.allowance_type.upper()}"
                await self._repo.add_balance(
                    BundleBalance(
                        id=bal_id,
                        subscription_id=sub_id,
                        allowance_type=spec.allowance_type,
                        total=spec.total,
                        consumed=0,
                        unit=spec.unit,
                        period_start=now,
                        period_end=period_end,
                    )
                )

        # ── Apply pending pivot: swap offering + snapshot, clear pending ──
        was_price_migration = False
        previous_offering_id = sub.offering_id
        if applying_pending:
            was_price_migration = sub.pending_offering_id == sub.offering_id
            sub.offering_id = sub.pending_offering_id
            sub.price_amount = amount
            sub.price_currency = currency
            sub.price_offering_price_id = offering_price_id
            sub.pending_offering_id = None
            sub.pending_offering_price_id = None
            sub.pending_effective_at = None
            # v1.1 — a plan change ends the promo (DECISIONS 2026-05-21).
            sub.discount_type = None
            sub.discount_value = None
            sub.discount_periods_remaining = 0
            sub.promo_code = None
            sub.promo_offer_definition_id = None
        elif applying_discount and sub.discount_periods_remaining > 0:
            # Consumed one discounted renewal period; perpetual (-1) never decrements.
            sub.discount_periods_remaining -= 1

        await self._transition(sub, "renew", reason="renewal_payment_approved")
        sub.current_period_start = now
        sub.current_period_end = period_end
        sub.next_renewal_at = period_end

        await publisher.publish(
            self._session,
            event_type="subscription.renewed",
            aggregate_type="subscription",
            aggregate_id=sub_id,
            payload={
                "subscriptionId": sub_id,
                "offeringId": sub.offering_id,
                "paymentAttemptId": payment_result.get("id", ""),
                "periodStart": now.isoformat(),
                "periodEnd": period_end.isoformat(),
                "priceSnapshot": {
                    "priceAmount": str(amount),
                    "priceCurrency": currency,
                    "priceOfferingPriceId": offering_price_id,
                },
                "amountCharged": str(charge_amount),
                "discountApplied": applying_discount,
            },
        )

        if applying_pending:
            event_name = (
                "subscription.price_migrated"
                if was_price_migration
                else "subscription.plan_changed"
            )
            await publisher.publish(
                self._session,
                event_type=event_name,
                aggregate_type="subscription",
                aggregate_id=sub_id,
                payload={
                    "subscriptionId": sub_id,
                    "previousOfferingId": previous_offering_id,
                    "newOfferingId": sub.offering_id,
                    "newPriceAmount": str(amount),
                    "newPriceCurrency": currency,
                    "newPriceOfferingPriceId": offering_price_id,
                },
            )

        await self._session.commit()
        return await self._repo.get(sub_id)

    async def schedule_plan_change(
        self, sub_id: str, new_offering_id: str
    ) -> Subscription:
        """Record a pending offering switch on the subscription.

        Applied at the next renewal — see `renew()`. No state change here;
        pending fields are advisory until renewal-time pivot. Idempotent
        only insofar as the policy bars a second pending change; to
        replace, the customer must cancel first.
        """
        sub = await self._repo.get(sub_id)
        if not sub:
            raise PolicyViolation(
                rule="subscription.not_found",
                message=f"Subscription {sub_id} not found",
                context={"subscription_id": sub_id},
            )

        check_subscription_active_or_pending_renewal(sub.state)
        check_not_same_offering(sub.offering_id, new_offering_id)
        check_no_pending_change(sub.pending_offering_id)
        await check_offering_sellable_now(self._catalog, new_offering_id)
        new_price = await fetch_active_price_for_target(self._catalog, new_offering_id)

        sub.pending_offering_id = new_offering_id
        sub.pending_offering_price_id = new_price["id"]
        sub.pending_effective_at = sub.next_renewal_at

        await publisher.publish(
            self._session,
            event_type="subscription.plan_change_scheduled",
            aggregate_type="subscription",
            aggregate_id=sub_id,
            payload={
                "subscriptionId": sub_id,
                "currentOfferingId": sub.offering_id,
                "newOfferingId": new_offering_id,
                "newPriceAmount": str(
                    new_price["price"]["taxIncludedAmount"]["value"]
                ),
                "newPriceCurrency": new_price["price"]["taxIncludedAmount"].get(
                    "unit", "SGD"
                ),
                "effectiveAt": (
                    sub.pending_effective_at.isoformat()
                    if sub.pending_effective_at
                    else None
                ),
            },
        )

        await self._session.commit()
        log.info(
            "subscription.plan_change.scheduled",
            subscription_id=sub_id,
            new_offering_id=new_offering_id,
        )
        return await self._repo.get(sub_id)

    async def cancel_pending_plan_change(self, sub_id: str) -> Subscription:
        """Clear pending plan-change fields. No-op if nothing is pending."""
        sub = await self._repo.get(sub_id)
        if not sub:
            raise PolicyViolation(
                rule="subscription.not_found",
                message=f"Subscription {sub_id} not found",
                context={"subscription_id": sub_id},
            )

        if sub.pending_offering_id is None:
            log.info("subscription.plan_change.cancel.noop", subscription_id=sub_id)
            return sub

        previous_pending = {
            "offeringId": sub.pending_offering_id,
            "offeringPriceId": sub.pending_offering_price_id,
            "effectiveAt": (
                sub.pending_effective_at.isoformat()
                if sub.pending_effective_at
                else None
            ),
        }
        sub.pending_offering_id = None
        sub.pending_offering_price_id = None
        sub.pending_effective_at = None

        await publisher.publish(
            self._session,
            event_type="subscription.plan_change_cancelled",
            aggregate_type="subscription",
            aggregate_id=sub_id,
            payload={
                "subscriptionId": sub_id,
                "previousPending": previous_pending,
            },
        )

        await self._session.commit()
        log.info("subscription.plan_change.cancelled", subscription_id=sub_id)
        return await self._repo.get(sub_id)

    async def migrate_subscriptions_to_price(
        self,
        *,
        filter: dict,
        new_price_id: str,
        effective_from: datetime,
        notice_days: int,
        initiated_by: str,
    ) -> dict:
        """Operator-initiated price migration with notice.

        Each affected subscription gets its own pending fields and its own
        per-subscription event — no batch UPDATE that loses the audit trail.
        Subscriptions terminated during the notice window simply skip the
        renewal-time pivot. Admin role required.

        Args:
            filter: Currently only ``{"offering_id": "PLAN_X"}`` is supported.
            new_price_id: Target ``product_offering_price.id``. Must belong
                to the subscription's current offering.
            effective_from: Earliest moment the new price may be applied.
                ``effective_from + notice_days`` becomes ``pending_effective_at``.
            notice_days: Regulatory notice (Singapore: 30 days for upward
                price moves).
            initiated_by: Operator identity for audit + downstream
                notifications.

        Returns:
            ``{count: N, subscriptionIds: [...]}``.
        """
        check_admin_role()

        offering_filter = filter.get("offering_id")
        if not offering_filter:
            raise PolicyViolation(
                rule="subscription.migrate_price.unsupported_filter",
                message="filter must include an 'offering_id' key (only filter v0.7 supports)",
                context={"filter": filter},
            )

        # Resolve the target price row and validate the offering match.
        new_price = await self._catalog.get_offering_price(new_price_id)
        if new_price is None:
            raise PolicyViolation(
                rule="subscription.migrate_price.unknown_price",
                message=f"Price {new_price_id} not found in catalog",
                context={"new_price_id": new_price_id},
            )
        # Catalog returns the price's offering id under the camelCase shape we use.
        # The TMF mapping doesn't expose offering_id; resolve via offering lookup.
        offering = await self._catalog.get_offering(offering_filter)
        offering_prices = {p["id"] for p in offering.get("productOfferingPrice", [])}
        if new_price_id not in offering_prices:
            raise PolicyViolation(
                rule="subscription.migrate_price.price_not_on_offering",
                message=(
                    f"Price {new_price_id} does not belong to offering {offering_filter}"
                ),
                context={
                    "new_price_id": new_price_id,
                    "offering_id": offering_filter,
                },
            )

        new_amount = Decimal(str(new_price["price"]["taxIncludedAmount"]["value"]))
        new_currency = new_price["price"]["taxIncludedAmount"].get("unit", "SGD")
        effective_at = effective_from + timedelta(days=notice_days)

        affected = await self._repo.list_active_for_offering(offering_filter)
        affected_ids: list[str] = []

        for sub in affected:
            old_amount = sub.price_amount
            sub.pending_offering_id = sub.offering_id  # same plan
            sub.pending_offering_price_id = new_price_id
            sub.pending_effective_at = effective_at

            await publisher.publish(
                self._session,
                event_type="subscription.price_migration_scheduled",
                aggregate_type="subscription",
                aggregate_id=sub.id,
                payload={
                    "subscriptionId": sub.id,
                    "offeringId": sub.offering_id,
                    "oldAmount": str(old_amount),
                    "newAmount": str(new_amount),
                    "newCurrency": new_currency,
                    "effectiveAt": effective_at.isoformat(),
                    "initiatedBy": initiated_by,
                },
            )
            await publisher.publish(
                self._session,
                event_type="notification.requested",
                aggregate_type="subscription",
                aggregate_id=sub.id,
                payload={
                    "customerId": sub.customer_id,
                    "channel": "email",
                    "template": "price_migration_notice",
                    "templateArgs": {
                        "subscriptionId": sub.id,
                        "offeringId": sub.offering_id,
                        "oldAmount": str(old_amount),
                        "newAmount": str(new_amount),
                        "currency": new_currency,
                        "effectiveAt": effective_at.isoformat(),
                    },
                },
            )
            affected_ids.append(sub.id)

        await self._session.commit()
        log.info(
            "subscription.price_migration.scheduled",
            offering_id=offering_filter,
            new_price_id=new_price_id,
            count=len(affected_ids),
            initiated_by=initiated_by,
        )
        return {"count": len(affected_ids), "subscriptionIds": affected_ids}

    async def terminate(
        self,
        sub_id: str,
        *,
        reason: str = "customer_requested",
        release_inventory: bool = True,
    ) -> Subscription:
        """Terminate a subscription — release MSISDN + eSIM, transition state.

        ``reason`` is forensic only (carried into the state-history row + the
        ``subscription.terminated`` event payload). It does not affect
        eligibility — callers that pass an arbitrary string don't bypass the
        ``is_valid_transition`` check. v0.10 portal cancel route uses
        ``"customer_requested"``; CSR-initiated cancels would use
        ``"csr_initiated"``; admin pruning would use ``"admin_cleanup"``.

        v0.17 — ``release_inventory`` defaults True (preserves existing
        behaviour). The MNP port-out flow passes False because it has
        already flipped the donor MSISDN to terminal ``ported_out``;
        re-releasing would either no-op (policy rejects terminal) or
        break the doctrine that ``ported_out`` is non-recyclable. eSIM
        recycle still runs in both cases.
        """
        sub = await self._repo.get(sub_id)
        if not sub:
            raise PolicyViolation(
                rule="subscription.not_found",
                message=f"Subscription {sub_id} not found",
                context={"subscription_id": sub_id},
            )

        if not is_valid_transition(sub.state, "terminate"):
            raise PolicyViolation(
                rule="subscription.terminate.invalid_state",
                message=f"Cannot terminate subscription in state '{sub.state}'",
                context={"state": sub.state},
            )

        now = clock_now()

        # Release inventory (skipped for port-out — see docstring)
        if release_inventory:
            try:
                await self._inventory.release_msisdn(sub.msisdn)
            except Exception:
                log.warning("inventory.release_msisdn.failed", msisdn=sub.msisdn)

        try:
            await self._inventory.recycle_esim(sub.iccid)
        except Exception:
            log.warning("inventory.recycle_esim.failed", iccid=sub.iccid[-4:])

        await self._transition(sub, "terminate", reason=reason)
        sub.terminated_at = now

        await publisher.publish(
            self._session,
            event_type="subscription.terminated",
            aggregate_type="subscription",
            aggregate_id=sub_id,
            payload={
                "subscriptionId": sub_id,
                "customerId": sub.customer_id,
                "msisdn": sub.msisdn,
                "iccid": sub.iccid,
                "terminatedAt": now.isoformat(),
                "reason": reason,
            },
        )

        await self._session.commit()
        log.info("subscription.terminated", subscription_id=sub_id, reason=reason)
        return await self._repo.get(sub_id)

    async def _transition(
        self, sub: Subscription, trigger: str, *, reason: str = ""
    ) -> None:
        if not is_valid_transition(sub.state, trigger):
            raise PolicyViolation(
                rule="subscription.transition.invalid",
                message=f"Cannot trigger '{trigger}' from state '{sub.state}'",
                context={"state": sub.state, "trigger": trigger},
            )
        from_state = sub.state
        to_state = get_next_state(sub.state, trigger)
        sub.state = to_state
        sub.state_reason = reason

        entry = SubscriptionStateHistory(
            subscription_id=sub.id,
            from_state=from_state,
            to_state=to_state,
            changed_by="system",
            reason=reason,
        )
        await self._repo.add_state_history(entry)
