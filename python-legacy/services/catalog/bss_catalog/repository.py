from datetime import datetime

from bss_clock import now as clock_now
from bss_models.catalog import (
    ProductOffering,
    ProductOfferingPrice,
    ProductSpecification,
    VasOffering,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bss_catalog.policies import PolicyViolation


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_offerings(
        self,
        *,
        lifecycle_status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ProductOffering]:
        stmt = (
            select(ProductOffering)
            .options(
                selectinload(ProductOffering.specification),
                selectinload(ProductOffering.prices),
                selectinload(ProductOffering.allowances),
            )
            .limit(limit)
            .offset(offset)
            .order_by(ProductOffering.id)
        )
        if lifecycle_status:
            stmt = stmt.where(ProductOffering.lifecycle_status == lifecycle_status)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_offering(self, offering_id: str) -> ProductOffering | None:
        stmt = (
            select(ProductOffering)
            .options(
                selectinload(ProductOffering.specification),
                selectinload(ProductOffering.prices),
                selectinload(ProductOffering.allowances),
            )
            .where(ProductOffering.id == offering_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── v0.7 — Active-catalog queries ─────────────────────────────────

    async def list_active_offerings(
        self,
        *,
        at: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ProductOffering]:
        """Offerings sellable at `at` (defaults to now).

        Filters: time-bound (valid_from/valid_to) + is_sellable + lifecycle_status='active'.
        Ordered by lowest active recurring price for deterministic display.
        """
        moment = at or clock_now()
        stmt = (
            select(ProductOffering)
            .options(
                selectinload(ProductOffering.specification),
                selectinload(ProductOffering.prices),
                selectinload(ProductOffering.allowances),
            )
            .where(
                ProductOffering.is_sellable.is_(True),
                ProductOffering.lifecycle_status == "active",
                or_(
                    ProductOffering.valid_from.is_(None),
                    ProductOffering.valid_from <= moment,
                ),
                or_(
                    ProductOffering.valid_to.is_(None),
                    ProductOffering.valid_to > moment,
                ),
            )
            .limit(limit)
            .offset(offset)
            .order_by(ProductOffering.id)
        )
        result = await self._session.execute(stmt)
        offerings = list(result.scalars().all())

        # Order by lowest active recurring price; offerings without an active
        # recurring price float to the end so they're noticed in admin views.
        async def _key(o: ProductOffering) -> tuple[int, float, str]:
            try:
                price = await self.get_active_price(o.id, at=moment)
                return (0, float(price.amount), o.id)
            except PolicyViolation:
                return (1, 0.0, o.id)

        keyed = [(await _key(o), o) for o in offerings]
        keyed.sort(key=lambda kv: kv[0])
        return [o for _, o in keyed]

    async def get_active_price(
        self,
        offering_id: str,
        *,
        at: datetime | None = None,
    ) -> ProductOfferingPrice:
        """Lowest-amount price row currently active on `offering_id`.

        Doctrine: when multiple rows are active simultaneously (e.g. base
        price + windowed promo), the lowest amount wins. Raises
        PolicyViolation('catalog.price.no_active_row') when none match —
        the renewal stack must never silently fall back to a phantom row.
        """
        moment = at or clock_now()
        stmt = (
            select(ProductOfferingPrice)
            .where(
                ProductOfferingPrice.offering_id == offering_id,
                ProductOfferingPrice.price_type == "recurring",
                or_(
                    ProductOfferingPrice.valid_from.is_(None),
                    ProductOfferingPrice.valid_from <= moment,
                ),
                or_(
                    ProductOfferingPrice.valid_to.is_(None),
                    ProductOfferingPrice.valid_to > moment,
                ),
            )
            .order_by(ProductOfferingPrice.amount.asc(), ProductOfferingPrice.id.asc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise PolicyViolation(
                rule="catalog.price.no_active_row",
                message=f"No active recurring price for offering {offering_id} at {moment.isoformat()}",
                context={"offering_id": offering_id, "at": moment.isoformat()},
            )
        return row

    async def get_offering_price_by_id(
        self, price_id: str
    ) -> ProductOfferingPrice | None:
        """Direct lookup by price id — no time filter.

        Used by renewal-time math to read the snapshot row regardless of
        whether it's still active. The snapshot remembers history.
        """
        stmt = select(ProductOfferingPrice).where(ProductOfferingPrice.id == price_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_specifications(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ProductSpecification]:
        stmt = (
            select(ProductSpecification)
            .limit(limit)
            .offset(offset)
            .order_by(ProductSpecification.id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_specification(self, spec_id: str) -> ProductSpecification | None:
        stmt = select(ProductSpecification).where(ProductSpecification.id == spec_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_vas_offerings(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[VasOffering]:
        stmt = (
            select(VasOffering)
            .limit(limit)
            .offset(offset)
            .order_by(VasOffering.id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_vas_offering(self, vas_id: str) -> VasOffering | None:
        stmt = select(VasOffering).where(VasOffering.id == vas_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
