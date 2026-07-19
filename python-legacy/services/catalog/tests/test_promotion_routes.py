"""v1.1 — promotion routes (TMF671 writes + portal-facing reads).

ASGITransport doesn't run lifespan, so the LoyaltyClient isn't built; we
inject a FakeLoyalty onto app.state.loyalty_client (reusing the service-test
fake). Writes hit the live dev DB → unique ids + cleanup.
"""

import uuid

import pytest_asyncio
from bss_catalog.app import create_app
from bss_catalog.config import Settings
from bss_middleware import TEST_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_promotion_service import FakeLoyalty

_HDRS = {"X-BSS-API-Token": TEST_TOKEN, "X-BSS-Actor": "admin"}
_TMF = "/tmf-api/promotionManagement/v4"


def _pid(prefix: str = "PROMO_RT") -> str:
    return f"{prefix}_TEST_{uuid.uuid4().hex[:8].upper()}"


@pytest_asyncio.fixture
async def loyalty(settings: Settings) -> FakeLoyalty:
    return FakeLoyalty()


@pytest_asyncio.fixture
async def client(settings: Settings, loyalty: FakeLoyalty):
    app = create_app(settings)
    engine = create_async_engine(settings.db_url, pool_size=2, max_overflow=2)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.loyalty_client = loyalty  # injected (no lifespan under ASGITransport)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=_HDRS) as c:
        yield c
    await engine.dispose()


async def _cleanup(settings: Settings, *ids: str):
    engine = create_async_engine(settings.db_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        await s.execute(
            text("DELETE FROM catalog.promotion_eligibility WHERE promotion_id = ANY(:ids)"),
            {"ids": list(ids)},
        )
        await s.execute(
            text("DELETE FROM catalog.promotion WHERE id = ANY(:ids)"), {"ids": list(ids)}
        )
        await s.commit()
    await engine.dispose()


class TestCreateAndRead:
    async def test_create_returns_201_active_with_type(self, client, settings):
        pid = _pid()
        try:
            r = await client.post(
                f"{_TMF}/promotion",
                json={
                    "promotionId": pid,
                    "discountType": "percent",
                    "discountValue": "20",
                    "durationKind": "single",
                    "audience": "targeted",
                },
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["state"] == "active"
            assert body["audience"] == "targeted"
            assert body["code"] == pid  # derived
            assert body["offerDefinitionId"] == f"OD_{pid}"
            assert body["@type"] == "Promotion"

            g = await client.get(f"{_TMF}/promotion/{pid}")
            assert g.status_code == 200
            assert g.json()["id"] == pid
        finally:
            await _cleanup(settings, pid)

    async def test_get_unknown_is_404(self, client):
        r = await client.get(f"{_TMF}/promotion/PROMO_NOPE_{uuid.uuid4().hex[:6]}")
        assert r.status_code == 404

    async def test_invalid_discount_type_is_422_policy(self, client, settings):
        pid = _pid()
        try:
            r = await client.post(
                f"{_TMF}/promotion",
                json={
                    "promotionId": pid,
                    "discountType": "bogus",
                    "discountValue": "20",
                    "durationKind": "single",
                },
            )
            assert r.status_code == 422
            assert r.json()["code"] == "POLICY_VIOLATION"
            assert r.json()["reason"] == "catalog.promotion.invalid_discount_type"
        finally:
            await _cleanup(settings, pid)

    async def test_list_includes_created(self, client, settings):
        pid = _pid()
        try:
            await client.post(
                f"{_TMF}/promotion",
                json={
                    "promotionId": pid,
                    "discountType": "percent",
                    "discountValue": "10",
                    "durationKind": "single",
                    "audience": "targeted",
                },
            )
            r = await client.get(f"{_TMF}/promotion", params={"state": "active"})
            assert r.status_code == 200
            assert pid in {p["id"] for p in r.json()}
        finally:
            await _cleanup(settings, pid)


class TestAssignRoute:
    async def test_assign_adds_eligibility(self, client, settings):
        pid = _pid()
        try:
            await client.post(
                f"{_TMF}/promotion",
                json={
                    "promotionId": pid,
                    "discountType": "percent",
                    "discountValue": "10",
                    "durationKind": "single",
                    "audience": "targeted",
                },
            )
            r = await client.post(
                f"{_TMF}/promotion/{pid}/assign",
                json={"customerIds": ["CUST-1", "CUST-2"]},
            )
            assert r.status_code == 200, r.text
            assert set(r.json()["eligible"]) == {"CUST-1", "CUST-2"}
        finally:
            await _cleanup(settings, pid)


class TestPortalReads:
    async def test_preview_returns_money_as_strings(self, client, settings):
        pid = _pid()
        code = f"RT_{uuid.uuid4().hex[:6].upper()}"
        try:
            await client.post(
                f"{_TMF}/promotion",
                json={
                    "promotionId": pid,
                    "discountType": "percent",
                    "discountValue": "20",
                    "durationKind": "single",
                    "code": code,
                    "promoCodeKind": "multi_use",
                },
            )
            r = await client.get("/promo/preview", params={"code": code, "offering": "PLAN_M"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["valid"] is True
            assert body["label"] == "20% off"
            assert isinstance(body["base"], str)
            assert isinstance(body["effective"], str)
        finally:
            await _cleanup(settings, pid)

    async def test_validate_returns_full_terms(self, client, settings):
        pid = _pid()
        code = f"RTV_{uuid.uuid4().hex[:6].upper()}"
        try:
            await client.post(
                f"{_TMF}/promotion",
                json={
                    "promotionId": pid,
                    "discountType": "percent",
                    "discountValue": "20",
                    "durationKind": "multi",
                    "periodsTotal": 3,
                    "code": code,
                    "promoCodeKind": "multi_use",
                },
            )
            r = await client.get("/promo/validate", params={"code": code, "offering": "PLAN_M"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["valid"] is True
            assert body["offerDefinitionId"] == f"OD_{pid}"
            assert body["discountType"] == "percent"
            assert body["periodsTotal"] == 3
            assert isinstance(body["effective"], str)
        finally:
            await _cleanup(settings, pid)

    async def test_preview_invalid_code_is_200_invalid(self, client):
        r = await client.get(
            "/promo/preview", params={"code": "NEVER", "offering": "PLAN_M"}
        )
        assert r.status_code == 200
        assert r.json()["valid"] is False
        assert r.json()["reason"] == "unknown_code"

    async def _create_targeted_and_assign(self, client, pid, value, customer):
        await client.post(
            f"{_TMF}/promotion",
            json={
                "promotionId": pid,
                "discountType": "percent",
                "discountValue": value,
                "durationKind": "single",
                "audience": "targeted",
            },
        )
        await client.post(
            f"{_TMF}/promotion/{pid}/assign", json={"customerIds": [customer]}
        )

    async def test_resolve_eligible_returns_code_and_terms(self, client, settings):
        pid = _pid()
        try:
            await self._create_targeted_and_assign(client, pid, "15", "CUST-1")
            r = await client.get(
                "/promo/resolve-eligible", params={"customerId": "CUST-1", "offering": "PLAN_M"}
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["valid"] is True
            assert body["code"] == pid  # COM claims by this code
            assert body["promotionId"] == pid
            assert body["discountPeriodsTotal"] == 1
        finally:
            await _cleanup(settings, pid)

    async def test_resolve_eligible_none_for_non_eligible(self, client):
        r = await client.get(
            "/promo/resolve-eligible", params={"customerId": "CUST-NONE", "offering": "PLAN_M"}
        )
        assert r.status_code == 200
        assert r.json()["valid"] is False

    async def test_targeted_code_rejected_for_non_eligible(self, client, settings):
        # a leaked targeted code typed by a non-eligible customer → invalid
        pid = _pid()
        try:
            await self._create_targeted_and_assign(client, pid, "20", "CUST-1")
            r = await client.get(
                "/promo/validate",
                params={"code": pid, "offering": "PLAN_M", "customerId": "CUST-OTHER"},
            )
            assert r.status_code == 200
            assert r.json()["valid"] is False
            assert r.json()["reason"] == "not_eligible"
            # but valid for the eligible customer
            r2 = await client.get(
                "/promo/validate",
                params={"code": pid, "offering": "PLAN_M", "customerId": "CUST-1"},
            )
            assert r2.json()["valid"] is True
        finally:
            await _cleanup(settings, pid)

    async def test_customer_offers_lists_eligible(self, client, settings):
        pid = _pid()
        try:
            await self._create_targeted_and_assign(client, pid, "30", "CUST-1")
            r = await client.get("/promo/customer-offers", params={"customerId": "CUST-1"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["customerId"] == "CUST-1"
            assert body["offers"][0]["promotion"]["label"] == "30% off"
        finally:
            await _cleanup(settings, pid)
