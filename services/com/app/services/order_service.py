"""Order orchestration service — calls policies, not repositories directly."""

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from bss_clients import ClientError
from bss_clock import now as clock_now
from bss_telemetry import semconv, tracer
from sqlalchemy.ext.asyncio import AsyncSession

from bss_models.order_mgmt import OrderItem, ProductOrder

from app.events.publisher import publish
from app.policies.base import PolicyViolation
from app.policies.order import (
    check_cancel_allowed_after_som,
    check_customer_exists,
    check_customer_has_payment_method,
    check_offering_currently_sellable,
    check_offering_exists,
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
    ):
        self._session = session
        self._repo = repo
        self._crm = crm_client
        self._catalog = catalog_client
        self._payment = payment_client
        self._som = som_client
        self._subscription = subscription_client
        self._exchange = exchange

    async def create_order(
        self,
        *,
        customer_id: str,
        offering_id: str,
        msisdn_preference: str | None = None,
        notes: str | None = None,
        discount_code: str | None = None,
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
            customer_id=customer_id, offering_id=offering_id, discount_code=discount_code
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
        self, *, customer_id: str, offering_id: str, discount_code: str | None
    ) -> dict:
        """Resolve a promo to stamp as INTENT on the order item.

        Returns the discount fields, or ``{}`` for no/invalid promo. Catalog
        owns all promo logic (loyalty link, applicability, effective price); COM
        just records the terms. A catalog/loyalty hiccup must never block the
        order, so transport errors degrade to "no discount" with a warning.
        """
        try:
            if discount_code:
                res = await self._catalog.validate_promo(
                    code=discount_code, offering=offering_id
                )
                if not res.get("valid"):
                    log.info(
                        "order.promo.code_invalid",
                        code=discount_code,
                        reason=res.get("reason"),
                    )
                    return {}
                offer_id = None  # a typed code's offer is created at claim
            else:
                res = await self._catalog.resolve_assigned_offer(
                    customer_id=customer_id, offering=offering_id
                )
                if not res.get("valid"):
                    return {}
                offer_id = res.get("offerId")  # assigned offer already exists
        except ClientError:
            log.warning("order.promo.resolve_failed", code=discount_code, exc_info=True)
            return {}

        raw_value = res.get("discountValue")
        return {
            "discount_code": discount_code if discount_code else None,
            "offer_definition_id": res.get("offerDefinitionId"),
            "discount_type": res.get("discountType"),
            "discount_value": Decimal(raw_value) if raw_value is not None else None,
            "discount_periods_total": res.get("discountPeriodsTotal"),
            "promo_offer_id": offer_id,
        }

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

            # Resolve price snapshot — prefer event payload, fall back to the
            # row stamped at create_order time. The order item is the durable
            # source of truth in case the event arrives stripped.
            if price_snapshot is None and order.items and order.items[0].price_amount is not None:
                item = order.items[0]
                price_snapshot = {
                    "priceAmount": str(item.price_amount),
                    "priceCurrency": item.price_currency,
                    "priceOfferingPriceId": item.price_offering_price_id,
                }

            # Create subscription
            create_kwargs = {
                "customer_id": customer_id,
                "offering_id": offering_id,
                "msisdn": msisdn,
                "iccid": iccid,
                "payment_method_id": payment_method_id,
            }
            if price_snapshot is not None:
                create_kwargs["price_snapshot"] = price_snapshot
            sub_result = await self._subscription.create(**create_kwargs)
            if sub_result.get("id"):
                span.set_attribute(semconv.BSS_SUBSCRIPTION_ID, sub_result["id"])

            # Update order item
            if order.items:
                order.items[0].target_subscription_id = sub_result.get("id")
                order.items[0].state = "completed"

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
