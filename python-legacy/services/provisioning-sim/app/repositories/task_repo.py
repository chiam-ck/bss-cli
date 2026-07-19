"""Provisioning task repository — CRUD + sequence IDs."""

from bss_models.provisioning import ProvisioningTask
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


class TaskRepository:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def next_id(self) -> str:
        result = await self._s.execute(
            text("SELECT nextval('provisioning.task_id_seq')")
        )
        seq = result.scalar_one()
        return f"PTK-{seq:04d}"

    async def create(self, task: ProvisioningTask) -> ProvisioningTask:
        self._s.add(task)
        await self._s.flush()
        return task

    async def get(self, task_id: str) -> ProvisioningTask | None:
        stmt = select(ProvisioningTask).where(ProvisioningTask.id == task_id)
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_tasks(
        self,
        service_id: str | None = None,
        state: str | None = None,
    ) -> list[ProvisioningTask]:
        stmt = select(ProvisioningTask)
        if service_id:
            stmt = stmt.where(ProvisioningTask.service_id == service_id)
        if state:
            stmt = stmt.where(ProvisioningTask.state == state)
        stmt = stmt.order_by(ProvisioningTask.created_at.desc())
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def update(self, task: ProvisioningTask) -> ProvisioningTask:
        await self._s.flush()
        return task
