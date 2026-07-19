"""Case + CaseNote repository."""

from bss_models.crm import Case, CaseNote
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


class CaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, case_id: str) -> Case | None:
        stmt = (
            select(Case)
            .options(selectinload(Case.notes), selectinload(Case.tickets))
            .where(Case.id == case_id)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_cases(
        self,
        *,
        customer_id: str | None = None,
        state: str | None = None,
        assigned_agent_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Case]:
        stmt = (
            select(Case)
            .options(selectinload(Case.notes), selectinload(Case.tickets))
            .limit(limit)
            .offset(offset)
            .order_by(Case.opened_at.desc())
        )
        if customer_id:
            stmt = stmt.where(Case.customer_id == customer_id)
        if state:
            stmt = stmt.where(Case.state == state)
        if assigned_agent_id:
            stmt = stmt.where(Case.opened_by_agent_id == assigned_agent_id)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def create(self, case: Case) -> Case:
        self._s.add(case)
        await self._s.flush()
        return case

    async def update(self, case: Case) -> Case:
        await self._s.flush()
        return case

    async def add_note(self, note: CaseNote) -> CaseNote:
        self._s.add(note)
        await self._s.flush()
        return note
