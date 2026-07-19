"""SOMClient — service-to-service client for Service Order Management.

Used by COM (cancel guard check) and future services.
"""

from __future__ import annotations

from typing import Any

from .auth import AuthProvider
from .base import BSSClient


class SOMClient(BSSClient):
    """Client for the SOM service (port 8005)."""

    def __init__(
        self,
        base_url: str = "http://som:8000",
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
    ):
        super().__init__(base_url, auth_provider, timeout)

    async def get_service_order(self, service_order_id: str) -> dict[str, Any]:
        """GET /tmf-api/serviceOrderingManagement/v4/serviceOrder/{id}."""
        resp = await self._request(
            "GET",
            f"/tmf-api/serviceOrderingManagement/v4/serviceOrder/{service_order_id}",
        )
        return resp.json()

    async def list_for_order(
        self, commercial_order_id: str
    ) -> list[dict[str, Any]]:
        """GET /tmf-api/serviceOrderingManagement/v4/serviceOrder?commercialOrderId={id}."""
        resp = await self._request(
            "GET",
            "/tmf-api/serviceOrderingManagement/v4/serviceOrder",
            params={"commercialOrderId": commercial_order_id},
        )
        return resp.json()

    async def get_service(self, service_id: str) -> dict[str, Any]:
        """GET /tmf-api/serviceInventoryManagement/v4/service/{id}."""
        resp = await self._request(
            "GET",
            f"/tmf-api/serviceInventoryManagement/v4/service/{service_id}",
        )
        return resp.json()

    async def list_services_for_subscription(
        self, subscription_id: str
    ) -> list[dict[str, Any]]:
        """GET /tmf-api/serviceInventoryManagement/v4/service?subscriptionId={id}."""
        resp = await self._request(
            "GET",
            "/tmf-api/serviceInventoryManagement/v4/service",
            params={"subscriptionId": subscription_id},
        )
        return resp.json()
