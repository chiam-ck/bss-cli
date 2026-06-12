"""v0.7 — CatalogClient active-aware methods."""

from datetime import datetime, timezone

import pytest
import respx
from bss_clients import CatalogClient, PolicyViolationFromServer
from httpx import Response

BASE_URL = "http://catalog:8000"


@pytest.fixture
def client():
    return CatalogClient(base_url=BASE_URL)


class TestListActiveOfferings:
    @pytest.mark.asyncio
    @respx.mock
    async def test_explicit_at_passes_iso(self, client):
        moment = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)
        route = respx.get(
            f"{BASE_URL}/tmf-api/productCatalogManagement/v4/productOffering"
        ).mock(return_value=Response(200, json=[]))
        await client.list_active_offerings(at=moment)
        called = route.calls.last.request
        assert "activeAt=2026-02-15T12%3A00%3A00%2B00%3A00" in str(called.url)

    @pytest.mark.asyncio
    @respx.mock
    async def test_default_at_sends_clock_now(self, client):
        route = respx.get(
            f"{BASE_URL}/tmf-api/productCatalogManagement/v4/productOffering"
        ).mock(return_value=Response(200, json=[]))
        await client.list_active_offerings()
        # bss_clock.now() is called — we don't assert the exact value, just that
        # an activeAt parameter was sent.
        assert "activeAt=" in str(route.calls.last.request.url)


class TestGetActivePrice:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_price_dict(self, client):
        respx.get(
            f"{BASE_URL}/tmf-api/productCatalogManagement/v4/productOfferingPrice/active/PLAN_M"
        ).mock(return_value=Response(200, json={"id": "PRICE_PLAN_M"}))
        result = await client.get_active_price("PLAN_M")
        assert result["id"] == "PRICE_PLAN_M"

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_active_row_raises_policy_violation(self, client):
        body = {
            "code": "POLICY_VIOLATION",
            "reason": "catalog.price.no_active_row",
            "message": "No active recurring price for offering PLAN_X",
            "context": {"offering_id": "PLAN_X"},
        }
        respx.get(
            f"{BASE_URL}/tmf-api/productCatalogManagement/v4/productOfferingPrice/active/PLAN_X"
        ).mock(return_value=Response(422, json=body))
        with pytest.raises(PolicyViolationFromServer) as exc_info:
            await client.get_active_price("PLAN_X")
        assert exc_info.value.rule == "catalog.price.no_active_row"


class TestGetOfferingPriceById:
    @pytest.mark.asyncio
    @respx.mock
    async def test_direct_lookup(self, client):
        respx.get(
            f"{BASE_URL}/tmf-api/productCatalogManagement/v4/productOfferingPrice/PRICE_PLAN_M"
        ).mock(return_value=Response(200, json={"id": "PRICE_PLAN_M"}))
        result = await client.get_offering_price("PRICE_PLAN_M")
        assert result["id"] == "PRICE_PLAN_M"


# ── v1.1 — promotion methods ───────────────────────────────────────────────

_PROMO = f"{BASE_URL}/tmf-api/promotionManagement/v4/promotion"


class TestPromotionWrites:
    @pytest.mark.asyncio
    @respx.mock
    async def test_create_promotion_posts_camel_body(self, client):
        route = respx.post(_PROMO).mock(
            return_value=Response(201, json={"id": "PROMO_X", "state": "active"})
        )
        await client.create_promotion(
            promotion_id="PROMO_X",
            discount_type="percent",
            discount_value="20",
            duration_kind="multi",
            periods_total=3,
            code="SUMMER",
            promo_code_kind="multi_use",
            applicable_offering_ids=["PLAN_M"],
        )
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert body == {
            "promotionId": "PROMO_X",
            "discountType": "percent",
            "discountValue": "20",
            "durationKind": "multi",
            "audience": "public",
            "currency": "SGD",
            "code": "SUMMER",
            "promoCodeKind": "multi_use",
            "applicableOfferingIds": ["PLAN_M"],
            "periodsTotal": 3,
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_promotion_422_raises_policy_violation(self, client):
        respx.post(_PROMO).mock(
            return_value=Response(
                422,
                json={
                    "code": "POLICY_VIOLATION",
                    "reason": "catalog.promotion.loyalty_refused",
                    "message": "loyalty refused",
                    "context": {},
                },
            )
        )
        with pytest.raises(PolicyViolationFromServer) as exc:
            await client.create_promotion(
                promotion_id="PROMO_Y",
                discount_type="percent",
                discount_value="20",
                duration_kind="single",
            )
        assert exc.value.rule == "catalog.promotion.loyalty_refused"

    @pytest.mark.asyncio
    @respx.mock
    async def test_assign_promotion(self, client):
        route = respx.post(f"{_PROMO}/PROMO_X/assign").mock(
            return_value=Response(200, json={"issued": ["CUST-1"], "skipped": []})
        )
        await client.assign_promotion("PROMO_X", customer_ids=["CUST-1"])
        import json as _json

        assert _json.loads(route.calls.last.request.content) == {"customerIds": ["CUST-1"]}


class TestPromotionReads:
    @pytest.mark.asyncio
    @respx.mock
    async def test_validate_promo_passes_query(self, client):
        route = respx.get("http://catalog:8000/promo/validate").mock(
            return_value=Response(200, json={"valid": True, "effective": "20.00"})
        )
        r = await client.validate_promo(code="SUMMER", offering="PLAN_M")
        assert r["valid"] is True
        url = str(route.calls.last.request.url)
        assert "code=SUMMER" in url and "offering=PLAN_M" in url

    @pytest.mark.asyncio
    @respx.mock
    async def test_preview_promo(self, client):
        respx.get("http://catalog:8000/promo/preview").mock(
            return_value=Response(200, json={"valid": True, "label": "20% off"})
        )
        r = await client.preview_promo(code="SUMMER", offering="PLAN_M")
        assert r["label"] == "20% off"

    @pytest.mark.asyncio
    @respx.mock
    async def test_list_customer_offers(self, client):
        route = respx.get("http://catalog:8000/promo/customer-offers").mock(
            return_value=Response(200, json={"customerId": "CUST-1", "offers": []})
        )
        await client.list_customer_offers(customer_id="CUST-1", state="issued")
        url = str(route.calls.last.request.url)
        assert "customerId=CUST-1" in url and "state=issued" in url

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_and_list_promotions(self, client):
        respx.get(f"{_PROMO}/PROMO_X").mock(return_value=Response(200, json={"id": "PROMO_X"}))
        respx.get(_PROMO).mock(return_value=Response(200, json=[{"id": "PROMO_X"}]))
        assert (await client.get_promotion("PROMO_X"))["id"] == "PROMO_X"
        assert (await client.list_promotions(state="active"))[0]["id"] == "PROMO_X"
