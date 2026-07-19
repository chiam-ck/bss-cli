"""RabbitMQ consumer for Rating.

Subscribes to `usage.recorded`. For each event:
 1. Fetch tariff (offering) from Catalog
 2. Call pure `rate_usage`
 3. Write audit row + best-effort publish `usage.rated`
"""

import json

import aio_pika
import structlog

from app.domain.rating import RatingError, UsageInput, rate_usage
from app.events import publisher

log = structlog.get_logger()


async def setup_consumer(app) -> None:
    settings = app.state.settings
    if not settings.mq_url:
        log.warning("mq.not_configured")
        return

    connection = await aio_pika.connect_robust(settings.mq_url)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=5)
    exchange = await channel.declare_exchange(
        "bss.events", aio_pika.ExchangeType.TOPIC, durable=True,
    )

    app.state.mq_connection = connection
    app.state.mq_exchange = exchange

    queue = await channel.declare_queue("rating.usage.recorded", durable=True)
    await queue.bind(exchange, "usage.recorded")

    async def on_usage_recorded(message: aio_pika.abc.AbstractIncomingMessage):
        async with message.process():
            body = json.loads(message.body)
            log.info(
                "mq.usage.recorded.received",
                usage_event_id=body.get("usageEventId"),
                subscription_id=body.get("subscriptionId"),
            )
            async with app.state.session_factory() as session:
                try:
                    await _handle_usage_recorded(
                        body,
                        session=session,
                        catalog_client=app.state.catalog_client,
                        exchange=exchange,
                    )
                    await session.commit()
                except RatingError as exc:
                    log.warning(
                        "rating.error",
                        usage_event_id=body.get("usageEventId"),
                        error=str(exc),
                    )
                    await session.rollback()
                except Exception:
                    log.exception(
                        "rating.handler.unexpected_error",
                        usage_event_id=body.get("usageEventId"),
                    )
                    await session.rollback()

    await queue.consume(on_usage_recorded)
    log.info("mq.consumer.started", queue="rating.usage.recorded")


async def _handle_usage_recorded(
    body: dict,
    *,
    session,
    catalog_client,
    exchange,
) -> None:
    offering_id = body.get("offeringId")
    if not offering_id:
        raise RatingError(
            f"usage.recorded payload missing offeringId "
            f"(usage_event_id={body.get('usageEventId')})"
        )

    tariff = await catalog_client.get_offering(offering_id)

    usage = UsageInput(
        usage_event_id=body["usageEventId"],
        subscription_id=body["subscriptionId"],
        msisdn=body["msisdn"],
        event_type=body["eventType"],
        quantity=int(body["quantity"]),
        unit=body["unit"],
    )
    result = rate_usage(usage, tariff)

    # v0.17 — roaming routing happens here, NOT in `rate_usage`.
    # Doctrine: the pure rating function stays unaware of roaming so
    # every existing test fixture and every existing scenario continues
    # to work. Override `allowance_type` to `data_roaming` only when the
    # event was produced on a visited network AND the offering carries a
    # `data_roaming` allowance row. If the offering carries no roaming
    # allowance, emit `usage.rejected` (rating-stage rejection — no
    # mediation row was rejected, but the decrement instruction is
    # withheld) and return without publishing `usage.rated`.
    allowance_type = result.allowance_type
    if bool(body.get("roamingIndicator", False)) and allowance_type == "data":
        has_roaming = any(
            (a or {}).get("allowanceType") == "data_roaming"
            for a in (tariff.get("bundleAllowance") or [])
        )
        if not has_roaming:
            await publisher.publish(
                session,
                event_type="usage.rejected",
                aggregate_type="usage",
                aggregate_id=result.usage_event_id,
                payload={
                    "usageEventId": result.usage_event_id,
                    "subscriptionId": result.subscription_id,
                    "msisdn": usage.msisdn,
                    "eventType": usage.event_type,
                    "reason": "rating.no_roaming_allowance",
                    "offeringId": offering_id,
                },
                exchange=exchange,
            )
            log.warning(
                "usage.rejected.no_roaming_allowance",
                usage_event_id=result.usage_event_id,
                offering_id=offering_id,
            )
            return
        allowance_type = "data_roaming"

    payload = {
        "usageEventId": result.usage_event_id,
        "subscriptionId": result.subscription_id,
        "allowanceType": allowance_type,
        "consumedQuantity": result.consumed_quantity,
        "unit": result.unit,
        "chargeAmount": str(result.charge_amount),
        "currency": result.currency,
        "offeringId": offering_id,
    }

    await publisher.publish(
        session,
        event_type="usage.rated",
        aggregate_type="usage",
        aggregate_id=result.usage_event_id,
        payload=payload,
        exchange=exchange,
    )
    log.info(
        "usage.rated.emitted",
        usage_event_id=result.usage_event_id,
        subscription_id=result.subscription_id,
        allowance_type=allowance_type,
        consumed_quantity=result.consumed_quantity,
    )
