"""Repository for ProductOrder aggregate — dumb CRUD over ORM."""


from bss_clock import now as clock_now
from bss_models.order_mgmt import OrderStateHistory, ProductOrder
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import auth_context


class OrderRepository:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def next_order_id(self) -> str:
        result = await self._s.execute(text("SELECT nextval('order_mgmt.product_order_id_seq')"))
        return f"ORD-{result.scalar_one():04d}"

    async def next_item_id(self) -> str:
        result = await self._s.execute(text("SELECT nextval('order_mgmt.order_item_id_seq')"))
        return f"OI-{result.scalar_one():04d}"

    async def create(self, order: ProductOrder) -> ProductOrder:
        self._s.add(order)
        await self._s.flush()
        return order

    async def get(self, order_id: str) -> ProductOrder | None:
        stmt = (
            select(ProductOrder)
            .options(
                selectinload(ProductOrder.items),
                selectinload(ProductOrder.state_history),
            )
            .where(ProductOrder.id == order_id)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_customer(self, customer_id: str) -> list[ProductOrder]:
        stmt = (
            select(ProductOrder)
            .options(
                selectinload(ProductOrder.items),
            )
            .where(ProductOrder.customer_id == customer_id)
            .order_by(ProductOrder.created_at.desc())
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def list(
        self,
        *,
        customer_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ProductOrder]:
        stmt = (
            select(ProductOrder)
            .options(
                selectinload(ProductOrder.items),
            )
            .order_by(ProductOrder.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if customer_id:
            stmt = stmt.where(ProductOrder.customer_id == customer_id)
        if state:
            stmt = stmt.where(ProductOrder.state == state)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def add_state_history(
        self,
        order_id: str,
        from_state: str | None,
        to_state: str | None,
        reason: str | None = None,
    ) -> None:
        ctx = auth_context.current()
        history = OrderStateHistory(
            order_id=order_id,
            from_state=from_state,
            to_state=to_state,
            changed_by=ctx.actor,
            reason=reason,
            event_time=clock_now(),
        )
        self._s.add(history)

    async def update(self, order: ProductOrder) -> ProductOrder:
        await self._s.flush()
        return order
