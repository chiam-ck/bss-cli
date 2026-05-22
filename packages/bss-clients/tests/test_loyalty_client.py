"""v1.1 — BearerAuthProvider + LoyaltyClient (loyalty-cli adapter).

Contract pinned against loyalty-cli :8080 (verified 2026-05-22):
- POST /v1/tools/<name>, body = tool args
- Bearer auth, plus Idempotency-Key + X-Actor-Id (+ X-Actor-Roles)
- refusal envelope: 422 {"detail": {"refused": true, "code", "detail"}}
"""

import uuid

import pytest
import respx
from httpx import Response

from bss_clients import (
    AuthProvider,
    BearerAuthProvider,
    LoyaltyClient,
    PolicyViolationFromServer,
    set_context,
)
from bss_clients.loyalty import REVOKE_ORDER_CANCELLED

BASE_URL = "http://loyalty-http:8080"
TOKEN = "70dd7cbe00ca350111a8ca7731f8423c4eecc69bb7b37c7b4344cc040155d4f9"


@pytest.fixture
def client():
    return LoyaltyClient(base_url=BASE_URL, auth_provider=BearerAuthProvider(TOKEN))


# ─────────────────────────────────────────────────────────────────────────────
# BearerAuthProvider
# ─────────────────────────────────────────────────────────────────────────────


class TestBearerAuthProvider:
    @pytest.mark.asyncio
    async def test_returns_bearer_header(self):
        headers = await BearerAuthProvider(TOKEN).get_headers()
        assert headers == {"Authorization": f"Bearer {TOKEN}"}

    def test_implements_protocol(self):
        assert isinstance(BearerAuthProvider(TOKEN), AuthProvider)

    def test_empty_token_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            BearerAuthProvider("")

    @pytest.mark.asyncio
    async def test_returns_a_copy_each_call(self):
        provider = BearerAuthProvider(TOKEN)
        first = await provider.get_headers()
        first["Authorization"] = "tampered"
        second = await provider.get_headers()
        assert second["Authorization"] == f"Bearer {TOKEN}"


# ─────────────────────────────────────────────────────────────────────────────
# Transport: path, headers, auth
# ─────────────────────────────────────────────────────────────────────────────


class TestTransport:
    @pytest.mark.asyncio
    @respx.mock
    async def test_posts_to_v1_tools_path_with_body(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/promo_code.show").mock(
            return_value=Response(200, json={"offer_definition_id": "OD-1"})
        )
        result = await client.show_promo_code("SUMMER25")
        assert route.called
        req = route.calls.last.request
        import json as _json

        assert _json.loads(req.content) == {"code": "SUMMER25"}
        assert result["offer_definition_id"] == "OD-1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_sends_bearer_and_loyalty_headers(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/offer.list").mock(
            return_value=Response(200, json={"rows": []})
        )
        await client.list_offers(customer_id="CUST-001")
        req = route.calls.last.request
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        assert req.headers["x-actor-roles"] == "author,reviewer,publisher"
        assert req.headers["idempotency-key"]  # always present
        # loyalty uses its own auth model — BSS perimeter headers must NOT leak
        lower = {h.lower() for h in req.headers}
        assert "x-bss-api-token" not in lower

    @pytest.mark.asyncio
    @respx.mock
    async def test_maps_bss_actor_to_x_actor_id(self, client):
        set_context(actor="CSR-007", channel="cockpit", request_id="req-1")
        route = respx.post(f"{BASE_URL}/v1/tools/offer.list").mock(
            return_value=Response(200, json={"rows": []})
        )
        try:
            await client.list_offers()
        finally:
            set_context(actor="system", channel="system", request_id="")
        assert route.calls.last.request.headers["x-actor-id"] == "CSR-007"

    @pytest.mark.asyncio
    @respx.mock
    async def test_explicit_actor_overrides_context(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/offer.redeem").mock(
            return_value=Response(200, json={"offer_id": "OFF-1", "state": "redeemed"})
        )
        await client._call(
            "offer.redeem", {"offer_id": "OFF-1"}, idempotency_key="k", actor="OP-9"
        )
        assert route.calls.last.request.headers["x-actor-id"] == "OP-9"

    @pytest.mark.asyncio
    @respx.mock
    async def test_write_uses_caller_idempotency_key(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/offer.redeem").mock(
            return_value=Response(200, json={"state": "redeemed"})
        )
        await client.redeem_offer(
            offer_id="OFF-1", order_ref="ORD-014", idempotency_key="ORD-014"
        )
        assert route.calls.last.request.headers["idempotency-key"] == "ORD-014"

    @pytest.mark.asyncio
    @respx.mock
    async def test_read_mints_uuid_idempotency_key(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/promo_code.show").mock(
            return_value=Response(200, json={})
        )
        await client.show_promo_code("X")
        key = route.calls.last.request.headers["idempotency-key"]
        uuid.UUID(key)  # parses → it's a real uuid, not a fixed string


# ─────────────────────────────────────────────────────────────────────────────
# Refusal envelope → PolicyViolationFromServer
# ─────────────────────────────────────────────────────────────────────────────


class TestRefusalTranslation:
    @pytest.mark.asyncio
    @respx.mock
    async def test_422_refused_becomes_policy_violation(self, client):
        respx.post(f"{BASE_URL}/v1/tools/offer.claim").mock(
            return_value=Response(
                422,
                json={
                    "detail": {
                        "refused": True,
                        "code": "offer.claim.code_exhausted",
                        "detail": "promo code SUMMER25 has no remaining uses",
                    }
                },
            )
        )
        with pytest.raises(PolicyViolationFromServer) as exc:
            await client.claim_offer(
                customer_id="CUST-001",
                source={"type": "promo_code", "code": "SUMMER25"},
                idempotency_key="ORD-014",
            )
        assert exc.value.rule == "offer.claim.code_exhausted"
        assert "no remaining uses" in exc.value.detail
        assert exc.value.context["source"] == "loyalty"


# ─────────────────────────────────────────────────────────────────────────────
# Typed methods build the right loyalty bodies
# ─────────────────────────────────────────────────────────────────────────────


def _body(route):
    import json as _json

    return _json.loads(route.calls.last.request.content)


class TestTypedMethods:
    @pytest.mark.asyncio
    @respx.mock
    async def test_register_offer_definition(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/offer_definition.register").mock(
            return_value=Response(200, json={"id": "OD-PROMO_SUMMER25"})
        )
        await client.register_offer_definition(
            definition_id="OD-PROMO_SUMMER25",
            display_name="Summer 25% off",
            idempotency_key="PROMO_SUMMER25",
        )
        body = _body(route)
        assert body == {
            "id": "OD-PROMO_SUMMER25",
            "display_name": "Summer 25% off",
            "kind": "regular",
        }
        # saga step 2: idem key is the promotion id
        assert route.calls.last.request.headers["idempotency-key"] == "PROMO_SUMMER25"

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_promo_code(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/promo_code.register").mock(
            return_value=Response(200, json={"code": "SUMMER25"})
        )
        await client.register_promo_code(
            code="SUMMER25",
            offer_definition_id="OD-1",
            kind="multi_use",
            idempotency_key="PROMO_SUMMER25",
        )
        assert _body(route) == {
            "code": "SUMMER25",
            "offer_definition_id": "OD-1",
            "kind": "multi_use",
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_issue_offer(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/offer.issue").mock(
            return_value=Response(200, json={"offer_id": "OFF-1", "state": "issued"})
        )
        await client.issue_offer(
            offer_id="OFF-CUST001-SUMMER",
            offer_definition_id="OD-1",
            customer_id="CUST-001",
            source={"type": "campaign", "campaign_id": "CMP-VIP"},
            idempotency_key="OFF-CUST001-SUMMER",
        )
        assert _body(route) == {
            "offer_id": "OFF-CUST001-SUMMER",
            "offer_definition_id": "OD-1",
            "customer_id": "CUST-001",
            "source": {"type": "campaign", "campaign_id": "CMP-VIP"},
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_list_offers_omits_unset_filters(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/offer.list").mock(
            return_value=Response(200, json={"rows": []})
        )
        await client.list_offers(customer_id="CUST-001", state="issued")
        assert _body(route) == {"customer_id": "CUST-001", "state": "issued"}

    @pytest.mark.asyncio
    @respx.mock
    async def test_claim_offer(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/offer.claim").mock(
            return_value=Response(200, json={"offer_id": "OFF-9"})
        )
        await client.claim_offer(
            customer_id="CUST-001",
            source={"type": "promo_code", "code": "SUMMER25"},
            idempotency_key="ORD-014",
        )
        assert _body(route) == {
            "customer_id": "CUST-001",
            "source": {"type": "promo_code", "code": "SUMMER25"},
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_revoke_offer_uses_order_cancelled_reason(self, client):
        route = respx.post(f"{BASE_URL}/v1/tools/offer.revoke").mock(
            return_value=Response(200, json={"state": "revoked"})
        )
        await client.revoke_offer(
            offer_id="OFF-9",
            reason=REVOKE_ORDER_CANCELLED,
            idempotency_key="ORD-014",
            order_ref="ORD-014",
        )
        assert _body(route) == {
            "offer_id": "OFF-9",
            "reason": "order_cancelled",
            "order_ref": "ORD-014",
        }


class TestHealthz:
    @pytest.mark.asyncio
    @respx.mock
    async def test_healthz_true_on_200(self, client):
        respx.get(f"{BASE_URL}/healthz").mock(return_value=Response(200, json={}))
        assert await client.healthz() is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_healthz_false_on_503(self, client):
        respx.get(f"{BASE_URL}/healthz").mock(return_value=Response(503))
        assert await client.healthz() is False
