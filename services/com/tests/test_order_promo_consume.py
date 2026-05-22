"""v1.1 — consume lifecycle in handle_service_order_completed.

claim (typed code) / advance_to_claimed (assigned) BEFORE subscription.create;
redeem on success; revoke on a create failure (payment decline). Builds an
in_progress order via the API (stamping the discount intent), then drives the
completion handler directly with a same-session OrderService + mocks.
"""

from unittest.mock import AsyncMock

import pytest
from bss_clients import PolicyViolationFromServer
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.order_repo import OrderRepository
from app.services.order_service import OrderService

TMF = "/tmf-api/productOrderingManagement/v4"

_VALID_CODE_TERMS = {
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

_VALID_ASSIGNED = {
    "valid": True,
    "offerId": "OFF_VIP_CUST-0001",
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


async def _inprogress_order(client, **create_extra) -> str:
    r = await client.post(
        f"{TMF}/productOrder",
        json={"customerId": "CUST-0001", "offeringId": "PLAN_M", **create_extra},
    )
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    s = await client.post(f"{TMF}/productOrder/{oid}/submit")
    assert s.status_code == 200, s.text
    return oid


def _handler_service(db_session: AsyncSession, mock_clients) -> OrderService:
    return OrderService(
        session=db_session,
        repo=OrderRepository(db_session),
        crm_client=None,
        catalog_client=None,
        payment_client=None,
        som_client=None,
        subscription_client=mock_clients["subscription"],
        loyalty_client=mock_clients["loyalty"],
        exchange=None,
    )


async def _complete(svc, oid):
    await svc.handle_service_order_completed(
        commercial_order_id=oid,
        customer_id="CUST-0001",
        offering_id="PLAN_M",
        msisdn="90000042",
        iccid="8910000000000042",
        payment_method_id="PM-0001",
        cfs_service_id="SVC-1",
    )


class TestNonTargetedConsume:
    @pytest.mark.asyncio
    async def test_claim_then_redeem_and_discount_on_snapshot(
        self, client, mock_clients, db_session
    ):
        mock_clients["catalog"].validate_promo = AsyncMock(return_value=_VALID_CODE_TERMS)
        oid = await _inprogress_order(client, discountCode="SUMMER")

        svc = _handler_service(db_session, mock_clients)
        await _complete(svc, oid)

        # claimed from the code, with the order id as idempotency key
        claim = mock_clients["loyalty"].claim_offer
        claim.assert_awaited_once()
        assert claim.await_args.kwargs["source"] == {"type": "promo_code", "code": "SUMMER"}
        assert claim.await_args.kwargs["idempotency_key"] == oid
        # discount terms forwarded to subscription.create
        snap = mock_clients["subscription"].create.await_args.kwargs["price_snapshot"]
        assert snap["discountType"] == "percent"
        assert snap["discountPeriodsTotal"] == 3
        assert snap["promoCode"] == "SUMMER"
        # redeemed on success, not revoked
        mock_clients["loyalty"].redeem_offer.assert_awaited_once()
        mock_clients["loyalty"].revoke_offer.assert_not_awaited()


class TestTargetedConsume:
    @pytest.mark.asyncio
    async def test_advance_to_claimed_for_assigned_offer(
        self, client, mock_clients, db_session
    ):
        mock_clients["catalog"].resolve_assigned_offer = AsyncMock(return_value=_VALID_ASSIGNED)
        oid = await _inprogress_order(client)  # no typed code → discovery

        svc = _handler_service(db_session, mock_clients)
        await _complete(svc, oid)

        # assigned offer advances, not claimed-from-code
        mock_clients["loyalty"].advance_offer_to_claimed.assert_awaited_once()
        assert (
            mock_clients["loyalty"].advance_offer_to_claimed.await_args.kwargs["offer_id"]
            == "OFF_VIP_CUST-0001"
        )
        mock_clients["loyalty"].claim_offer.assert_not_awaited()
        mock_clients["loyalty"].redeem_offer.assert_awaited_once()


class TestDeclineRevokes:
    @pytest.mark.asyncio
    async def test_payment_decline_revokes_entitlement(
        self, client, mock_clients, db_session
    ):
        mock_clients["catalog"].validate_promo = AsyncMock(return_value=_VALID_CODE_TERMS)
        oid = await _inprogress_order(client, discountCode="SUMMER")

        # subscription.create declines after the claim
        mock_clients["subscription"].create = AsyncMock(
            side_effect=PolicyViolationFromServer(
                rule="subscription.create.requires_payment_success",
                message="declined",
            )
        )
        svc = _handler_service(db_session, mock_clients)
        with pytest.raises(PolicyViolationFromServer):
            await _complete(svc, oid)

        mock_clients["loyalty"].claim_offer.assert_awaited_once()
        revoke = mock_clients["loyalty"].revoke_offer
        revoke.assert_awaited_once()
        assert revoke.await_args.kwargs["reason"] == "order_cancelled"
        mock_clients["loyalty"].redeem_offer.assert_not_awaited()


class TestNoPromoUnaffected:
    @pytest.mark.asyncio
    async def test_no_discount_skips_loyalty(self, client, mock_clients, db_session):
        oid = await _inprogress_order(client)  # no promo (mocks default to invalid)

        svc = _handler_service(db_session, mock_clients)
        await _complete(svc, oid)

        mock_clients["loyalty"].claim_offer.assert_not_awaited()
        mock_clients["loyalty"].advance_offer_to_claimed.assert_not_awaited()
        mock_clients["loyalty"].redeem_offer.assert_not_awaited()
