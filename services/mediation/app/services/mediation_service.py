"""Mediation service — TMF635 online mediation (not batch, not OCS).

Flow:
  1. receive usage event
  2. enrich via SubscriptionClient.get_by_msisdn
  3. run policies BEFORE persist (block-at-edge doctrine)
  4. INSERT mediation.usage_event + audit.domain_event (same TX)
  5. commit → best-effort publish to bss.events (routing key usage.recorded)
"""

from datetime import datetime

import aio_pika
import structlog
from bss_clients import SubscriptionClient
from bss_clock import now as clock_now
from bss_models.mediation import UsageEvent
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import publisher
from app.policies.base import PolicyViolation
from app.policies.usage import (
    check_msisdn_matches,
    check_positive_quantity,
    check_subscription_active,
    check_subscription_exists,
    check_valid_event_type,
)
from app.repositories.usage_repo import UsageEventRepository

log = structlog.get_logger()


class MediationService:
    def __init__(
        self,
        session: AsyncSession,
        repo: UsageEventRepository,
        subscription_client: SubscriptionClient,
        exchange: aio_pika.abc.AbstractExchange | None = None,
    ):
        self._session = session
        self._repo = repo
        self._subscription = subscription_client
        self._exchange = exchange

    async def ingest(
        self,
        *,
        msisdn: str,
        event_type: str,
        event_time: datetime,
        quantity: int,
        unit: str,
        source: str | None = None,
        raw_cdr_ref: str | None = None,
        roaming_indicator: bool = False,
    ) -> UsageEvent:
        # Policies that don't require enrichment
        check_positive_quantity(quantity)
        check_valid_event_type(event_type)

        # Enrich via Subscription
        try:
            sub = await check_subscription_exists(msisdn, self._subscription)
        except PolicyViolation as exc:
            await self._record_rejection(
                msisdn=msisdn,
                event_type=event_type,
                event_time=event_time,
                quantity=quantity,
                unit=unit,
                source=source,
                raw_cdr_ref=raw_cdr_ref,
                reason=exc.rule,
            )
            raise

        check_msisdn_matches(sub, msisdn)

        try:
            check_subscription_active(sub)
        except PolicyViolation as exc:
            await self._record_rejection(
                msisdn=msisdn,
                event_type=event_type,
                event_time=event_time,
                quantity=quantity,
                unit=unit,
                source=source,
                raw_cdr_ref=raw_cdr_ref,
                reason=exc.rule,
                subscription_id=sub.get("id"),
                state=sub.get("state"),
            )
            raise

        # Persist — no row until every policy has passed.
        event_id = await self._repo.next_id()
        evt = UsageEvent(
            id=event_id,
            msisdn=msisdn,
            subscription_id=sub["id"],
            event_type=event_type,
            event_time=event_time,
            quantity=quantity,
            unit=unit,
            source=source,
            raw_cdr_ref=raw_cdr_ref,
            processed=False,
            roaming_indicator=roaming_indicator,
        )
        await self._repo.create(evt)

        payload = self._repo.to_payload(
            evt,
            offeringId=sub.get("offeringId"),
        )

        await publisher.publish(
            self._session,
            event_type="usage.recorded",
            aggregate_type="usage",
            aggregate_id=event_id,
            payload=payload,
            exchange=self._exchange,
        )

        await self._session.commit()
        log.info(
            "usage.recorded",
            usage_event_id=event_id,
            subscription_id=sub["id"],
            event_type=event_type,
            quantity=quantity,
        )
        return evt

    async def get(self, event_id: str) -> UsageEvent | None:
        return await self._repo.get(event_id)

    async def list_by_filters(self, **kwargs) -> list[UsageEvent]:
        return await self._repo.list_by_filters(**kwargs)

    async def _record_rejection(
        self,
        *,
        msisdn: str,
        event_type: str,
        event_time: datetime,
        quantity: int,
        unit: str,
        source: str | None,
        raw_cdr_ref: str | None,
        reason: str,
        subscription_id: str | None = None,
        state: str | None = None,
    ) -> None:
        """Doctrine: reject events leave NO mediation.usage_event row — the
        only trace is a `usage.rejected` audit row so the attempt is observable
        without corrupting the CDR stream."""
        payload = {
            "msisdn": msisdn,
            "subscriptionId": subscription_id,
            "state": state,
            "eventType": event_type,
            "eventTime": event_time.isoformat(),
            "quantity": quantity,
            "unit": unit,
            "source": source,
            "rawCdrRef": raw_cdr_ref,
            "reason": reason,
            "rejectedAt": clock_now().isoformat(),
        }
        await publisher.publish(
            self._session,
            event_type="usage.rejected",
            aggregate_type="usage",
            aggregate_id=subscription_id or msisdn,
            payload=payload,
            exchange=self._exchange,
        )
        await self._session.commit()
