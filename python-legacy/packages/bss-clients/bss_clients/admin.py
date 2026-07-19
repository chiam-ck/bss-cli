"""AdminClient — service-to-service client for the admin-api.

Only the CLI coordinator (``bss admin reset``) and scenario runner use
this — it is deliberately NOT exposed to the LLM tool surface. Every
call hits an endpoint gated by ``BSS_ALLOW_ADMIN_RESET``, so a
misconfigured deployment returns 403 instead of wiping data.
"""

from __future__ import annotations

from typing import Any

from .auth import AuthProvider
from .base import BSSClient


class AdminClient(BSSClient):
    """Client for a single service's ``/admin-api/v1`` surface."""

    def __init__(
        self,
        base_url: str,
        auth_provider: AuthProvider | None = None,
        timeout: float = 30.0,
    ):
        # Reset can TRUNCATE ten+ tables — bump timeout above the default 5s.
        super().__init__(base_url, auth_provider, timeout)

    async def reset_operational_data(self) -> dict[str, Any]:
        """POST /admin-api/v1/reset-operational-data.

        Returns ``{service, schemas: [{schema, truncated, updated}], resetAt}``.
        Raises ``ClientError(403)`` if the target service has
        ``BSS_ALLOW_ADMIN_RESET`` unset.
        """
        resp = await self._request("POST", "/admin-api/v1/reset-operational-data")
        return resp.json()
