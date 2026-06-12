"""Catalog service layer — admin write paths.

Reads stay on the repository; writes go through this layer so the policy
seam (admin role + structural validation) is consistent with every other
BSS service. The CLI is a transport — it never reaches the repository
directly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import structlog
from bss_clock import now as clock_now
from bss_models.catalog import (
    BundleAllowance,
    ProductOffering,
    ProductOfferingPrice,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bss_catalog.policies import PolicyViolation
from bss_catalog.repository import CatalogRepository

log = structlog.get_logger()


def _check_admin(actor: str) -> None:
    """v0.7 — admin gate.

    Catalog service has no auth_context module; the actor identity flows in
    via X-BSS-Actor (see bss-clients header propagation). Any non-empty
    actor is treated as admin in v0.3, which matches the existing Auth
    posture; Phase 12 swaps this for a JWT role check.
    """
    if not actor or actor in {"anonymous", ""}:
        raise PolicyViolation(
            rule="catalog.admin_only",
            message="Catalog write operations require an authenticated admin actor",
            context={"actor": actor},
        )


class CatalogAdminService:
    def __init__(self, session: AsyncSession, repo: CatalogRepository, actor: str):
        self._session = session
        self._repo = repo
        self._actor = actor

    async def add_offering(
        self,
        *,
        offering_id: str,
        name: str,
        spec_id: str,
        amount: Decimal,
        currency: str,
        price_id: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        data_mb: int | None = None,
        voice_minutes: int | None = None,
        sms_count: int | None = None,
        data_roaming_mb: int | None = None,
    ) -> ProductOffering:
        """Insert offering + recurring price + bundle allowances atomically."""
        _check_admin(self._actor)

        existing = await self._repo.get_offering(offering_id)
        if existing is not None:
            raise PolicyViolation(
                rule="catalog.offering.already_exists",
                message=f"Offering {offering_id} already exists",
                context={"offering_id": offering_id},
            )

        resolved_price_id = price_id or f"PRICE_{offering_id}"

        offering = ProductOffering(
            id=offering_id,
            name=name,
            spec_id=spec_id,
            is_bundle=True,
            is_sellable=True,
            lifecycle_status="active",
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._session.add(offering)

        price = ProductOfferingPrice(
            id=resolved_price_id,
            offering_id=offering_id,
            price_type="recurring",
            recurring_period_length=1,
            recurring_period_type="month",
            amount=amount,
            currency=currency,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._session.add(price)

        if data_mb is not None:
            self._session.add(BundleAllowance(
                id=f"BA_{offering_id}_DATA",
                offering_id=offering_id,
                allowance_type="data",
                quantity=data_mb,
                unit="mb",
            ))
        if voice_minutes is not None:
            self._session.add(BundleAllowance(
                id=f"BA_{offering_id}_VOICE",
                offering_id=offering_id,
                allowance_type="voice",
                quantity=voice_minutes,
                unit="minutes",
            ))
        if sms_count is not None:
            self._session.add(BundleAllowance(
                id=f"BA_{offering_id}_SMS",
                offering_id=offering_id,
                allowance_type="sms",
                quantity=sms_count,
                unit="count",
            ))
        if data_roaming_mb is not None:
            self._session.add(BundleAllowance(
                id=f"BA_{offering_id}_ROAM",
                offering_id=offering_id,
                allowance_type="data_roaming",
                quantity=data_roaming_mb,
                unit="mb",
            ))

        await self._session.commit()
        log.info(
            "catalog.offering.added",
            offering_id=offering_id,
            actor=self._actor,
            amount=str(amount),
        )
        return await self._repo.get_offering(offering_id)

    async def set_offering_window(
        self,
        *,
        offering_id: str,
        valid_from: datetime | None,
        valid_to: datetime | None,
    ) -> ProductOffering:
        _check_admin(self._actor)

        existing = await self._repo.get_offering(offering_id)
        if existing is None:
            raise PolicyViolation(
                rule="catalog.offering.not_found",
                message=f"Offering {offering_id} not found",
                context={"offering_id": offering_id},
            )

        await self._session.execute(
            update(ProductOffering)
            .where(ProductOffering.id == offering_id)
            .values(valid_from=valid_from, valid_to=valid_to)
        )
        await self._session.commit()
        log.info(
            "catalog.offering.windowed",
            offering_id=offering_id,
            valid_from=valid_from.isoformat() if valid_from else None,
            valid_to=valid_to.isoformat() if valid_to else None,
            actor=self._actor,
        )
        return await self._repo.get_offering(offering_id)

    async def add_price(
        self,
        *,
        offering_id: str,
        price_id: str,
        amount: Decimal,
        currency: str,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        retire_current: bool = False,
    ) -> ProductOfferingPrice:
        """Insert a new price row on an existing offering.

        When ``retire_current`` is True, every existing price row for the
        offering with no ``valid_to`` gets stamped with ``valid_to=valid_from``
        (or now() if no valid_from), retiring them as the new row takes over.
        """
        _check_admin(self._actor)

        offering = await self._repo.get_offering(offering_id)
        if offering is None:
            raise PolicyViolation(
                rule="catalog.offering.not_found",
                message=f"Offering {offering_id} not found",
                context={"offering_id": offering_id},
            )

        existing_price = await self._repo.get_offering_price_by_id(price_id)
        if existing_price is not None:
            raise PolicyViolation(
                rule="catalog.price.already_exists",
                message=f"Price {price_id} already exists",
                context={"price_id": price_id},
            )

        if retire_current:
            cut = valid_from or clock_now()
            await self._session.execute(
                update(ProductOfferingPrice)
                .where(ProductOfferingPrice.offering_id == offering_id)
                .where(ProductOfferingPrice.valid_to.is_(None))
                .values(valid_to=cut)
            )

        price = ProductOfferingPrice(
            id=price_id,
            offering_id=offering_id,
            price_type="recurring",
            recurring_period_length=1,
            recurring_period_type="month",
            amount=amount,
            currency=currency,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._session.add(price)
        await self._session.commit()
        log.info(
            "catalog.price.added",
            offering_id=offering_id,
            price_id=price_id,
            amount=str(amount),
            actor=self._actor,
            retire_current=retire_current,
        )
        return await self._repo.get_offering_price_by_id(price_id)
