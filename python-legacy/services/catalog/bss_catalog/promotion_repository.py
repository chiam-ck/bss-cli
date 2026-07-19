"""Promotion repository — dumb reads over the catalog.promotion table (v1.1).

Writes (insert / state transitions) are driven by PromotionService through
the session directly, mirroring CatalogAdminService. This layer only
queries — no business logic, no outward calls.
"""

from __future__ import annotations

from bss_models.catalog import Promotion, PromotionEligibility
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class PromotionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, promotion_id: str) -> Promotion | None:
        stmt = select(Promotion).where(Promotion.id == promotion_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_code(self, code: str) -> Promotion | None:
        """Lookup by typed code. Used for create-time uniqueness + preview/validate.

        Codeless targeted promotions have NULL code and are never returned here.
        """
        stmt = select(Promotion).where(Promotion.code == code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_offer_definition_id(self, offer_definition_id: str) -> Promotion | None:
        """The loyalty join-key lookup — promo terms for an entitlement.

        Used by validate-for-order and the targeted-offer display path, which
        start from a loyalty OfferDefinition id and need the BSS money terms.
        """
        stmt = select(Promotion).where(
            Promotion.offer_definition_id == offer_definition_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        *,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Promotion]:
        stmt = select(Promotion).order_by(Promotion.id).limit(limit).offset(offset)
        if state is not None:
            stmt = stmt.where(Promotion.state == state)
        return list((await self._session.execute(stmt)).scalars().all())

    # ── eligibility (v1.1.1 — targeted = code + eligibility list) ─────────

    async def add_eligibility(
        self,
        *,
        promotion_id: str,
        customer_id: str,
        created_by: str,
        loyalty_offer_id: str | None = None,
    ) -> bool:
        """Add a (promotion, customer) eligibility row. Idempotent — returns
        False if it already existed (so a re-run reports it as 'already').

        v1.3.0 — ``loyalty_offer_id`` is the upfront-minted loyalty offer (the
        ``offer.issue`` saga step at assign time) so the customer↔offer pairing
        exists in loyalty immediately. COM uses it at activation via
        ``advance_to_claimed`` (targeted path); public typed codes still
        claim-by-code and never reach this column.
        """
        existing = await self._session.execute(
            select(PromotionEligibility.id).where(
                PromotionEligibility.promotion_id == promotion_id,
                PromotionEligibility.customer_id == customer_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False
        self._session.add(
            PromotionEligibility(
                promotion_id=promotion_id,
                customer_id=customer_id,
                created_by=created_by,
                loyalty_offer_id=loyalty_offer_id,
            )
        )
        return True

    async def get_loyalty_offer_id(
        self, *, promotion_id: str, customer_id: str
    ) -> str | None:
        """Look up the upfront-minted loyalty offer id for (promo, customer).

        Returns None for rows written before v1.3.0; consume falls back to
        claim-by-code in that case (a transparent backstop, not a doctrine
        path).
        """
        r = await self._session.execute(
            select(PromotionEligibility.loyalty_offer_id).where(
                PromotionEligibility.promotion_id == promotion_id,
                PromotionEligibility.customer_id == customer_id,
            )
        )
        return r.scalar_one_or_none()

    async def remove_eligibility(
        self, *, promotion_id: str, customer_id: str
    ) -> str | None:
        """Remove a (promotion, customer) eligibility row.

        Returns the row's ``loyalty_offer_id`` (which may be ``None`` for a
        pre-v1.3.0 row, or for a v1.3.0 row where ``offer.issue`` had degraded)
        so the service layer can ``offer.revoke`` against it. Returns the
        sentinel ``__not_found__`` if there was no row to remove (so the
        service can report it under ``not_eligible`` — idempotent unassign).
        """
        from sqlalchemy import delete as sa_delete

        r = await self._session.execute(
            select(PromotionEligibility.id, PromotionEligibility.loyalty_offer_id).where(
                PromotionEligibility.promotion_id == promotion_id,
                PromotionEligibility.customer_id == customer_id,
            )
        )
        row = r.first()
        if row is None:
            return "__not_found__"
        await self._session.execute(
            sa_delete(PromotionEligibility).where(PromotionEligibility.id == row.id)
        )
        return row.loyalty_offer_id

    async def is_eligible(self, promotion_id: str, customer_id: str) -> bool:
        r = await self._session.execute(
            select(PromotionEligibility.id)
            .where(
                PromotionEligibility.promotion_id == promotion_id,
                PromotionEligibility.customer_id == customer_id,
            )
            .limit(1)
        )
        return r.scalar_one_or_none() is not None

    async def list_eligible_promotions(self, customer_id: str) -> list[Promotion]:
        """Active targeted promotions this customer is eligible for (the
        auto-apply candidate set + the dashboard read)."""
        stmt = (
            select(Promotion)
            .join(PromotionEligibility, PromotionEligibility.promotion_id == Promotion.id)
            .where(
                PromotionEligibility.customer_id == customer_id,
                Promotion.state == "active",
                Promotion.audience == "targeted",
            )
            .order_by(Promotion.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())
