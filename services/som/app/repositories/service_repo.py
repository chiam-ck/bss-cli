"""Service repository — dumb CRUD over ORM."""

from bss_models.service_inventory import Service, ServiceStateHistory
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import auth_context


class ServiceRepository:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def next_id(self) -> str:
        result = await self._s.execute(text("SELECT nextval('service_inventory.service_id_seq')"))
        return f"SVC-{result.scalar_one():04d}"

    async def create(self, service: Service) -> Service:
        self._s.add(service)
        await self._s.flush()
        return service

    async def get(self, service_id: str) -> Service | None:
        stmt = (
            select(Service)
            .options(
                selectinload(Service.children).selectinload(Service.children),
                selectinload(Service.state_history),
            )
            .where(Service.id == service_id)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def get_for_update(self, service_id: str) -> Service | None:
        """Like :meth:`get`, but takes a ``FOR UPDATE`` row lock on the CFS.

        The provisioning task handlers (completed/failed/stuck) do a
        read-modify-write on the CFS ``characteristics`` JSONB (``pendingTasks``).
        The aio-pika consumer runs callbacks concurrently (prefetch 5), each in
        its own session/transaction, so an unlocked read lets two simultaneous
        ``provisioning.task.completed`` events clobber each other's pendingTasks
        update. Locking the row here serializes the RMW: the second handler
        blocks until the first commits. ``FOR UPDATE`` applies to the primary
        Service row only; ``selectinload`` runs its own unlocked SELECTs, which
        is fine — we only need to serialize on the CFS row.
        """
        stmt = (
            select(Service)
            .options(
                selectinload(Service.children).selectinload(Service.children),
                selectinload(Service.state_history),
            )
            .where(Service.id == service_id)
            .with_for_update()
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_subscription(self, subscription_id: str) -> list[Service]:
        stmt = (
            select(Service)
            .options(selectinload(Service.children).selectinload(Service.children))
            .where(Service.subscription_id == subscription_id)
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def add_state_history(
        self,
        service_id: str,
        from_state: str | None,
        to_state: str,
        reason: str | None = None,
    ) -> None:
        ctx = auth_context.current()
        history = ServiceStateHistory(
            service_id=service_id,
            from_state=from_state,
            to_state=to_state,
            changed_by=ctx.actor,
            reason=reason,
        )
        self._s.add(history)

    async def update(self, service: Service) -> Service:
        await self._s.flush()
        return service
