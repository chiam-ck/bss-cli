"""ProvisioningClient — service-to-service client for Provisioning-Sim (port 8008).

Stands in for HLR/PCRF/OCS/SM-DP+ with configurable per-task-type fault injection.
"""

from __future__ import annotations

from typing import Any

from .auth import AuthProvider
from .base import BSSClient


class ProvisioningClient(BSSClient):
    """Client for the Provisioning-Sim service (port 8008)."""

    def __init__(
        self,
        base_url: str = "http://provisioning-sim:8000",
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
    ):
        super().__init__(base_url, auth_provider, timeout)

    # ── Tasks ────────────────────────────────────────────────────────────

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """GET /provisioning-api/v1/task/{id}."""
        resp = await self._request(
            "GET", f"/provisioning-api/v1/task/{task_id}"
        )
        return resp.json()

    async def list_tasks(
        self,
        *,
        service_id: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /provisioning-api/v1/task."""
        params: dict[str, Any] = {}
        if service_id:
            params["serviceId"] = service_id
        if state:
            params["state"] = state
        resp = await self._request(
            "GET", "/provisioning-api/v1/task", params=params
        )
        return resp.json()

    async def resolve_task(self, task_id: str, *, note: str) -> dict[str, Any]:
        """POST /provisioning-api/v1/task/{id}/resolve — manual intervention for stuck tasks."""
        resp = await self._request(
            "POST",
            f"/provisioning-api/v1/task/{task_id}/resolve",
            json={"note": note},
        )
        return resp.json()

    async def retry_task(self, task_id: str) -> dict[str, Any]:
        """POST /provisioning-api/v1/task/{id}/retry."""
        resp = await self._request(
            "POST", f"/provisioning-api/v1/task/{task_id}/retry"
        )
        return resp.json()

    # ── Fault injection ──────────────────────────────────────────────────

    async def list_fault_injection(self) -> list[dict[str, Any]]:
        """GET /provisioning-api/v1/fault-injection."""
        resp = await self._request(
            "GET", "/provisioning-api/v1/fault-injection"
        )
        return resp.json()

    async def update_fault_injection(
        self,
        fault_id: str,
        *,
        enabled: bool | None = None,
        probability: float | None = None,
        fault_type: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /provisioning-api/v1/fault-injection/{id}."""
        body: dict[str, Any] = {}
        if enabled is not None:
            body["enabled"] = enabled
        if probability is not None:
            body["probability"] = probability
        if fault_type is not None:
            body["faultType"] = fault_type
        resp = await self._request(
            "PATCH",
            f"/provisioning-api/v1/fault-injection/{fault_id}",
            json=body,
        )
        return resp.json()
