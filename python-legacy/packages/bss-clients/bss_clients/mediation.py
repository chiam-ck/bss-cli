"""MediationClient — service-to-service client for Mediation (port 8007).

TMF635 online usage mediation surface. One event at a time, block-at-edge.
"""

from __future__ import annotations

from typing import Any

from .auth import AuthProvider
from .base import BSSClient


class MediationClient(BSSClient):
    """Client for the Mediation service (port 8007)."""

    def __init__(
        self,
        base_url: str = "http://mediation:8000",
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
    ):
        super().__init__(base_url, auth_provider, timeout)

    async def submit_usage(
        self,
        *,
        msisdn: str,
        event_type: str,
        event_time: str,
        quantity: int,
        unit: str,
        source: str | None = None,
        raw_cdr_ref: str | None = None,
        roaming_indicator: bool = False,
    ) -> dict[str, Any]:
        """POST /tmf-api/usageManagement/v4/usage — submit one usage event.

        v0.17 — ``roaming_indicator`` defaults False; set True to mark a
        usage event as having occurred on a visited network. Mediation
        passes through; rating routes to ``data_roaming``.
        """
        body: dict[str, Any] = {
            "msisdn": msisdn,
            "eventType": event_type,
            "eventTime": event_time,
            "quantity": quantity,
            "unit": unit,
        }
        if source is not None:
            body["source"] = source
        if raw_cdr_ref is not None:
            body["rawCdrRef"] = raw_cdr_ref
        if roaming_indicator:
            body["roamingIndicator"] = True
        resp = await self._request(
            "POST", "/tmf-api/usageManagement/v4/usage", json=body
        )
        return resp.json()

    async def get_usage(self, event_id: str) -> dict[str, Any]:
        """GET /tmf-api/usageManagement/v4/usage/{id}."""
        resp = await self._request(
            "GET", f"/tmf-api/usageManagement/v4/usage/{event_id}"
        )
        return resp.json()

    async def list_usage(
        self,
        *,
        subscription_id: str | None = None,
        msisdn: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /tmf-api/usageManagement/v4/usage."""
        params: dict[str, Any] = {"limit": limit}
        if subscription_id:
            params["subscriptionId"] = subscription_id
        if msisdn:
            params["msisdn"] = msisdn
        if event_type:
            params["type"] = event_type
        if since:
            params["since"] = since
        resp = await self._request(
            "GET", "/tmf-api/usageManagement/v4/usage", params=params
        )
        return resp.json()
