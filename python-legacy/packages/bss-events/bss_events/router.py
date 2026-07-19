"""Audit-events query router.

Each service mounts this under ``/audit-api/v1`` to expose a
filtered read over ``audit.domain_event``. Intended use is the
scenario runner asserting on what happened during a run.

Filters are all optional; when multiple are supplied they AND
together. Results are ordered by ``occurred_at ASC`` (then ``id
ASC`` as a stable tiebreak), bounded by ``limit`` (default 100,
max 1000).

The endpoint is unguarded — read-only over an append-only log, no
secret material in the payload. When RBAC lands (Phase 12) this
will become role-scoped; for v0.1 any caller may query freely.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

log = structlog.get_logger()

_MAX_LIMIT = 1000
_DEFAULT_LIMIT = 100


def audit_events_router() -> APIRouter:
    """Build the ``/events`` router. Mount under ``/audit-api/v1``.

    Requires ``app.state.session_factory`` — the service's async
    SQLAlchemy session factory. The handler reads from
    ``audit.domain_event`` in a short-lived, read-only session.
    """
    router = APIRouter(tags=["audit"])

    @router.get("/events")
    async def list_events(
        request: Request,
        aggregate_type: Annotated[str | None, Query(alias="aggregateType")] = None,
        aggregate_id: Annotated[str | None, Query(alias="aggregateId")] = None,
        event_type: Annotated[str | None, Query(alias="eventType")] = None,
        event_type_prefix: Annotated[str | None, Query(alias="eventTypePrefix")] = None,
        occurred_since: Annotated[str | None, Query(alias="occurredSince")] = None,
        occurred_until: Annotated[str | None, Query(alias="occurredUntil")] = None,
        # v0.9 — filter by perimeter-resolved identity. "default" /
        # "portal_self_serve" / "partner_<name>". Lets the new hero
        # scenario assert per-surface attribution.
        service_identity: Annotated[
            str | None, Query(alias="serviceIdentity")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    ) -> dict:
        since_dt = _parse_iso_or_400(occurred_since, "occurredSince")
        until_dt = _parse_iso_or_400(occurred_until, "occurredUntil")

        where: list[str] = []
        params: dict[str, object] = {}
        if aggregate_type is not None:
            where.append("aggregate_type = :aggregate_type")
            params["aggregate_type"] = aggregate_type
        if aggregate_id is not None:
            where.append("aggregate_id = :aggregate_id")
            params["aggregate_id"] = aggregate_id
        if event_type is not None:
            where.append("event_type = :event_type")
            params["event_type"] = event_type
        if event_type_prefix is not None:
            where.append("event_type LIKE :event_type_prefix")
            params["event_type_prefix"] = event_type_prefix + "%"
        if since_dt is not None:
            where.append("occurred_at >= :since")
            params["since"] = since_dt
        if until_dt is not None:
            where.append("occurred_at <= :until")
            params["until"] = until_dt
        if service_identity is not None:
            where.append("service_identity = :service_identity")
            params["service_identity"] = service_identity

        clause = "WHERE " + " AND ".join(where) if where else ""
        params["limit"] = limit

        sql = text(
            f"""
            SELECT
                event_id, event_type, aggregate_type, aggregate_id,
                occurred_at, trace_id, actor, channel, tenant_id,
                service_identity, payload,
                schema_version, published_to_mq
            FROM audit.domain_event
            {clause}
            ORDER BY occurred_at ASC, id ASC
            LIMIT :limit
            """
        )

        session_factory = request.app.state.session_factory
        async with session_factory() as session:
            rows = (await session.execute(sql, params)).mappings().all()

        events = [
            {
                "eventId": str(r["event_id"]),
                "eventType": r["event_type"],
                "aggregateType": r["aggregate_type"],
                "aggregateId": r["aggregate_id"],
                "occurredAt": r["occurred_at"].isoformat(),
                "traceId": r["trace_id"],
                "actor": r["actor"],
                "channel": r["channel"],
                "tenantId": r["tenant_id"],
                "serviceIdentity": r["service_identity"],
                "payload": r["payload"] or {},
                "schemaVersion": r["schema_version"],
                "publishedToMq": r["published_to_mq"],
            }
            for r in rows
        ]

        return {"events": events, "count": len(events)}

    return router


def _parse_iso_or_400(raw: str | None, field: str) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_TIMESTAMP",
                "message": f"{field!r} must be ISO-8601, got {raw!r}",
            },
        )
