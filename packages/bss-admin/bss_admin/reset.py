"""Admin reset router — per-service operational-data wipe.

Each service mounts this router with a hardcoded list of ``ResetPlan``s,
one per Postgres schema the service owns. Every plan declares its schema
once and all statements are prefixed with it, so a misconfigured plan
cannot reach into another service's schema (let alone the Campaign OS
co-tenant schema).

The endpoint is gated behind the ``BSS_ALLOW_ADMIN_RESET`` env flag. In
production this is unset and the endpoint returns 403. Scenario runs and
developer REPL sessions set it to ``true`` explicitly.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

import structlog
from bss_clock import now as clock_now
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

log = structlog.get_logger()


@dataclass(frozen=True)
class TableReset:
    """How to reset a single table.

    ``mode="truncate"`` wipes every row (with ``RESTART IDENTITY CASCADE``
    so dependent serial columns reset). ``mode="update"`` runs a fixed SQL
    ``UPDATE`` that restores rows to an available/default state — used for
    reference-backed pools like ``inventory.msisdn_pool`` where we want
    the pool rows kept but their assignment cleared.
    """

    name: str
    mode: Literal["truncate", "update"] = "truncate"
    update_sql: str | None = None  # required when mode == "update"


@dataclass(frozen=True)
class ResetPlan:
    """One schema's admin reset manifest.

    ``schema`` is the Postgres schema the service owns — every table in
    ``tables`` is prefixed with it. Listing a table in another schema is
    not possible: the handler quotes ``{schema}.{name}`` directly.
    """

    schema: str
    tables: tuple[TableReset, ...] = field(default_factory=tuple)


def _is_allowed() -> bool:
    flag = os.environ.get("BSS_ALLOW_ADMIN_RESET", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def admin_router(
    *,
    service_name: str,
    plans: Sequence[ResetPlan],
) -> APIRouter:
    """Build the ``/reset-operational-data`` router for one service.

    ``service_name`` is the short logical name (``"crm"``, ``"com"``, …);
    it tags the audit marker and response body. ``plans`` lists every
    schema the service owns — usually one, but e.g. CRM owns both ``crm``
    and ``inventory`` and passes two plans. Mount under ``/admin-api/v1``
    in the service's ``main.py``.
    """
    router = APIRouter(tags=["admin"])

    @router.post("/reset-operational-data")
    async def reset_operational_data(request: Request) -> dict:
        if not _is_allowed():
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "ADMIN_RESET_DISABLED",
                    "message": (
                        "Admin reset is gated behind the BSS_ALLOW_ADMIN_RESET "
                        "env flag. Set it to 'true' in the service environment "
                        "(scenario runs and developer REPLs only)."
                    ),
                },
            )

        session_factory = request.app.state.session_factory
        per_schema: list[dict] = []
        started_at = clock_now()

        async with session_factory() as session:
            async with session.begin():
                for plan in plans:
                    truncated: list[str] = []
                    updated: list[str] = []
                    for table in plan.tables:
                        qualified = f'"{plan.schema}"."{table.name}"'
                        if table.mode == "truncate":
                            await session.execute(
                                text(f"TRUNCATE TABLE {qualified} RESTART IDENTITY CASCADE")
                            )
                            truncated.append(table.name)
                        elif table.mode == "update":
                            if not table.update_sql:
                                raise RuntimeError(
                                    f"TableReset({table.name}) mode=update requires update_sql"
                                )
                            await session.execute(text(table.update_sql))
                            updated.append(table.name)
                        else:  # pragma: no cover — dataclass Literal guards this
                            raise RuntimeError(f"unknown reset mode: {table.mode!r}")
                    per_schema.append(
                        {
                            "schema": plan.schema,
                            "truncated": truncated,
                            "updated": updated,
                        }
                    )

                # Audit marker — written to audit.domain_event so scenarios can
                # filter with ``occurred_at >= resetAt`` rather than truncating
                # audit history (kept for cross-run forensics).
                actor = request.headers.get("X-BSS-Actor", "system")
                channel = request.headers.get("X-BSS-Channel", "cli")
                await session.execute(
                    text(
                        """
                        INSERT INTO audit.domain_event (
                            event_id, event_type, aggregate_type, aggregate_id,
                            occurred_at, actor, channel, tenant_id, payload,
                            schema_version, published_to_mq
                        ) VALUES (
                            :event_id, :event_type, :aggregate_type, :aggregate_id,
                            :occurred_at, :actor, :channel, :tenant_id,
                            CAST(:payload AS JSONB), 1, false
                        )
                        """
                    ),
                    {
                        "event_id": uuid4(),
                        "event_type": "admin.operational_data_reset",
                        "aggregate_type": "service",
                        "aggregate_id": service_name,
                        "occurred_at": started_at,
                        "actor": actor,
                        "channel": channel,
                        "tenant_id": "DEFAULT",
                        "payload": json.dumps(
                            {
                                "service": service_name,
                                "schemas": per_schema,
                            }
                        ),
                    },
                )

        log.warning(
            "admin.reset.completed",
            service=service_name,
            schemas=per_schema,
        )

        return {
            "service": service_name,
            "schemas": per_schema,
            "resetAt": started_at.isoformat(),
        }

    return router
