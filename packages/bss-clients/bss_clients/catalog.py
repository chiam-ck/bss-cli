"""CatalogClient — service-to-service client for Catalog."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bss_clock import now as clock_now

from .auth import AuthProvider
from .base import BSSClient


class CatalogClient(BSSClient):
    """Client for the Catalog service (port 8001)."""

    def __init__(
        self,
        base_url: str = "http://catalog:8000",
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
    ):
        super().__init__(base_url, auth_provider, timeout)

    async def get_offering(self, offering_id: str) -> dict[str, Any]:
        """GET /tmf-api/productCatalogManagement/v4/productOffering/{id}."""
        resp = await self._request(
            "GET",
            f"/tmf-api/productCatalogManagement/v4/productOffering/{offering_id}",
        )
        return resp.json()

    async def list_offerings(self) -> list[dict[str, Any]]:
        """GET /tmf-api/productCatalogManagement/v4/productOffering."""
        resp = await self._request(
            "GET",
            "/tmf-api/productCatalogManagement/v4/productOffering",
        )
        return resp.json()

    async def list_active_offerings(
        self, *, at: datetime | None = None
    ) -> list[dict[str, Any]]:
        """GET /productOffering?activeAt=ISO — sellable-now offerings, sorted by lowest price."""
        moment = at or clock_now()
        resp = await self._request(
            "GET",
            "/tmf-api/productCatalogManagement/v4/productOffering",
            params={"activeAt": moment.isoformat()},
        )
        return resp.json()

    async def get_active_price(
        self, offering_id: str, *, at: datetime | None = None
    ) -> dict[str, Any]:
        """GET /productOfferingPrice/active/{offering_id}?activeAt=ISO.

        Raises ``PolicyViolationFromServer('catalog.price.no_active_row')``
        when no row matches at the requested moment.
        """
        params: dict[str, Any] = {}
        if at is not None:
            params["activeAt"] = at.isoformat()
        resp = await self._request(
            "GET",
            f"/tmf-api/productCatalogManagement/v4/productOfferingPrice/active/{offering_id}",
            params=params,
        )
        return resp.json()

    async def get_offering_price(self, price_id: str) -> dict[str, Any]:
        """GET /productOfferingPrice/{id} — direct lookup, no time filter."""
        resp = await self._request(
            "GET",
            f"/tmf-api/productCatalogManagement/v4/productOfferingPrice/{price_id}",
        )
        return resp.json()

    async def get_vas(self, vas_id: str) -> dict[str, Any]:
        """GET /vas/offering/{vas_id}."""
        resp = await self._request("GET", f"/vas/offering/{vas_id}")
        return resp.json()

    async def list_vas(self) -> list[dict[str, Any]]:
        """GET /vas/offering."""
        resp = await self._request("GET", "/vas/offering")
        return resp.json()

    # ── v0.7 — admin write paths ───────────────────────────────────────

    async def admin_add_offering(
        self,
        *,
        offering_id: str,
        name: str,
        amount: str,
        currency: str = "SGD",
        spec_id: str = "SPEC_MOBILE_PREPAID",
        price_id: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        data_mb: int | None = None,
        voice_minutes: int | None = None,
        sms_count: int | None = None,
        data_roaming_mb: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "offeringId": offering_id,
            "name": name,
            "specId": spec_id,
            "amount": amount,
            "currency": currency,
        }
        if price_id is not None:
            body["priceId"] = price_id
        if valid_from is not None:
            body["validFrom"] = valid_from.isoformat()
        if valid_to is not None:
            body["validTo"] = valid_to.isoformat()
        if data_mb is not None:
            body["dataMb"] = data_mb
        if voice_minutes is not None:
            body["voiceMinutes"] = voice_minutes
        if sms_count is not None:
            body["smsCount"] = sms_count
        if data_roaming_mb is not None:
            body["dataRoamingMb"] = data_roaming_mb
        resp = await self._request("POST", "/admin/catalog/offering", json=body)
        return resp.json()

    async def admin_set_offering_window(
        self,
        offering_id: str,
        *,
        valid_from: datetime | None,
        valid_to: datetime | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if valid_from is not None:
            body["validFrom"] = valid_from.isoformat()
        if valid_to is not None:
            body["validTo"] = valid_to.isoformat()
        resp = await self._request(
            "PATCH",
            f"/admin/catalog/offering/{offering_id}/window",
            json=body,
        )
        return resp.json()

    async def admin_add_price(
        self,
        offering_id: str,
        *,
        price_id: str,
        amount: str,
        currency: str = "SGD",
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        retire_current: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "priceId": price_id,
            "amount": amount,
            "currency": currency,
            "retireCurrent": retire_current,
        }
        if valid_from is not None:
            body["validFrom"] = valid_from.isoformat()
        if valid_to is not None:
            body["validTo"] = valid_to.isoformat()
        resp = await self._request(
            "POST",
            f"/admin/catalog/offering/{offering_id}/price",
            json=body,
        )
        return resp.json()

    async def admin_retire_offering(self, offering_id: str) -> dict[str, Any]:
        """POST /admin/catalog/offering/{offering_id}/retire — retire an offering.

        Sets lifecycle_status='retired' + is_sellable=False and stamps
        valid_to=now() when the current window is still open.
        Raises ``PolicyViolationFromServer`` if already retired or not found.
        """
        resp = await self._request(
            "POST", f"/admin/catalog/offering/{offering_id}/retire"
        )
        return resp.json()

    # ── v1.1 — promotions (catalog holds the loyalty link) ───────────────

    _PROMO = "/tmf-api/promotionManagement/v4/promotion"

    async def create_promotion(
        self,
        *,
        promotion_id: str,
        discount_type: str,
        discount_value: str,
        duration_kind: str,
        audience: str = "public",
        currency: str = "SGD",
        code: str | None = None,
        promo_code_kind: str | None = None,
        applicable_offering_ids: list[str] | None = None,
        periods_total: int | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """POST the create-promotion saga. Raises PolicyViolationFromServer on
        a validation failure or a translated loyalty refusal."""
        body: dict[str, Any] = {
            "promotionId": promotion_id,
            "discountType": discount_type,
            "discountValue": discount_value,
            "durationKind": duration_kind,
            "audience": audience,
            "currency": currency,
        }
        if code is not None:
            body["code"] = code
        if promo_code_kind is not None:
            body["promoCodeKind"] = promo_code_kind
        if applicable_offering_ids is not None:
            body["applicableOfferingIds"] = applicable_offering_ids
        if periods_total is not None:
            body["periodsTotal"] = periods_total
        if valid_from is not None:
            body["validFrom"] = valid_from.isoformat()
        if valid_to is not None:
            body["validTo"] = valid_to.isoformat()
        if display_name is not None:
            body["displayName"] = display_name
        resp = await self._request("POST", self._PROMO, json=body)
        return resp.json()

    async def get_promotion(self, promotion_id: str) -> dict[str, Any]:
        resp = await self._request("GET", f"{self._PROMO}/{promotion_id}")
        return resp.json()

    async def list_promotions(
        self, *, state: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if state is not None:
            params["state"] = state
        resp = await self._request("GET", self._PROMO, params=params)
        return resp.json()

    async def assign_promotion(
        self,
        promotion_id: str,
        *,
        customer_ids: list[str],
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"customerIds": customer_ids}
        if source is not None:
            body["source"] = source
        resp = await self._request("POST", f"{self._PROMO}/{promotion_id}/assign", json=body)
        return resp.json()

    async def unassign_promotion(
        self,
        promotion_id: str,
        *,
        customer_ids: list[str],
    ) -> dict[str, Any]:
        """v1.3.1 — remove customers from a targeted promo's eligibility list +
        ``offer.revoke`` the upfront-minted loyalty offer. Idempotent."""
        body: dict[str, Any] = {"customerIds": customer_ids}
        resp = await self._request("POST", f"{self._PROMO}/{promotion_id}/unassign", json=body)
        return resp.json()

    async def exhaust_promotion(self, promotion_id: str) -> dict[str, Any]:
        """v1.4.1 — operator-initiated terminal stop. Flips an ``active``
        promotion to ``exhausted``; the validate/resolve paths then reject
        the code so new orders proceed at full price. Idempotent (re-call
        on an already-exhausted row returns the row unchanged). Raises
        ``NotFound`` if the promo doesn't exist."""
        resp = await self._request(
            "POST", f"{self._PROMO}/{promotion_id}/exhaust"
        )
        return resp.json()

    async def preview_promo(
        self, *, code: str, offering: str, customer_id: str | None = None
    ) -> dict[str, Any]:
        """Portal live-preview: {valid, label, base, effective, reason}.
        ``customer_id`` gates a targeted code on eligibility."""
        params: dict[str, Any] = {"code": code, "offering": offering}
        if customer_id is not None:
            params["customerId"] = customer_id
        resp = await self._request("GET", "/promo/preview", params=params)
        return resp.json()

    async def validate_promo(
        self, *, code: str, offering: str, customer_id: str | None = None
    ) -> dict[str, Any]:
        """Order-time validation: full discount terms COM stamps onto the order.
        ``customer_id`` gates a targeted code on eligibility."""
        params: dict[str, Any] = {"code": code, "offering": offering}
        if customer_id is not None:
            params["customerId"] = customer_id
        resp = await self._request("GET", "/promo/validate", params=params)
        return resp.json()

    async def list_customer_offers(
        self, *, customer_id: str, state: str | None = None
    ) -> dict[str, Any]:
        """Targeted-offer entitlement reads for the dashboard / order discovery."""
        params: dict[str, Any] = {"customerId": customer_id}
        if state is not None:
            params["state"] = state
        resp = await self._request("GET", "/promo/customer-offers", params=params)
        return resp.json()

    async def resolve_eligible_promo(
        self, *, customer_id: str, offering: str
    ) -> dict[str, Any]:
        """Targeted order-time resolution (v1.1.1): best targeted promo the
        customer is eligible for + terms + the promo's CODE (COM claims by code).
        {valid: False} if none."""
        resp = await self._request(
            "GET",
            "/promo/resolve-eligible",
            params={"customerId": customer_id, "offering": offering},
        )
        return resp.json()


