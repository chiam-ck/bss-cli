"""Customer + Party + Individual + ContactMedium + Agent repository."""

from bss_models.crm import (
    Agent,
    ContactMedium,
    Customer,
    Individual,
    Party,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


class CustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # ── Customer ────────────────────────────────────────────────────

    async def get(self, customer_id: str) -> Customer | None:
        stmt = (
            select(Customer)
            .options(
                selectinload(Customer.identity),
                selectinload(Customer.cases),
                selectinload(Customer.tickets),
            )
            .where(Customer.id == customer_id)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_party(self, customer_id: str) -> Customer | None:
        stmt = (
            select(Customer)
            .options(selectinload(Customer.identity))
            .where(Customer.id == customer_id)
        )
        result = await self._s.execute(stmt)
        cust = result.scalar_one_or_none()
        if cust:
            # Eager-load party + individual + contacts
            party_stmt = (
                select(Party)
                .options(
                    selectinload(Party.individual),
                    selectinload(Party.contact_mediums),
                )
                .where(Party.id == cust.party_id)
            )
            party_result = await self._s.execute(party_stmt)
            cust._party = party_result.scalar_one_or_none()
        return cust

    async def list_customers(
        self,
        *,
        status: str | None = None,
        name_contains: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Customer]:
        stmt = select(Customer).order_by(Customer.id)
        if status:
            stmt = stmt.where(Customer.status == status)
        if name_contains:
            like = f"%{name_contains}%"
            stmt = (
                stmt.join(Party, Customer.party_id == Party.id)
                .join(Individual, Individual.party_id == Party.id)
                .where(
                    Individual.given_name.ilike(like)
                    | Individual.family_name.ilike(like)
                )
            )
        stmt = stmt.limit(limit).offset(offset)
        result = await self._s.execute(stmt)
        customers = list(result.scalars().all())

        # Eager-load party + individual + contact_mediums so the TMF629
        # projection can emit ``individual.givenName`` / ``familyName``.
        if customers:
            party_ids = [c.party_id for c in customers]
            party_stmt = (
                select(Party)
                .options(
                    selectinload(Party.individual),
                    selectinload(Party.contact_mediums),
                )
                .where(Party.id.in_(party_ids))
            )
            parties = (await self._s.execute(party_stmt)).scalars().all()
            by_id = {p.id: p for p in parties}
            for c in customers:
                c._party = by_id.get(c.party_id)
        return customers

    async def find_by_email(self, email: str) -> Customer | None:
        stmt = (
            select(Customer)
            .join(Party, Customer.party_id == Party.id)
            .join(ContactMedium, ContactMedium.party_id == Party.id)
            .where(
                ContactMedium.medium_type == "email",
                ContactMedium.value == email,
                ContactMedium.valid_to.is_(None),
            )
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def create_party(self, party: Party) -> Party:
        self._s.add(party)
        await self._s.flush()
        return party

    async def create_individual(self, individual: Individual) -> Individual:
        self._s.add(individual)
        await self._s.flush()
        return individual

    async def create_customer(self, customer: Customer) -> Customer:
        self._s.add(customer)
        await self._s.flush()
        return customer

    async def update(self, customer: Customer) -> Customer:
        await self._s.flush()
        return customer

    # ── Contact Medium ──────────────────────────────────────────────

    async def create_contact_medium(self, cm: ContactMedium) -> ContactMedium:
        self._s.add(cm)
        await self._s.flush()
        return cm

    async def get_contact_medium(self, cm_id: str) -> ContactMedium | None:
        result = await self._s.execute(
            select(ContactMedium).where(ContactMedium.id == cm_id)
        )
        return result.scalar_one_or_none()

    async def get_contact_mediums_for_party(self, party_id: str) -> list[ContactMedium]:
        result = await self._s.execute(
            select(ContactMedium)
            .where(ContactMedium.party_id == party_id, ContactMedium.valid_to.is_(None))
            .order_by(ContactMedium.id)
        )
        return list(result.scalars().all())

    async def delete_contact_medium(self, cm: ContactMedium) -> None:
        await self._s.delete(cm)
        await self._s.flush()

    async def update_contact_medium_value(
        self, cm: ContactMedium, new_value: str
    ) -> ContactMedium:
        """Set ``cm.value = new_value`` and flush. v0.10 — used by phone/address.

        Email changes do NOT go through this method; the cross-schema
        atomic flow in ``bss_portal_auth.email_change`` writes
        ``ContactMedium.value`` directly within a single transaction
        spanning ``crm`` + ``portal_auth``.
        """
        cm.value = new_value
        await self._s.flush()
        return cm

    async def get_individual_for_party(self, party_id: str) -> Individual | None:
        result = await self._s.execute(
            select(Individual).where(Individual.party_id == party_id)
        )
        return result.scalar_one_or_none()

    async def update_individual_name(
        self,
        ind: Individual,
        *,
        given_name: str | None = None,
        family_name: str | None = None,
    ) -> Individual:
        """Partial-update the individual's name. v0.10 — used by the
        portal's name-update flow on /profile/contact."""
        if given_name is not None:
            ind.given_name = given_name
        if family_name is not None:
            ind.family_name = family_name
        await self._s.flush()
        return ind

    # ── Agent ───────────────────────────────────────────────────────

    async def get_agent(self, agent_id: str) -> Agent | None:
        result = await self._s.execute(
            select(Agent).where(Agent.id == agent_id)
        )
        return result.scalar_one_or_none()

    async def list_agents(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Agent]:
        stmt = select(Agent).limit(limit).offset(offset).order_by(Agent.id)
        if status:
            stmt = stmt.where(Agent.status == status)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())
