"""Order orchestration service — calls policies, not repositories directly."""

from decimal import Decimal

import structlog
from bss_clients import ClientError, PolicyViolationFromServer
from bss_clock import now as clock_now
from bss_models.order_mgmt import OrderItem, ProductOrder
from bss_telemetry import semconv, tracer
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.publisher import publish
from app.policies.base import PolicyViolation
from app.policies.order import (
    check_cancel_allowed_after_som,
    check_customer_exists,
    check_customer_has_payment_method,
    check_offering_currently_sellable,
    check_order_transition,
)
from app.repositories.order_repo import OrderRepository

log = structlog.get_logger()


class OrderService:
    def __init__(
        self,
        session: AsyncSession,
        repo: OrderRepository,
        crm_client,
        catalog_client,
        payment_client,
        som_client,
        subscription_client,
        exchange,
        loyalty_client=None,
    ):
        self._session = session
        self._repo = repo
        self._crm = crm_client
        self._catalog = catalog_client
        self._payment = payment_client
        self._som = som_client
        self._subscription = subscription_client
        self._loyalty = loyalty_client
        self._exchange = exchange

    async def create_order(
        self,
        *,
        customer_id: str,
        offering_id: str,
        msisdn_preference: str | None = None,
        notes: str | None = None,
        discount_code: str | None = None,
        skip_assigned_offer: bool = False,
    ) -> ProductOrder:
        """Create a new order (acknowledged) and stamp the price snapshot.

        v0.7 — the active price row at this moment is captured on the order
        item so renewal will charge what the customer signed up for, even if
        the catalog row is later retired or repriced.

        v1.1 — resolve a promo discount and stamp it as INTENT on the order item
        (the entitlement is not consumed until activation, Flow 2/3). A typed
        ``discount_code`` is validated; otherwise the customer's assigned offers
        are checked for one applicable to this offering. An invalid/absent promo
        never blocks the order — it simply proceeds at full price.
        """
        await check_customer_exists(customer_id, self._crm)
        _, active_price = await check_offering_currently_sellable(offering_id, self._catalog)
        await check_customer_has_payment_method(customer_id, self._payment)

        # Snapshot — the offering's active price at order-creation moment.
        price_amount = Decimal(str(active_price["price"]["taxIncludedAmount"]["value"]))
        price_currency = active_price["price"]["taxIncludedAmount"].get("unit", "SGD")
        price_offering_price_id = active_price["id"]

        discount = await self._resolve_discount(
            customer_id=customer_id,
            offering_id=offering_id,
            discount_code=discount_code,
            skip_assigned_offer=skip_assigned_offer,
        )

        order_id = await self._repo.next_order_id()
        item_id = await self._repo.next_item_id()

        order = ProductOrder(
            id=order_id,
            customer_id=customer_id,
            state="acknowledged",
            order_date=clock_now(),
            msisdn_preference=msisdn_preference,
            notes=notes,
        )
        item = OrderItem(
            id=item_id,
            order_id=order_id,
            action="add",
            offering_id=offering_id,
            state="acknowledged",
            price_amount=price_amount,
            price_currency=price_currency,
            price_offering_price_id=price_offering_price_id,
            # v1.1 — discount INTENT (not yet claimed). promo_offer_id is set
            # now only for an assigned offer (it already exists); a typed code's
            # offer is created at claim time in handle_service_order_completed.
            discount_code=discount.get("discount_code"),
            promo_offer_definition_id=discount.get("offer_definition_id"),
            discount_type=discount.get("discount_type"),
            discount_value=discount.get("discount_value"),
            discount_periods_total=discount.get("discount_periods_total"),
            promo_offer_id=discount.get("promo_offer_id"),
        )
        self._session.add(item)
        await self._repo.create(order)

        await self._repo.add_state_history(order_id, None, "acknowledged", reason="order created")

        await publish(
            self._session,
            event_type="order.acknowledged",
            aggregate_type="ProductOrder",
            aggregate_id=order_id,
            payload={
                "commercialOrderId": order_id,
                "customerId": customer_id,
                "offeringId": offering_id,
                "priceSnapshot": {
                    "priceAmount": str(price_amount),
                    "priceCurrency": price_currency,
                    "priceOfferingPriceId": price_offering_price_id,
                },
            },
            exchange=self._exchange,
        )

        await self._session.commit()
        return await self._repo.get(order_id)

    async def _resolve_discount(
        self,
        *,
        customer_id: str,
        offering_id: str,
        discount_code: str | None,
        skip_assigned_offer: bool = False,
    ) -> dict:
        """Resolve a promo to stamp as INTENT on the order item.

        Precedence: a *valid* typed ``discount_code`` wins. If the typed code is
        invalid (a typo), we fall back to the customer's cheapest applicable
        *assigned* offer rather than dropping to full price — a typo shouldn't
        cost the customer their auto-offer. ``skip_assigned_offer`` (the funnel
        opt-out) suppresses that fallback. Promos do NOT stack: exactly one
        applies, composed onto the base price. Returns the discount fields or
        ``{}``. Catalog owns all promo logic; a transport hiccup degrades to "no
        discount" (never blocks the order).
        """
        res: dict | None = None
        code_to_claim: str | None = None
        try:
            if discount_code:
                typed = await self._catalog.validate_promo(
                    code=discount_code, offering=offering_id, customer_id=customer_id
                )
                if typed.get("valid"):
                    res, code_to_claim = typed, discount_code  # typed code wins
                else:
                    log.info(
                        "order.promo.code_invalid_fallback_to_eligible",
                        code=discount_code,
                        reason=typed.get("reason"),
                    )
            # No code, or an invalid code → the customer's best eligible targeted
            # promo auto-applies (unless they opted out). v1.1.1: this returns a
            # CODE too, so the consume path is identical to a typed code.
            if res is None and not skip_assigned_offer:
                eligible = await self._catalog.resolve_eligible_promo(
                    customer_id=customer_id, offering=offering_id
                )
                if eligible.get("valid"):
                    res, code_to_claim = eligible, eligible.get("code")
        except ClientError:
            log.warning("order.promo.resolve_failed", code=discount_code, exc_info=True)
            return {}

        if res is None:
            return {}

        raw_value = res.get("discountValue")
        return {
            # the code to claim at activation — typed or the targeted promo's code
            "discount_code": code_to_claim,
            "offer_definition_id": res.get("offerDefinitionId"),
            "discount_type": res.get("discountType"),
            "discount_value": Decimal(raw_value) if raw_value is not None else None,
            "discount_periods_total": res.get("discountPeriodsTotal"),
            # v1.3.0 — targeted promos are pre-paired in loyalty at assign time,
            # so the loyalty offer id is known at create. Public typed codes
            # carry NULL here (mint-and-claimed by code at activation). At
            # claim-at-activation COM uses ``advance_to_claimed`` if this is set,
            # else falls back to ``claim_offer(source=promo_code)``.
            "promo_offer_id": res.get("loyaltyOfferId"),
        }

    async def _claim_entitlement(self, order: ProductOrder, item) -> str | None:
        """Consume the promo entitlement at activation (the gate).

        v1.3.0 — TWO paths, picked by whether the offer was pre-paired at
        assign time (targeted) vs. minted now from a typed code (public):

        * **Targeted** (``item.promo_offer_id`` is set): the loyalty offer was
          ``offer.issue``-d at ``bss promo assign`` time. Move it to ``claimed``
          via ``advance_to_claimed`` — the path retired in v1.1.1 is back, but
          ONLY for this lane (visibility/audit win on assignment).
        * **Public typed** (no ``promo_offer_id`` set): mint-and-claim by code
          via ``offer.claim(source=promo_code)`` (the v1.1.1 path, unchanged).
        * **Backstop**: a v1.3.0 targeted row where ``offer.issue`` degraded at
          assign time also has no ``promo_offer_id`` — it transparently falls
          through to claim-by-code, exact same behaviour as a pre-v1.3.0 row.

        loyalty dedupes idempotency on (actor, key) WITHOUT the tool name, so
        each op gets a distinct ``{order_id}:<op>`` key. Returns the loyalty
        offer id to redeem/revoke, or None when there's no promo.
        """
        if item is None or not item.discount_type or not item.discount_code:
            return None
        if self._loyalty is None:
            return None
        # Targeted path: pre-paired offer → advance it.
        if item.promo_offer_id:
            await self._loyalty.advance_offer_to_claimed(
                offer_id=item.promo_offer_id,
                order_ref=order.id,
                idempotency_key=f"{order.id}:claim",
            )
            return item.promo_offer_id
        # Public typed path: mint-and-claim by code.
        result = await self._loyalty.claim_offer(
            customer_id=order.customer_id,
            source={"type": "promo_code", "code": item.discount_code},
            idempotency_key=f"{order.id}:claim",
        )
        return result.get("offer_id")

    async def _redeem_entitlement(self, offer_id: str, order_id: str) -> None:
        """Finalize the entitlement after a successful activation. Best-effort:
        the subscription already exists, so a redeem hiccup is logged, not fatal."""
        try:
            await self._loyalty.redeem_offer(
                offer_id=offer_id, order_ref=order_id, idempotency_key=f"{order_id}:redeem"
            )
        except ClientError:
            log.warning("order.promo.redeem_failed", offer_id=offer_id, exc_info=True)

    async def _revoke_entitlement(self, offer_id: str, order_id: str) -> None:
        """Release the entitlement when activation fails (payment decline).
        Best-effort — a revoke hiccup is logged; loyalty's TTL/expiry is the
        backstop. Reason maps to loyalty's ``order_cancelled`` RevokeReason."""
        try:
            await self._loyalty.revoke_offer(
                offer_id=offer_id,
                reason="order_cancelled",
                idempotency_key=f"{order_id}:revoke",
                order_ref=order_id,
            )
        except ClientError:
            log.warning("order.promo.revoke_failed", offer_id=offer_id, exc_info=True)

    async def submit_order(self, order_id: str) -> ProductOrder:
        """Submit an order (acknowledged -> in_progress), publish to MQ."""
        order = await self._repo.get(order_id)
        if not order:
            raise PolicyViolation(
                rule="order.not_found",
                message=f"Order {order_id} not found",
                context={"order_id": order_id},
            )

        check_order_transition(order.state, "in_progress")

        # Get payment method for event payload
        payment_methods = await self._payment.list_methods(order.customer_id)
        payment_method_id = payment_methods[0]["id"] if payment_methods else ""

        item = order.items[0] if order.items else None
        offering_id = item.offering_id if item else ""

        # Forward the price snapshot stamped at create_order time.
        price_snapshot = None
        if item is not None and item.price_amount is not None:
            price_snapshot = {
                "priceAmount": str(item.price_amount),
                "priceCurrency": item.price_currency,
                "priceOfferingPriceId": item.price_offering_price_id,
            }

        old_state = order.state
        order.state = "in_progress"
        await self._repo.add_state_history(order_id, old_state, "in_progress", reason="order submitted")
        await self._repo.update(order)

        # Publish order.in_progress event to MQ
        payload = {
            "commercialOrderId": order_id,
            "customerId": order.customer_id,
            "offeringId": offering_id,
            "msisdnPreference": order.msisdn_preference,
            "paymentMethodId": payment_method_id,
        }
        if price_snapshot is not None:
            payload["priceSnapshot"] = price_snapshot

        await publish(
            self._session,
            event_type="order.in_progress",
            aggregate_type="ProductOrder",
            aggregate_id=order_id,
            payload=payload,
            exchange=self._exchange,
        )

        await self._session.commit()
        return await self._repo.get(order_id)

    async def cancel_order(self, order_id: str) -> ProductOrder:
        """Cancel an order — acknowledged always OK, in_progress only if SOM hasn't started."""
        order = await self._repo.get(order_id)
        if not order:
            raise PolicyViolation(
                rule="order.not_found",
                message=f"Order {order_id} not found",
                context={"order_id": order_id},
            )

        check_order_transition(order.state, "cancelled")

        if order.state == "in_progress":
            await check_cancel_allowed_after_som(order_id, self._som)

        old_state = order.state
        order.state = "cancelled"
        order.completed_date = clock_now()
        await self._repo.add_state_history(order_id, old_state, "cancelled", reason="cancelled by user")
        await self._repo.update(order)

        await publish(
            self._session,
            event_type="order.cancelled",
            aggregate_type="ProductOrder",
            aggregate_id=order_id,
            payload={
                "commercialOrderId": order_id,
                "customerId": order.customer_id,
            },
            exchange=self._exchange,
        )

        await self._session.commit()
        return await self._repo.get(order_id)

    async def handle_service_order_completed(
        self,
        *,
        commercial_order_id: str,
        customer_id: str,
        offering_id: str,
        msisdn: str,
        iccid: str,
        payment_method_id: str,
        cfs_service_id: str,
        price_snapshot: dict | None = None,
    ) -> None:
        """Called from MQ consumer when service_order.completed."""
        with tracer("bss-com").start_as_current_span(
            "com.order.complete_to_subscription"
        ) as span:
            span.set_attribute(semconv.BSS_ORDER_ID, commercial_order_id)
            span.set_attribute(semconv.BSS_CUSTOMER_ID, customer_id)
            span.set_attribute(semconv.BSS_OFFERING_ID, offering_id)

            order = await self._repo.get(commercial_order_id)
            if not order or order.state != "in_progress":
                log.warning(
                    "order.service_order_completed.skipped",
                    commercial_order_id=commercial_order_id,
                    reason="order not found or not in_progress",
                )
                return

            item = order.items[0] if order.items else None

            # Resolve price snapshot — prefer event payload, fall back to the
            # row stamped at create_order time. The order item is the durable
            # source of truth in case the event arrives stripped.
            if price_snapshot is None and item is not None and item.price_amount is not None:
                price_snapshot = {
                    "priceAmount": str(item.price_amount),
                    "priceCurrency": item.price_currency,
                    "priceOfferingPriceId": item.price_offering_price_id,
                }

            # v1.1 — consume the promo entitlement (the gate). claim-at-activation:
            # SOM has already succeeded by now, so a provisioning failure never
            # burns the code; only a payment decline can (→ revoke below). The
            # discount terms then ride on the snapshot so subscription charges
            # the effective price for period 1.
            #
            # v1.1.3 — a promo failure must NOT brick an order that has already
            # cleared KYC + payment. If the claim is refused (exhausted code,
            # loyalty policy refusal) or loyalty is unreachable, degrade to full
            # price: drop the discount from the snapshot, emit a signal, and let
            # activation proceed. Mirrors the create-time "loyalty error → full
            # price, never block the order" rule. The discount only rides on the
            # snapshot when the entitlement was actually claimed.
            offer_id = None
            promo_claimed = False
            try:
                offer_id = await self._claim_entitlement(order, item)
                promo_claimed = offer_id is not None
            except (PolicyViolationFromServer, ClientError) as exc:
                log.warning(
                    "order.promo.claim_failed_degrade_to_full_price",
                    commercial_order_id=order.id,
                    customer_id=customer_id,
                    promo_code=item.discount_code if item is not None else None,
                    error=str(exc),
                )
                await publish(
                    self._session,
                    event_type="order.promo_not_applied",
                    aggregate_type="ProductOrder",
                    aggregate_id=order.id,
                    payload={
                        "commercialOrderId": order.id,
                        "customerId": customer_id,
                        "promoCode": item.discount_code if item is not None else None,
                        "reason": "claim_refused",
                    },
                    exchange=self._exchange,
                )

            if promo_claimed and item is not None and price_snapshot is not None:
                price_snapshot = {
                    **price_snapshot,
                    "discountType": item.discount_type,
                    "discountValue": (
                        str(item.discount_value) if item.discount_value is not None else None
                    ),
                    "discountPeriodsTotal": item.discount_periods_total,
                    "promoCode": item.discount_code,
                    "promoOfferDefinitionId": item.promo_offer_definition_id,
                }

            # Create subscription. v1.2 — pass the commercial order id as the
            # idempotency key: a redelivered service_order.completed returns the
            # existing subscription instead of charging the card-on-file twice.
            create_kwargs = {
                "customer_id": customer_id,
                "offering_id": offering_id,
                "msisdn": msisdn,
                "iccid": iccid,
                "payment_method_id": payment_method_id,
                "commercial_order_id": commercial_order_id,
            }
            if price_snapshot is not None:
                create_kwargs["price_snapshot"] = price_snapshot
            try:
                sub_result = await self._subscription.create(**create_kwargs)
            except Exception:
                # Activation failed (typically a payment decline). Release the
                # entitlement so a single-use code isn't burned, then propagate.
                if offer_id is not None:
                    await self._revoke_entitlement(offer_id, order.id)
                raise
            if sub_result.get("id"):
                span.set_attribute(semconv.BSS_SUBSCRIPTION_ID, sub_result["id"])

            # Activation succeeded → redeem the entitlement.
            if offer_id is not None:
                await self._redeem_entitlement(offer_id, order.id)

            # Update order item
            if item is not None:
                item.target_subscription_id = sub_result.get("id")
                item.state = "completed"
                if offer_id is not None:
                    # capture the loyalty offer id (for a typed code it was minted
                    # at claim; for an assigned offer it's the same id).
                    item.promo_offer_id = offer_id

            check_order_transition(order.state, "completed")
            order.state = "completed"
            order.completed_date = clock_now()
            await self._repo.add_state_history(
                order.id, "in_progress", "completed", reason="service order completed"
            )

            await publish(
                self._session,
                event_type="order.completed",
                aggregate_type="ProductOrder",
                aggregate_id=order.id,
                payload={
                    "commercialOrderId": order.id,
                    "customerId": order.customer_id,
                    "subscriptionId": sub_result.get("id"),
                    "cfsServiceId": cfs_service_id,
                },
                exchange=self._exchange,
            )
            # NOTE: session.commit() is done by the consumer

    async def handle_service_order_failed(
        self,
        *,
        commercial_order_id: str,
        reason: str,
    ) -> None:
        """Called from MQ consumer when service_order.failed."""
        order = await self._repo.get(commercial_order_id)
        if not order or order.state != "in_progress":
            log.warning(
                "order.service_order_failed.skipped",
                commercial_order_id=commercial_order_id,
                reason="order not found or not in_progress",
            )
            return

        check_order_transition(order.state, "failed")
        order.state = "failed"
        order.completed_date = clock_now()
        if order.items:
            order.items[0].state = "failed"

        await self._repo.add_state_history(
            order.id, "in_progress", "failed", reason=reason
        )

        await publish(
            self._session,
            event_type="order.failed",
            aggregate_type="ProductOrder",
            aggregate_id=order.id,
            payload={
                "commercialOrderId": order.id,
                "customerId": order.customer_id,
                "reason": reason,
            },
            exchange=self._exchange,
        )
        # NOTE: session.commit() is done by the consumer

    async def get_order(self, order_id: str) -> ProductOrder | None:
        """Read-only: get a single order."""
        return await self._repo.get(order_id)

    async def list_orders_for_customer(self, customer_id: str) -> list[ProductOrder]:
        """Read-only: list orders for a customer."""
        return await self._repo.list_by_customer(customer_id)

    async def list_orders(
        self,
        *,
        customer_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ProductOrder]:
        """Read-only: list orders newest-first, optionally filtered.

        v1.6 — the cockpit CRM order queue needs a cross-customer view;
        reads are free (motto #7), so no policy gate on listing.
        """
        return await self._repo.list(
            customer_id=customer_id, state=state, limit=limit, offset=offset
        )
