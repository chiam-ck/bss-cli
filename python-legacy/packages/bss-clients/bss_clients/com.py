"""COMClient — service-to-service client for Commercial Order Management (port 8004).

TMF622 ProductOrder surface.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .auth import AuthProvider
from .base import BSSClient
from .errors import Timeout


class COMClient(BSSClient):
    """Client for the COM service (port 8004)."""

    def __init__(
        self,
        base_url: str = "http://com:8000",
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
    ):
        super().__init__(base_url, auth_provider, timeout)

    async def create_order(
        self,
        *,
        customer_id: str,
        offering_id: str,
        msisdn_preference: str | None = None,
        notes: str | None = None,
        discount_code: str | None = None,
        skip_assigned_offer: bool = False,
    ) -> dict[str, Any]:
        """POST /tmf-api/productOrderingManagement/v4/productOrder.

        ``discount_code`` (v1.1) is an optional typed promo code. When omitted,
        COM auto-applies the customer's cheapest assigned offer — unless
        ``skip_assigned_offer`` is set (the customer opted out).
        """
        body: dict[str, Any] = {
            "customerId": customer_id,
            "offeringId": offering_id,
        }
        if msisdn_preference is not None:
            body["msisdnPreference"] = msisdn_preference
        if notes is not None:
            body["notes"] = notes
        if discount_code is not None:
            body["discountCode"] = discount_code
        if skip_assigned_offer:
            body["skipAssignedOffer"] = True
        resp = await self._request(
            "POST",
            "/tmf-api/productOrderingManagement/v4/productOrder",
            json=body,
        )
        return resp.json()

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """GET /tmf-api/productOrderingManagement/v4/productOrder/{id}."""
        resp = await self._request(
            "GET",
            f"/tmf-api/productOrderingManagement/v4/productOrder/{order_id}",
        )
        return resp.json()

    async def list_orders(
        self,
        customer_id: str | None = None,
        *,
        state: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /tmf-api/productOrderingManagement/v4/productOrder.

        v1.6 — ``customerId`` is optional; without it the endpoint
        returns orders across customers, newest first (cockpit CRM
        order queue). ``state``/``limit``/``offset`` filter and page.
        """
        params: dict[str, Any] = {}
        if customer_id:
            params["customerId"] = customer_id
        if state:
            params["state"] = state
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        resp = await self._request(
            "GET",
            "/tmf-api/productOrderingManagement/v4/productOrder",
            params=params,
        )
        return resp.json()

    async def submit_order(self, order_id: str) -> dict[str, Any]:
        """POST /tmf-api/productOrderingManagement/v4/productOrder/{id}/submit."""
        resp = await self._request(
            "POST",
            f"/tmf-api/productOrderingManagement/v4/productOrder/{order_id}/submit",
        )
        return resp.json()

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """POST /tmf-api/productOrderingManagement/v4/productOrder/{id}/cancel."""
        resp = await self._request(
            "POST",
            f"/tmf-api/productOrderingManagement/v4/productOrder/{order_id}/cancel",
        )
        return resp.json()

    async def wait_until(
        self,
        order_id: str,
        *,
        target_state: str,
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.5,
    ) -> dict[str, Any]:
        """Poll GET order until ``state == target_state`` or timeout.

        Raises ``Timeout`` if the order doesn't reach the target state in time.
        Returns the final order payload.
        """
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = await self.get_order(order_id)
            if last.get("state") == target_state:
                return last
            # Terminal non-target states shortcut
            if last.get("state") in ("failed", "cancelled"):
                return last
            await asyncio.sleep(poll_interval_s)
        raise Timeout(
            f"Order {order_id} did not reach state={target_state} "
            f"within {timeout_s}s (last state={last.get('state')!r})"
        )
