"""Usage event repository — writes to mediation.usage_event with sequence IDs."""

from datetime import datetime
from typing import Any

from bss_models.mediation import UsageEvent
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


class UsageEventRepository:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def next_id(self) -> str:
        result = await self._s.execute(
            text("SELECT nextval('mediation.usage_event_id_seq')")
        )
        seq = result.scalar_one()
        return f"UE-{seq:06d}"

    async def create(self, event: UsageEvent) -> UsageEvent:
        self._s.add(event)
        await self._s.flush()
        return event

    async def get(self, event_id: str) -> UsageEvent | None:
        stmt = select(UsageEvent).where(UsageEvent.id == event_id)
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_filters(
        self,
        *,
        subscription_id: str | None = None,
        msisdn: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[UsageEvent]:
        stmt = select(UsageEvent)
        if subscription_id:
            stmt = stmt.where(UsageEvent.subscription_id == subscription_id)
        if msisdn:
            stmt = stmt.where(UsageEvent.msisdn == msisdn)
        if event_type:
            stmt = stmt.where(UsageEvent.event_type == event_type)
        if since:
            stmt = stmt.where(UsageEvent.event_time >= since)
        stmt = stmt.order_by(UsageEvent.event_time.desc()).limit(limit)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def mark_processed(self, event_id: str, error: str | None = None) -> None:
        evt = await self.get(event_id)
        if evt:
            evt.processed = error is None
            evt.processing_error = error
            await self._s.flush()

    @staticmethod
    def to_payload(event: UsageEvent, **extra: Any) -> dict[str, Any]:
        """Build the event payload dict used for audit + MQ publish."""
        base: dict[str, Any] = {
            "usageEventId": event.id,
            "subscriptionId": event.subscription_id,
            "msisdn": event.msisdn,
            "eventType": event.event_type,
            "eventTime": event.event_time.isoformat() if event.event_time else None,
            "quantity": event.quantity,
            "unit": event.unit,
            "source": event.source,
            "rawCdrRef": event.raw_cdr_ref,
            # v0.17 — rating consumer reads this to decide whether to
            # decrement `data_roaming` instead of `data`. Default false
            # is set on the column itself.
            "roamingIndicator": event.roaming_indicator,
        }
        base.update(extra)
        return base
