"""InventoryClient — service-to-service client for CRM's Inventory sub-domain.

Inventory lives inside CRM (port 8002) under /inventory-api/v1/.
This client isolates callers from that hosting detail.
"""

from __future__ import annotations

from typing import Any

from .auth import AuthProvider
from .base import BSSClient


class InventoryClient(BSSClient):
    """Client for the Inventory sub-domain (hosted on CRM, port 8002)."""

    def __init__(
        self,
        base_url: str = "http://crm:8000",
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
    ):
        super().__init__(base_url, auth_provider, timeout)

    # ── MSISDN ──────────────────────────────────────────────────────

    async def list_msisdns(
        self,
        *,
        state: str | None = None,
        prefix: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """GET /inventory-api/v1/msisdn."""
        params: dict[str, Any] = {"limit": limit}
        if state:
            params["status"] = state
        if prefix:
            params["prefix"] = prefix
        resp = await self._request(
            "GET", "/inventory-api/v1/msisdn", params=params
        )
        return resp.json()

    async def count_msisdns(
        self, *, prefix: str | None = None
    ) -> dict[str, Any]:
        """GET /inventory-api/v1/msisdn/count — group-by-status pool count."""
        params: dict[str, Any] = {}
        if prefix:
            params["prefix"] = prefix
        resp = await self._request(
            "GET", "/inventory-api/v1/msisdn/count", params=params
        )
        return resp.json()

    async def get_msisdn(self, msisdn: str) -> dict[str, Any]:
        """GET /inventory-api/v1/msisdn/{msisdn}."""
        resp = await self._request(
            "GET", f"/inventory-api/v1/msisdn/{msisdn}"
        )
        return resp.json()

    async def reserve_msisdn(self, msisdn: str) -> dict[str, Any]:
        """POST /inventory-api/v1/msisdn/{msisdn}/reserve."""
        resp = await self._request(
            "POST", f"/inventory-api/v1/msisdn/{msisdn}/reserve"
        )
        return resp.json()

    async def reserve_next_msisdn(
        self, preference: str | None = None
    ) -> dict[str, Any]:
        """POST /inventory-api/v1/msisdn/reserve-next — atomic auto-pick."""
        body = {"preference": preference} if preference else {}
        resp = await self._request(
            "POST", "/inventory-api/v1/msisdn/reserve-next", json=body
        )
        return resp.json()

    async def assign_msisdn(self, msisdn: str) -> dict[str, Any]:
        """POST /inventory-api/v1/msisdn/{msisdn}/assign."""
        resp = await self._request(
            "POST", f"/inventory-api/v1/msisdn/{msisdn}/assign"
        )
        return resp.json()

    async def release_msisdn(self, msisdn: str) -> dict[str, Any]:
        """POST /inventory-api/v1/msisdn/{msisdn}/release."""
        resp = await self._request(
            "POST", f"/inventory-api/v1/msisdn/{msisdn}/release"
        )
        return resp.json()

    async def add_msisdn_range(
        self, prefix: str, count: int
    ) -> dict[str, Any]:
        """POST /inventory-api/v1/msisdn/add-range — v0.17 operator-only."""
        resp = await self._request(
            "POST",
            "/inventory-api/v1/msisdn/add-range",
            json={"prefix": prefix, "count": count},
        )
        return resp.json()

    # ── eSIM ────────────────────────────────────────────────────────

    async def list_esims(
        self, *, state: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """GET /inventory-api/v1/esim."""
        params: dict[str, Any] = {"limit": limit}
        if state:
            params["status"] = state
        resp = await self._request(
            "GET", "/inventory-api/v1/esim", params=params
        )
        return resp.json()

    async def get_esim(self, iccid: str) -> dict[str, Any]:
        """GET /inventory-api/v1/esim/{iccid}."""
        resp = await self._request(
            "GET", f"/inventory-api/v1/esim/{iccid}"
        )
        return resp.json()

    async def reserve_esim(self) -> dict[str, Any]:
        """POST /inventory-api/v1/esim/reserve."""
        resp = await self._request(
            "POST", "/inventory-api/v1/esim/reserve"
        )
        return resp.json()

    async def assign_msisdn_to_esim(
        self, iccid: str, msisdn: str
    ) -> dict[str, Any]:
        """POST /inventory-api/v1/esim/{iccid}/assign-msisdn."""
        resp = await self._request(
            "POST",
            f"/inventory-api/v1/esim/{iccid}/assign-msisdn",
            json={"msisdn": msisdn},
        )
        return resp.json()

    async def release_esim(self, iccid: str) -> dict[str, Any]:
        """POST /inventory-api/v1/esim/{iccid}/release — reserved→available."""
        resp = await self._request(
            "POST", f"/inventory-api/v1/esim/{iccid}/release"
        )
        return resp.json()

    async def recycle_esim(self, iccid: str) -> dict[str, Any]:
        """POST /inventory-api/v1/esim/{iccid}/recycle — activated→recycled."""
        resp = await self._request(
            "POST", f"/inventory-api/v1/esim/{iccid}/recycle"
        )
        return resp.json()

    async def get_activation_code(self, iccid: str) -> dict[str, Any]:
        """GET /inventory-api/v1/esim/{iccid}/activation.

        Returns {iccid, imsi, activationCode, msisdn?}.
        """
        resp = await self._request(
            "GET", f"/inventory-api/v1/esim/{iccid}/activation"
        )
        return resp.json()
