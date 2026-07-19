"""ServiceOrder repository — dumb CRUD over ORM."""

from bss_models.service_inventory import ServiceOrder
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


class ServiceOrderRepository:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def next_order_id(self) -> str:
        result = await self._s.execute(text("SELECT nextval('service_inventory.service_order_id_seq')"))
        return f"SO-{result.scalar_one():04d}"

    async def next_item_id(self) -> str:
        result = await self._s.execute(text("SELECT nextval('service_inventory.service_order_item_id_seq')"))
        return f"SOI-{result.scalar_one():04d}"

    async def create(self, order: ServiceOrder) -> ServiceOrder:
        self._s.add(order)
        await self._s.flush()
        return order

    async def get(self, order_id: str) -> ServiceOrder | None:
        stmt = (
            select(ServiceOrder)
            .options(selectinload(ServiceOrder.items))
            .where(ServiceOrder.id == order_id)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_commercial_order(self, commercial_order_id: str) -> list[ServiceOrder]:
        stmt = (
            select(ServiceOrder)
            .options(selectinload(ServiceOrder.items))
            .where(ServiceOrder.commercial_order_id == commercial_order_id)
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def update(self, order: ServiceOrder) -> ServiceOrder:
        await self._s.flush()
        return order
