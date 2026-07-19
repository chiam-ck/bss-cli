"""VAS purchase repository."""

from bss_models.subscription import VasPurchase
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class VasPurchaseRepository:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def create(self, purchase: VasPurchase) -> VasPurchase:
        self._s.add(purchase)
        await self._s.flush()
        return purchase

    async def list_for_subscription(self, sub_id: str) -> list[VasPurchase]:
        stmt = (
            select(VasPurchase)
            .where(VasPurchase.subscription_id == sub_id)
            .order_by(VasPurchase.applied_at.desc())
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())
