"""KYC (CustomerIdentity) repository."""

from bss_models.crm import CustomerIdentity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class KycRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, customer_id: str) -> CustomerIdentity | None:
        result = await self._s.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.customer_id == customer_id
            )
        )
        return result.scalar_one_or_none()

    async def find_by_document_hash(
        self,
        *,
        tenant_id: str,
        document_type: str,
        document_number_hash: str,
    ) -> CustomerIdentity | None:
        result = await self._s.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.tenant_id == tenant_id,
                CustomerIdentity.document_type == document_type,
                CustomerIdentity.document_number_hash == document_number_hash,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, identity: CustomerIdentity) -> CustomerIdentity:
        self._s.add(identity)
        await self._s.flush()
        return identity

    async def delete(self, identity: CustomerIdentity) -> None:
        await self._s.delete(identity)
