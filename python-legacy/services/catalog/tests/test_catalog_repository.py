"""Repository tests against seeded catalog data.

Expects: make migrate && make seed
"""

import pytest
from bss_catalog.repository import CatalogRepository


@pytest.fixture
def repo(db_session):
    return CatalogRepository(db_session)


SEED_PLANS = {"PLAN_S", "PLAN_M", "PLAN_L"}


class TestListOfferings:
    async def test_includes_seed_plans(self, repo: CatalogRepository):
        # The canonical seed ships PLAN_S/M/L, but a deployment may carry
        # additional offerings (#36 — the catalog renders every active
        # offering, not a hardcoded set). Assert the seed plans are present
        # rather than an exact count, so the test stays green whether the
        # target DB has only the seed or has been extended.
        offerings = await repo.list_offerings()
        ids = {o.id for o in offerings}
        assert SEED_PLANS <= ids

    async def test_filter_by_lifecycle_status(self, repo: CatalogRepository):
        active = await repo.list_offerings(lifecycle_status="active")
        # Filter correctness: everything returned is active, and the seed
        # plans (all active) are among them.
        assert all(o.lifecycle_status == "active" for o in active)
        assert SEED_PLANS <= {o.id for o in active}
        retired = await repo.list_offerings(lifecycle_status="retired")
        assert all(o.lifecycle_status == "retired" for o in retired)
        assert SEED_PLANS.isdisjoint({o.id for o in retired})

    async def test_limit_and_offset(self, repo: CatalogRepository):
        first = await repo.list_offerings(limit=1, offset=0)
        assert len(first) == 1
        second = await repo.list_offerings(limit=1, offset=1)
        assert len(second) == 1
        assert first[0].id != second[0].id

    async def test_offerings_have_prices_loaded(self, repo: CatalogRepository):
        offerings = await repo.list_offerings()
        for o in offerings:
            assert len(o.prices) >= 1

    async def test_offerings_have_allowances_loaded(self, repo: CatalogRepository):
        offerings = await repo.list_offerings()
        for o in offerings:
            assert len(o.allowances) >= 1


class TestGetOffering:
    async def test_existing_offering(self, repo: CatalogRepository):
        offering = await repo.get_offering("PLAN_S")
        assert offering is not None
        assert offering.name == "Lite"
        assert offering.is_bundle is True
        assert offering.lifecycle_status == "active"

    async def test_offering_not_found(self, repo: CatalogRepository):
        offering = await repo.get_offering("DOES_NOT_EXIST")
        assert offering is None

    async def test_offering_has_specification(self, repo: CatalogRepository):
        offering = await repo.get_offering("PLAN_S")
        assert offering is not None
        assert offering.specification is not None
        assert offering.specification.id == "SPEC_MOBILE_PREPAID"

    async def test_plan_s_allowances(self, repo: CatalogRepository):
        offering = await repo.get_offering("PLAN_S")
        assert offering is not None
        allowance_map = {a.allowance_type: a for a in offering.allowances}
        assert allowance_map["data"].quantity == 5120
        assert allowance_map["voice"].quantity == 100
        assert allowance_map["sms"].quantity == 100


class TestListSpecifications:
    async def test_returns_one_spec(self, repo: CatalogRepository):
        specs = await repo.list_specifications()
        assert len(specs) == 1
        assert specs[0].id == "SPEC_MOBILE_PREPAID"


class TestGetSpecification:
    async def test_existing_spec(self, repo: CatalogRepository):
        spec = await repo.get_specification("SPEC_MOBILE_PREPAID")
        assert spec is not None
        assert spec.name == "Mobile Prepaid Bundle"
        assert spec.brand == "BSS-CLI"

    async def test_spec_not_found(self, repo: CatalogRepository):
        spec = await repo.get_specification("DOES_NOT_EXIST")
        assert spec is None


class TestListVasOfferings:
    async def test_returns_four_vas(self, repo: CatalogRepository):
        # v0.17 added VAS_ROAMING_1GB to the seed → 4 offerings.
        vas = await repo.list_vas_offerings()
        assert len(vas) == 4
        ids = {v.id for v in vas}
        assert ids == {
            "VAS_DATA_1GB",
            "VAS_DATA_5GB",
            "VAS_UNLIMITED_DAY",
            "VAS_ROAMING_1GB",
        }


class TestGetVasOffering:
    async def test_existing_vas(self, repo: CatalogRepository):
        vas = await repo.get_vas_offering("VAS_DATA_1GB")
        assert vas is not None
        assert vas.name == "Data Top-Up 1GB"
        assert float(vas.price_amount) == 3.00

    async def test_vas_not_found(self, repo: CatalogRepository):
        vas = await repo.get_vas_offering("DOES_NOT_EXIST")
        assert vas is None
