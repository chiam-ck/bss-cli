"""AuditClient — read-only view of a service's ``audit.domain_event``.

Used by the scenario runner to assert on what happened during a run
— e.g., "after reset, was ``order.completed`` emitted for ORD-042?".
Every service mounts the same router at ``/audit-api/v1/events`` via
``bss_events.audit_events_router``; this client is the paired caller.
"""

from __future__ import annotations

from typing import Any

from .auth import AuthProvider
from .base import BSSClient


class AuditClient(BSSClient):
    """Client for a single service's ``/audit-api/v1`` surface."""

    def __init__(
        self,
        base_url: str,
        auth_provider: AuthProvider | None = None,
        timeout: float = 10.0,
    ):
        super().__init__(base_url, auth_provider, timeout)

    async def list_events(
        self,
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_type: str | None = None,
        event_type_prefix: str | None = None,
        occurred_since: str | None = None,
        occurred_until: str | None = None,
        service_identity: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /audit-api/v1/events with optional filters.

        Returns the event list (unwrapped from the envelope) ordered by
        ``occurredAt`` ascending. Each event is a dict with the
        camelCase fields the router emits.

        ``service_identity`` (v0.9+) scopes results to a single
        perimeter-resolved identity, e.g. ``"portal_self_serve"``.
        """
        params: dict[str, Any] = {"limit": limit}
        if aggregate_type is not None:
            params["aggregateType"] = aggregate_type
        if aggregate_id is not None:
            params["aggregateId"] = aggregate_id
        if event_type is not None:
            params["eventType"] = event_type
        if event_type_prefix is not None:
            params["eventTypePrefix"] = event_type_prefix
        if occurred_since is not None:
            params["occurredSince"] = occurred_since
        if occurred_until is not None:
            params["occurredUntil"] = occurred_until
        if service_identity is not None:
            params["serviceIdentity"] = service_identity

        resp = await self._request("GET", "/audit-api/v1/events", params=params)
        body = resp.json()
        events: list[dict[str, Any]] = body.get("events", [])
        return events
