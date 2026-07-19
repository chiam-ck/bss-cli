"""LoyaltyClient — bss-clients adapter for chiam-ck/loyalty-cli (v1.1).

loyalty-cli is the entitlement engine behind BSS promotions. It ships
**unmodified**; BSS composes over its existing HTTP tool surface
(``POST /v1/tools/<name>``). Only the catalog and COM services construct a
LoyaltyClient — the bearer token never leaves a BSS process (same posture
as the OpenRouter key never leaving the orchestrator).

Contract (verified against loyalty-cli ``:8080/openapi.json``, 2026-05-22):

- **Auth:** ``Authorization: Bearer <BSS_LOYALTY_API_TOKEN>`` via
  :class:`~bss_clients.auth.BearerAuthProvider`.
- **Headers:** every call requires ``Idempotency-Key`` and ``X-Actor-Id``;
  roles travel optionally in ``X-Actor-Roles``. BSS maps its propagated
  actor (``X-BSS-Actor`` contextvar) onto loyalty's ``X-Actor-Id``.
- **Idempotency:** loyalty mandates the header on *every* call. Writes pass
  a stable key derived from the order/promotion id so a BSS-side retry
  replays rather than double-applies; reads mint a uuid4.
- **Refusals:** HTTP 422 ``{"detail": {"refused": true, "code", "detail"}}``
  is translated to :class:`PolicyViolationFromServer` so callers branch on
  the same exception type as a native BSS policy violation.

The typed methods cover exactly the loyalty tools the v1.1 plan calls:
catalog → ``offer_definition.register``, ``promo_code.register/show``,
``offer.issue``, ``offer.list``; COM → ``offer.claim``,
``offer.advance_to_claimed``, ``offer.redeem``, ``offer.revoke``.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from .auth import AuthProvider
from .base import BSSClient, _actor_var
from .errors import (
    ClientError,
    NotFound,
    PolicyViolationFromServer,
    ServerError,
    Timeout,
)

# loyalty enum values, surfaced as constants so BSS callers don't hardcode
# strings the loyalty API would 422 on. Mirrors loyalty's RevokeReason /
# PromoCodeKind / OfferDefinitionKind (verified against openapi.json).
REVOKE_ORDER_CANCELLED = "order_cancelled"  # BSS "order failed/cancelled" maps here
REVOKE_OPERATOR_ACTION = "operator_action"
REVOKE_CUSTOMER_CHANGED_MIND = "customer_changed_mind"

PROMO_KIND_SINGLE_USE_SHARED = "single_use_shared"
PROMO_KIND_MULTI_USE = "multi_use"
PROMO_KIND_SINGLE_USE_UNIQUE = "single_use_unique_per_customer"

OFFER_DEF_KIND_REGULAR = "regular"


class LoyaltyClient(BSSClient):
    """Client for the loyalty-cli HTTP tool surface (external; v1.1).

    Unlike the sibling BSS clients, this one does **not** send the BSS
    perimeter headers (``X-BSS-API-Token`` / ``X-BSS-Actor`` /
    ``X-BSS-Channel``) — loyalty has its own auth + actor model. It also
    overrides response handling to understand loyalty's refusal envelope.
    """

    def __init__(
        self,
        base_url: str,
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
        *,
        actor_roles: str = "author,reviewer,publisher",
    ) -> None:
        super().__init__(base_url, auth_provider, timeout)
        # loyalty gates a few writes on roles (campaign.role.required). The
        # offer/promo_code/offer_definition tools BSS uses don't, but sending
        # the full set (loyalty's own CLI default) is harmless + future-proof.
        self._actor_roles = actor_roles

    # ── transport ──────────────────────────────────────────────────────

    async def _call(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        actor: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """POST ``/v1/tools/<tool_name>`` with ``args`` as the JSON body.

        ``idempotency_key`` is mandatory on loyalty's side; when omitted
        (reads) a uuid4 is minted so the call still satisfies the contract.
        Write callers MUST pass a stable key.
        """
        headers = {
            "X-Actor-Id": actor or _actor_var.get(),
            "X-Actor-Roles": self._actor_roles,
            "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
        }
        headers.update(await self._auth.get_headers())

        try:
            resp = await self._client.request(
                "POST",
                f"/v1/tools/{tool_name}",
                json=args,
                headers=headers,
                timeout=timeout if timeout is not None else self._timeout,
            )
        except httpx.TimeoutException:
            raise Timeout(f"POST /v1/tools/{tool_name} timed out")
        return self._handle_loyalty_response(resp)

    @staticmethod
    def _handle_loyalty_response(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 404:
            raise NotFound(resp.text)

        if resp.status_code == 422:
            try:
                body = resp.json()
            except Exception:
                raise ClientError(422, resp.text)
            # FastAPI wraps the HTTPException detail under "detail":
            #   {"detail": {"refused": true, "code": ..., "detail": ...}}
            detail = body.get("detail", body) if isinstance(body, dict) else {}
            if isinstance(detail, dict) and detail.get("refused"):
                raise PolicyViolationFromServer(
                    rule=detail.get("code", "loyalty.refused"),
                    message=detail.get("detail") or resp.text,
                    context={"source": "loyalty", "refused": True},
                )
            raise ClientError(422, resp.text)

        if resp.status_code >= 500:
            raise ServerError(resp.status_code, resp.text)

        resp.raise_for_status()
        return resp.json()

    # ── readiness ───────────────────────────────────────────────────────

    async def healthz(self) -> bool:
        """GET /healthz — for caller lifespan readiness. True iff 200."""
        try:
            resp = await self._client.get("/healthz", timeout=self._timeout)
        except httpx.TimeoutException:
            raise Timeout("GET /healthz timed out")
        return resp.status_code == 200

    # ── customer registry (CRM syncs BSS customers into loyalty) ─────────

    async def register_customer(
        self, *, customer_id: str, active: bool = True, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """``customer.register`` — mirror a BSS customer into loyalty's registry
        so its customer-facing views/audit recognise the id. Idempotent on the
        customer id (re-registering an existing customer is a safe replay)."""
        return await self._call(
            "customer.register",
            {"id": customer_id, "active": active},
            idempotency_key=idempotency_key or f"cust:{customer_id}",
        )

    # ── offer definitions + promo codes (catalog: create saga) ──────────

    async def register_offer_definition(
        self,
        *,
        definition_id: str,
        display_name: str,
        idempotency_key: str,
        kind: str = OFFER_DEF_KIND_REGULAR,
        **extra: Any,
    ) -> dict[str, Any]:
        """``offer_definition.register`` — the OD that promo codes/offers hang off.

        ``idempotency_key`` is the catalog ``promotion`` id (saga step 2), so a
        retried saga relinks the same OD instead of minting a duplicate.
        """
        args: dict[str, Any] = {
            "id": definition_id,
            "display_name": display_name,
            "kind": kind,
            **extra,
        }
        return await self._call(
            "offer_definition.register", args, idempotency_key=idempotency_key
        )

    async def register_promo_code(
        self,
        *,
        code: str,
        offer_definition_id: str,
        kind: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """``promo_code.register`` — bind a typed code to an OD (non-targeted)."""
        return await self._call(
            "promo_code.register",
            {"code": code, "offer_definition_id": offer_definition_id, "kind": kind},
            idempotency_key=idempotency_key,
        )

    async def show_promo_code(self, code: str) -> dict[str, Any]:
        """``promo_code.show`` — read OD id / state / binding for a code. No consume."""
        return await self._call("promo_code.show", {"code": code})

    async def issue_offer(
        self,
        *,
        offer_id: str,
        offer_definition_id: str,
        customer_id: str,
        source: dict[str, Any],
        idempotency_key: str,
        parent_offer_id: str | None = None,
    ) -> dict[str, Any]:
        """``offer.issue`` — targeted assignment: leave an ``issued`` offer on a customer.

        ``offer_id`` is caller-supplied (BSS mints it). ``source`` is loyalty's
        discriminated union, e.g. ``{"type": "campaign", "campaign_id": ...}`` or
        ``{"type": "gift", "issued_by": <operator>}``.
        """
        args: dict[str, Any] = {
            "offer_id": offer_id,
            "offer_definition_id": offer_definition_id,
            "customer_id": customer_id,
            "source": source,
        }
        if parent_offer_id is not None:
            args["parent_offer_id"] = parent_offer_id
        return await self._call("offer.issue", args, idempotency_key=idempotency_key)

    async def list_offers(
        self,
        *,
        customer_id: str | None = None,
        state: str | None = None,
        offer_definition_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """``offer.list`` — entitlement reads (preview / dashboard). Returns
        ``{"rows": [...], "limit", "offset", "has_more"}``.
        """
        args: dict[str, Any] = {}
        if customer_id is not None:
            args["customer_id"] = customer_id
        if state is not None:
            args["state"] = state
        if offer_definition_id is not None:
            args["offer_definition_id"] = offer_definition_id
        if limit is not None:
            args["limit"] = limit
        if offset is not None:
            args["offset"] = offset
        return await self._call("offer.list", args)

    # ── consume lifecycle (COM) ─────────────────────────────────────────

    async def claim_offer(
        self,
        *,
        customer_id: str,
        source: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """``offer.claim`` — consume a non-targeted code at activation (the gate).

        ``source`` = ``{"type": "promo_code", "code": <code>}``. ``idempotency_key``
        is the order id, so a SOM-completed retry never double-burns the code.
        Returns the created offer (carries ``offer_id`` for redeem/revoke).
        """
        return await self._call(
            "offer.claim",
            {"customer_id": customer_id, "source": source},
            idempotency_key=idempotency_key,
        )

    async def advance_offer_to_claimed(
        self,
        *,
        offer_id: str,
        idempotency_key: str,
        order_ref: str | None = None,
    ) -> dict[str, Any]:
        """``offer.advance_to_claimed`` — move a targeted ``issued`` offer to ``claimed``."""
        args: dict[str, Any] = {"offer_id": offer_id}
        if order_ref is not None:
            args["order_ref"] = order_ref
        return await self._call(
            "offer.advance_to_claimed", args, idempotency_key=idempotency_key
        )

    async def redeem_offer(
        self,
        *,
        offer_id: str,
        order_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """``offer.redeem`` — finalize on activation success."""
        return await self._call(
            "offer.redeem",
            {"offer_id": offer_id, "order_ref": order_ref},
            idempotency_key=idempotency_key,
        )

    async def expire_offer(
        self,
        *,
        offer_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """``offer.expire`` — terminal transition for an ``issued`` offer that
        was never claimed (e.g. unassigning a customer before they ordered).

        FSM: ``issued → expired``. ``offer.revoke`` is illegal from ``issued``
        — that's the ``claimed → revoked`` lane (order failed / cancelled).
        """
        return await self._call(
            "offer.expire", {"offer_id": offer_id}, idempotency_key=idempotency_key
        )

    async def revoke_offer(
        self,
        *,
        offer_id: str,
        reason: str,
        idempotency_key: str,
        restore_inventory: bool | None = None,
        order_ref: str | None = None,
    ) -> dict[str, Any]:
        """``offer.revoke`` — release the entitlement when the order fails/cancels.

        ``reason`` must be a loyalty ``RevokeReason`` (use ``REVOKE_*`` constants);
        a BSS order decline maps to ``REVOKE_ORDER_CANCELLED``.
        """
        args: dict[str, Any] = {"offer_id": offer_id, "reason": reason}
        if restore_inventory is not None:
            args["restore_inventory"] = restore_inventory
        if order_ref is not None:
            args["order_ref"] = order_ref
        return await self._call("offer.revoke", args, idempotency_key=idempotency_key)
