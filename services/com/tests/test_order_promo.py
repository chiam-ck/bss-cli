"""v1.1 — promo discount intent stamped on the order item at create time.

create_order resolves a discount through the catalog client (typed code →
validate_promo; otherwise eligible-promo discovery → resolve_eligible_promo)
and stamps it as INTENT. An invalid/absent promo never blocks the order.
"""

from unittest.mock import AsyncMock

TMF = "/tmf-api/productOrderingManagement/v4"


async def _create(client, **extra):
    body = {"customerId": "CUST-0001", "offeringId": "PLAN_M", **extra}
    return await client.post(f"{TMF}/productOrder", json=body)


class TestNonTargetedCode:
    async def test_valid_code_stamps_discount_intent(self, client, mock_clients):
        mock_clients["catalog"].validate_promo = AsyncMock(
            return_value={
                "valid": True,
                "offerDefinitionId": "OD_PROMO_SUMMER",
                "discountType": "percent",
                "discountValue": "20",
                "durationKind": "multi",
                "periodsTotal": 3,
                "discountPeriodsTotal": 3,
                "base": "25.00",
                "effective": "20.00",
                "label": "20% off",
            }
        )
        r = await _create(client, discountCode="SUMMER")
        assert r.status_code == 201, r.text
        item = r.json()["items"][0]
        assert item["discountCode"] == "SUMMER"
        assert item["promoOfferDefinitionId"] == "OD_PROMO_SUMMER"
        assert item["discountType"] == "percent"
        assert item["discountPeriodsTotal"] == 3
        # a typed code's loyalty offer is created at claim, not now
        assert item["promoOfferId"] is None
        mock_clients["catalog"].validate_promo.assert_awaited_once()

    async def test_invalid_code_proceeds_at_full_price(self, client, mock_clients):
        # invalid code AND no assigned offer (mock default) → full price
        mock_clients["catalog"].validate_promo = AsyncMock(
            return_value={"valid": False, "reason": "expired"}
        )
        r = await _create(client, discountCode="OLDCODE")
        assert r.status_code == 201
        item = r.json()["items"][0]
        assert item["discountCode"] is None
        assert item["discountType"] is None

    async def test_invalid_code_falls_back_to_eligible_promo(self, client, mock_clients):
        # a typo shouldn't cost the customer their auto-applied (eligible) promo
        mock_clients["catalog"].validate_promo = AsyncMock(
            return_value={"valid": False, "reason": "promo_code.not_found"}
        )
        mock_clients["catalog"].resolve_eligible_promo = AsyncMock(
            return_value={
                "valid": True, "code": "VIP_DEMO", "offerDefinitionId": "OD_VIP",
                "discountType": "percent", "discountValue": "15",
                "discountPeriodsTotal": 1, "base": "25.00", "effective": "21.25",
            }
        )
        r = await _create(client, discountCode="TYPOO")
        assert r.status_code == 201
        item = r.json()["items"][0]
        # fell back to the eligible promo → its CODE is stamped (claimed at activation)
        assert item["discountCode"] == "VIP_DEMO"
        assert item["promoOfferId"] is None  # set at claim, not create
        assert item["discountType"] == "percent"

    async def test_valid_code_overrides_eligible_promo(self, client, mock_clients):
        mock_clients["catalog"].validate_promo = AsyncMock(
            return_value={
                "valid": True, "offerDefinitionId": "OD_SUMMER",
                "discountType": "percent", "discountValue": "30",
                "discountPeriodsTotal": 1, "base": "25.00", "effective": "17.50",
            }
        )
        mock_clients["catalog"].resolve_eligible_promo = AsyncMock(
            return_value={"valid": True, "code": "VIP", "discountType": "percent",
                          "discountValue": "15", "discountPeriodsTotal": 1}
        )
        r = await _create(client, discountCode="SUMMER")
        assert r.status_code == 201
        item = r.json()["items"][0]
        assert item["discountCode"] == "SUMMER"   # typed code stamped
        # typed code won → eligibility discovery never consulted
        mock_clients["catalog"].resolve_eligible_promo.assert_not_awaited()


class TestTargetedEligiblePromo:
    async def test_eligible_promo_stamps_its_code(self, client, mock_clients):
        mock_clients["catalog"].resolve_eligible_promo = AsyncMock(
            return_value={
                "valid": True,
                "code": "PROMO_VIP",
                "promotionId": "PROMO_VIP",
                "offerDefinitionId": "OD_PROMO_VIP",
                "discountType": "percent",
                "discountValue": "15",
                "durationKind": "single",
                "periodsTotal": None,
                "discountPeriodsTotal": 1,
                "base": "25.00",
                "effective": "21.25",
                "label": "15% off",
            }
        )
        r = await _create(client)  # no typed code → eligibility discovery
        assert r.status_code == 201, r.text
        item = r.json()["items"][0]
        # the targeted promo's CODE is stamped (claimed at activation, same as typed)
        assert item["discountCode"] == "PROMO_VIP"
        assert item["promoOfferDefinitionId"] == "OD_PROMO_VIP"
        assert item["promoOfferId"] is None  # set at claim
        assert item["discountPeriodsTotal"] == 1
        # typed-code path must NOT be consulted when no code is supplied
        mock_clients["catalog"].validate_promo.assert_not_awaited()


class TestOptOutOfAssignedOffer:
    async def test_skip_assigned_offer_ignores_auto_apply(self, client, mock_clients):
        # customer IS eligible for a targeted promo...
        mock_clients["catalog"].resolve_eligible_promo = AsyncMock(
            return_value={
                "valid": True, "code": "VIP", "offerDefinitionId": "OD_VIP",
                "discountType": "percent", "discountValue": "15",
                "discountPeriodsTotal": 1, "base": "25.00", "effective": "21.25",
            }
        )
        # ...but they opted out → no discount stamped, and discovery isn't consulted
        r = await _create(client, skipAssignedOffer=True)
        assert r.status_code == 201
        item = r.json()["items"][0]
        assert item["discountType"] is None
        assert item["discountCode"] is None
        mock_clients["catalog"].resolve_eligible_promo.assert_not_awaited()


class TestPromoNeverBlocksOrder:
    async def test_catalog_error_degrades_to_no_discount(self, client, mock_clients):
        from bss_clients import ServerError

        mock_clients["catalog"].resolve_eligible_promo = AsyncMock(
            side_effect=ServerError(503, "catalog down")
        )
        r = await _create(client)
        assert r.status_code == 201
        item = r.json()["items"][0]
        assert item["discountType"] is None
