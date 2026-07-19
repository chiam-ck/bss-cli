"""v0.7 — HTTP routes for active-aware catalog queries."""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import text


@pytest.fixture
async def cleanup_test_rows(settings):
    yield
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text("DELETE FROM catalog.product_offering_price WHERE id LIKE 'TEST_PRICE_%'")
        )
        await session.execute(
            text("DELETE FROM catalog.product_offering WHERE id LIKE 'TEST_OFFERING_%'")
        )
        await session.commit()
    await engine.dispose()


class TestActiveOfferingsRoute:
    async def test_active_at_returns_seeded_plans(self, client: AsyncClient):
        now = datetime.now(timezone.utc)
        r = await client.get(
            "/tmf-api/productCatalogManagement/v4/productOffering",
            params={"activeAt": now.isoformat()},
        )
        assert r.status_code == 200, r.text
        ids = [o["id"] for o in r.json()]
        assert {"PLAN_S", "PLAN_M", "PLAN_L"}.issubset(set(ids))


class TestActivePriceRoute:
    async def test_active_price_for_seed_offering(self, client: AsyncClient):
        r = await client.get(
            "/tmf-api/productCatalogManagement/v4/productOfferingPrice/active/PLAN_M"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == "PRICE_PLAN_M"
        assert body["price"]["taxIncludedAmount"]["value"] == 25.0

    async def test_active_price_for_unknown_offering_is_policy_violation(
        self, client: AsyncClient, cleanup_test_rows
    ):
        # An offering with no recurring price row at all → no active row.
        from bss_catalog.config import Settings
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        engine = create_async_engine(Settings().db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(text("""
                INSERT INTO catalog.product_offering
                    (id, name, spec_id, is_bundle, is_sellable, lifecycle_status,
                     valid_from, valid_to)
                VALUES ('TEST_OFFERING_BARE', 'Bare', 'SPEC_MOBILE_PREPAID',
                        true, true, 'active', NULL, NULL)
            """))
            await session.commit()
        await engine.dispose()

        r = await client.get(
            "/tmf-api/productCatalogManagement/v4/productOfferingPrice/active/TEST_OFFERING_BARE"
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["code"] == "POLICY_VIOLATION"
        assert body["reason"] == "catalog.price.no_active_row"


class TestPriceLookupByIdRoute:
    async def test_existing_price(self, client: AsyncClient):
        r = await client.get(
            "/tmf-api/productCatalogManagement/v4/productOfferingPrice/PRICE_PLAN_S"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == "PRICE_PLAN_S"
        assert body["price"]["taxIncludedAmount"]["value"] == 10.0

    async def test_unknown_price_404(self, client: AsyncClient):
        r = await client.get(
            "/tmf-api/productCatalogManagement/v4/productOfferingPrice/PRICE_DOES_NOT_EXIST"
        )
        assert r.status_code == 404
