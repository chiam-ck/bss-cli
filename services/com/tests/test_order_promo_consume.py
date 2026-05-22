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

_VALID_ELIGIBLE = {
    "valid": True,
    "code": "PROMO_VIP",  # v1.1.1 — targeted resolves to a CODE now
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
        assert claim.await_args.kwargs["idempotency_key"] == f"{oid}:claim"
        # discount terms forwarded to subscription.create
        snap = mock_clients["subscription"].create.await_args.kwargs["price_snapshot"]
        assert snap["discountType"] == "percent"
        assert snap["discountPeriodsTotal"] == 3
        assert snap["promoCode"] == "SUMMER"
        # redeemed on success, not revoked
        mock_clients["loyalty"].redeem_offer.assert_awaited_once()
        mock_clients["loyalty"].revoke_offer.assert_not_awaited()
        # regression guard: claim + redeem must use DISTINCT idempotency keys
        # (loyalty dedupes on (actor,key) without the tool name).
        redeem_key = mock_clients["loyalty"].redeem_offer.await_args.kwargs["idempotency_key"]
        assert redeem_key != claim.await_args.kwargs["idempotency_key"]


class TestTargetedConsume:
    @pytest.mark.asyncio
    async def test_targeted_claims_by_code(self, client, mock_clients, db_session):
        # v1.1.1 — targeted resolves to a code; consume is the SAME claim-by-code
        # path as a typed code (no offer.issue/advance anymore).
        mock_clients["catalog"].resolve_eligible_promo = AsyncMock(return_value=_VALID_ELIGIBLE)
        oid = await _inprogress_order(client)  # no typed code → eligibility discovery

        svc = _handler_service(db_session, mock_clients)
        await _complete(svc, oid)

        claim = mock_clients["loyalty"].claim_offer
        claim.assert_awaited_once()
        # claimed by the targeted promo's code
        assert claim.await_args.kwargs["source"] == {"type": "promo_code", "code": "PROMO_VIP"}
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
        mock_clients["loyalty"].redeem_offer.assert_not_awaited()


class TestClaimFailureDegradesToFullPrice:
    """v1.1.3 regression — an exhausted/refused promo code must NOT brick an
    order that has already cleared KYC + payment. The claim raises
    ``promo_code.consume.illegal_state`` (e.g. an exhausted shared code); the
    order must still complete, at FULL price, with the discount dropped from the
    snapshot and nothing left to redeem/revoke. Previously this propagated and
    left the order stuck ``in_progress`` forever (no subscription)."""

    @pytest.mark.asyncio
    async def test_exhausted_code_claim_refusal_completes_at_full_price(
        self, client, mock_clients, db_session
    ):
        mock_clients["catalog"].validate_promo = AsyncMock(return_value=_VALID_CODE_TERMS)
        oid = await _inprogress_order(client, discountCode="SUMMER")

        # The loyalty offer is already exhausted → claim refused at activation.
        mock_clients["loyalty"].claim_offer = AsyncMock(
            side_effect=PolicyViolationFromServer(
                rule="promo_code.consume.illegal_state",
                message="Illegal transition exhausted -> exhausted",
            )
        )

        svc = _handler_service(db_session, mock_clients)
        # Must NOT raise — the order completes despite the promo failure.
        await _complete(svc, oid)

        # Subscription created (order proceeded) at FULL price: no discount terms
        # rode onto the snapshot because nothing was claimed.
        create = mock_clients["subscription"].create
        create.assert_awaited_once()
        snap = create.await_args.kwargs.get("price_snapshot") or {}
        assert "discountType" not in snap
        assert "promoCode" not in snap

        # Nothing was claimed → nothing to redeem or revoke.
        mock_clients["loyalty"].redeem_offer.assert_not_awaited()
        mock_clients["loyalty"].revoke_offer.assert_not_awaited()

        # The order reached a terminal completed state (not stranded in_progress).
        order = await OrderRepository(db_session).get(oid)
        assert order.state == "completed"
