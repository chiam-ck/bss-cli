"""Interaction repository."""

from bss_models.crm import Interaction
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class InteractionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, interaction: Interaction) -> Interaction:
        self._s.add(interaction)
        await self._s.flush()
        return interaction

    async def list_for_customer(
        self,
        customer_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Interaction]:
        stmt = (
            select(Interaction)
            .where(Interaction.customer_id == customer_id)
            .order_by(Interaction.occurred_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())
