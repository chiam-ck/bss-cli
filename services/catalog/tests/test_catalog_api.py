"""API tests — httpx AsyncClient against the ASGI app.

Asserts TMF620 schema shape (camelCase field names) and correct content
from seeded data.
"""

from bss_models import BSS_RELEASE
from httpx import AsyncClient


class TestHealth:
    async def test_health(self, client: AsyncClient):
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "catalog"
        # v0.18.1 — service version is sourced from BSS_RELEASE.
        assert body["version"] == BSS_RELEASE

    async def test_ready(self, client: AsyncClient):
        r = await client.get("/ready")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"

    async def test_request_id_header(self, client: AsyncClient):
        r = await client.get("/health")
        assert "x-request-id" in r.headers


class TestProductOffering:
    async def test_list_offerings(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productOffering")
        assert r.status_code == 200
        offerings = r.json()
        # Seed plans present; a deployment may add more (#36).
        ids = {o["id"] for o in offerings}
        assert {"PLAN_S", "PLAN_M", "PLAN_L"} <= ids

        # TMF620 camelCase field names — assert shape on a known seed plan
        # (list ordering isn't guaranteed, so don't index [0]).
        plan_s = next(o for o in offerings if o["id"] == "PLAN_S")
        assert "isBundle" in plan_s
        assert "isSellable" in plan_s
        assert "lifecycleStatus" in plan_s
        assert "productOfferingPrice" in plan_s
        assert "bundleAllowance" in plan_s
        assert "@type" in plan_s
        assert plan_s["@type"] == "ProductOffering"

    async def test_list_offerings_has_spec_ref(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productOffering")
        first = r.json()[0]
        spec = first["productSpecification"]
        assert spec is not None
        assert spec["@type"] == "ProductSpecificationRef"
        assert "href" in spec

    async def test_list_offerings_has_prices(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productOffering")
        for offering in r.json():
            assert len(offering["productOfferingPrice"]) >= 1
            price = offering["productOfferingPrice"][0]
            assert "priceType" in price
            assert "price" in price
            assert "taxIncludedAmount" in price["price"]
            assert price["@type"] == "ProductOfferingPrice"

    async def test_list_offerings_has_allowances(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productOffering")
        for offering in r.json():
            assert len(offering["bundleAllowance"]) >= 1
            allowance = offering["bundleAllowance"][0]
            assert "allowanceType" in allowance
            assert "quantity" in allowance
            assert "unit" in allowance

    async def test_list_offerings_filter_lifecycle(self, client: AsyncClient):
        r = await client.get(
            "/tmf-api/productCatalogManagement/v4/productOffering",
            params={"lifecycleStatus": "retired"},
        )
        assert r.status_code == 200
        assert len(r.json()) == 0

    async def test_get_offering_by_id(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productOffering/PLAN_S")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "PLAN_S"
        assert body["name"] == "Lite"
        assert body["isBundle"] is True
        assert body["lifecycleStatus"] == "active"
        assert body["@type"] == "ProductOffering"

    async def test_get_offering_not_found(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productOffering/DOES_NOT_EXIST")
        assert r.status_code == 404

    async def test_get_offering_plan_s_price(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productOffering/PLAN_S")
        body = r.json()
        price = body["productOfferingPrice"][0]
        assert price["priceType"] == "recurring"
        assert price["recurringChargePeriodLength"] == 1
        assert price["recurringChargePeriodType"] == "month"
        assert price["price"]["taxIncludedAmount"]["value"] == 10.0
        assert price["price"]["taxIncludedAmount"]["unit"] == "SGD"

    async def test_get_offering_plan_s_allowances(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productOffering/PLAN_S")
        allowances = r.json()["bundleAllowance"]
        by_type = {a["allowanceType"]: a for a in allowances}
        assert by_type["data"]["quantity"] == 5120
        assert by_type["voice"]["quantity"] == 100
        assert by_type["sms"]["quantity"] == 100


class TestProductSpecification:
    async def test_list_specifications(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productSpecification")
        assert r.status_code == 200
        specs = r.json()
        assert len(specs) == 1
        first = specs[0]
        assert first["id"] == "SPEC_MOBILE_PREPAID"
        assert "lifecycleStatus" in first
        assert first["@type"] == "ProductSpecification"
        assert "href" in first

    async def test_get_specification(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productSpecification/SPEC_MOBILE_PREPAID")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Mobile Prepaid Bundle"
        assert body["brand"] == "BSS-CLI"
        assert body["@type"] == "ProductSpecification"

    async def test_get_specification_not_found(self, client: AsyncClient):
        r = await client.get("/tmf-api/productCatalogManagement/v4/productSpecification/DOES_NOT_EXIST")
        assert r.status_code == 404


class TestVas:
    async def test_list_vas_offerings(self, client: AsyncClient):
        r = await client.get("/vas/offering")
        assert r.status_code == 200
        vas = r.json()
        # v0.17 added VAS_ROAMING_1GB → 4 seeded VAS offerings.
        assert len(vas) == 4

        first = vas[0]
        assert "priceAmount" in first
        assert "currency" in first
        assert "allowanceType" in first

    async def test_get_vas_offering(self, client: AsyncClient):
        r = await client.get("/vas/offering/VAS_DATA_1GB")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Data Top-Up 1GB"
        assert body["priceAmount"] == 3.0
        assert body["currency"] == "SGD"

    async def test_get_vas_offering_not_found(self, client: AsyncClient):
        r = await client.get("/vas/offering/DOES_NOT_EXIST")
        assert r.status_code == 404
