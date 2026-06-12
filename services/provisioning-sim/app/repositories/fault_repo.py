"""Fault injection repository — CRUD for fault rules."""

from bss_models.provisioning import FaultInjection
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class FaultRepository:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def list_all(self) -> list[FaultInjection]:
        stmt = select(FaultInjection).order_by(FaultInjection.task_type)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def get(self, fault_id: str) -> FaultInjection | None:
        stmt = select(FaultInjection).where(FaultInjection.id == fault_id)
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_fault(self, task_type: str) -> FaultInjection | None:
        stmt = (
            select(FaultInjection)
            .where(
                FaultInjection.task_type == task_type,
                FaultInjection.enabled.is_(True),
            )
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def update(self, fault: FaultInjection) -> FaultInjection:
        await self._s.flush()
        return fault
