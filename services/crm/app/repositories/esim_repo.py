"""eSIM profile repository."""


from bss_clock import now as clock_now
from bss_models.inventory import EsimProfile
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


class EsimRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, iccid: str) -> EsimProfile | None:
        result = await self._s.execute(
            select(EsimProfile).where(EsimProfile.iccid == iccid)
        )
        return result.scalar_one_or_none()

    async def list_esims(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[EsimProfile]:
        stmt = (
            select(EsimProfile)
            .limit(limit)
            .offset(offset)
            .order_by(EsimProfile.iccid)
        )
        if status:
            stmt = stmt.where(EsimProfile.profile_state == status)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def reserve_next_available(self, tenant_id: str) -> EsimProfile | None:
        """Atomically reserve the next available eSIM profile."""
        result = await self._s.execute(
            text("""
                SELECT iccid FROM inventory.esim_profile
                WHERE profile_state = 'available' AND tenant_id = :t
                ORDER BY iccid LIMIT 1
                FOR UPDATE SKIP LOCKED
            """),
            {"t": tenant_id},
        )
        iccid = result.scalar_one_or_none()
        if not iccid:
            return None
        now = clock_now()
        await self._s.execute(
            text("""
                UPDATE inventory.esim_profile
                SET profile_state = 'reserved', reserved_at = :now, updated_at = :now
                WHERE iccid = :i
            """),
            {"i": iccid, "now": now},
        )
        return await self.get(iccid)

    async def update_state(
        self, iccid: str, new_state: str, **extra_fields: str | None
    ) -> EsimProfile | None:
        row = await self.get(iccid)
        if not row:
            return None
        row.profile_state = new_state
        now = clock_now()
        if new_state == "downloaded":
            row.downloaded_at = now
        elif new_state == "activated":
            row.activated_at = now
        elif new_state == "available":
            row.reserved_at = None
            row.assigned_msisdn = None
            row.assigned_to_subscription_id = None
        for k, v in extra_fields.items():
            if hasattr(row, k):
                setattr(row, k, v)
        await self._s.flush()
        return row
