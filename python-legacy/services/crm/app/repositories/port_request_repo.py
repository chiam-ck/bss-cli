"""v0.17 — PortRequest repository.

Mirrors the ``CaseRepository`` shape (dumb CRUD over the ORM model).
The MNP FSM lives in ``port_request_service.py``; this layer holds no
domain logic.
"""

from bss_models.crm import PortRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class PortRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, port_id: str) -> PortRequest | None:
        result = await self._s.execute(
            select(PortRequest).where(PortRequest.id == port_id)
        )
        return result.scalar_one_or_none()

    async def list_requests(
        self,
        *,
        state: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PortRequest]:
        stmt = (
            select(PortRequest)
            .order_by(PortRequest.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if state:
            stmt = stmt.where(PortRequest.state == state)
        if direction:
            stmt = stmt.where(PortRequest.direction == direction)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def get_active_for_donor(
        self, donor_msisdn: str, tenant_id: str
    ) -> PortRequest | None:
        """Used by the donor-uniqueness policy: returns the open
        (``requested`` or ``validated``) row for this donor MSISDN, if
        any. Mirrors the partial unique index on
        ``crm.port_request.donor_msisdn``.
        """
        result = await self._s.execute(
            select(PortRequest).where(
                PortRequest.donor_msisdn == donor_msisdn,
                PortRequest.tenant_id == tenant_id,
                PortRequest.state.in_(("requested", "validated")),
            )
        )
        return result.scalar_one_or_none()

    async def create(self, port: PortRequest) -> PortRequest:
        self._s.add(port)
        await self._s.flush()
        return port

    async def update(self, port: PortRequest) -> PortRequest:
        await self._s.flush()
        return port
