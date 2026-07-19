"""Ticket + TicketStateHistory repository."""

from bss_models.crm import Ticket, TicketStateHistory
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.ticket_state import TERMINAL


class TicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, ticket_id: str) -> Ticket | None:
        stmt = (
            select(Ticket)
            .options(selectinload(Ticket.state_history))
            .where(Ticket.id == ticket_id)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_tickets(
        self,
        *,
        customer_id: str | None = None,
        case_id: str | None = None,
        state: str | None = None,
        assigned_to_agent_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Ticket]:
        stmt = (
            select(Ticket)
            .options(selectinload(Ticket.state_history))
            .limit(limit)
            .offset(offset)
            .order_by(Ticket.opened_at.desc())
        )
        if customer_id:
            stmt = stmt.where(Ticket.customer_id == customer_id)
        if case_id:
            stmt = stmt.where(Ticket.case_id == case_id)
        if state:
            stmt = stmt.where(Ticket.state == state)
        if assigned_to_agent_id:
            stmt = stmt.where(Ticket.assigned_to_agent_id == assigned_to_agent_id)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def find_open_by_case(self, case_id: str) -> list[Ticket]:
        """Return tickets for a case that are NOT in a terminal state."""
        stmt = (
            select(Ticket)
            .where(
                Ticket.case_id == case_id,
                Ticket.state.notin_(list(TERMINAL)),
            )
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def create(self, ticket: Ticket) -> Ticket:
        self._s.add(ticket)
        await self._s.flush()
        return ticket

    async def update(self, ticket: Ticket) -> Ticket:
        await self._s.flush()
        return ticket

    async def add_state_history(self, entry: TicketStateHistory) -> TicketStateHistory:
        self._s.add(entry)
        await self._s.flush()
        return entry
