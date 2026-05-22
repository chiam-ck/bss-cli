"""v1.1 — promo discount intent stamped on the order item at create time.

create_order resolves a discount through the catalog client (typed code →
validate_promo; otherwise assigned-offer discovery → resolve_assigned_offer)
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
        mock_clients["catalog"].validate_promo = AsyncMock(
            return_value={"valid": False, "reason": "expired"}
        )
        r = await _create(client, discountCode="OLDCODE")
        assert r.status_code == 201
        item = r.json()["items"][0]
        assert item["discountCode"] is None
        assert item["discountType"] is None


class TestTargetedAssignedOffer:
    async def test_assigned_offer_stamps_offer_id(self, client, mock_clients):
        mock_clients["catalog"].resolve_assigned_offer = AsyncMock(
            return_value={
                "valid": True,
                "offerId": "OFF_PROMO_VIP_CUST-0001",
                "offerState": "issued",
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
        r = await _create(client)  # no typed code → discovery
        assert r.status_code == 201, r.text
        item = r.json()["items"][0]
        assert item["discountCode"] is None
        assert item["promoOfferId"] == "OFF_PROMO_VIP_CUST-0001"
        assert item["promoOfferDefinitionId"] == "OD_PROMO_VIP"
        assert item["discountPeriodsTotal"] == 1
        # typed-code path must NOT be consulted when no code is supplied
        mock_clients["catalog"].validate_promo.assert_not_awaited()


class TestPromoNeverBlocksOrder:
    async def test_catalog_error_degrades_to_no_discount(self, client, mock_clients):
        from bss_clients import ServerError

        mock_clients["catalog"].resolve_assigned_offer = AsyncMock(
            side_effect=ServerError(503, "catalog down")
        )
        r = await _create(client)
        assert r.status_code == 201
        item = r.json()["items"][0]
        assert item["discountType"] is None
