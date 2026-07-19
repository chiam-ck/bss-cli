"""v0.18 — admin endpoint for deterministic renewal sweeps.

POST /admin-api/v1/renewal/tick-now invokes _sweep_due + _sweep_skipped
once on demand. Used by the v0.18 hero scenario
(`customer_renews_automatically.yaml`) to drive a single tick after
`clock.advance` without waiting for the wall-clock interval.

Gated by ``BSS_ALLOW_ADMIN_RESET`` — the same flag that gates
``reset-operational-data`` and ``clock/freeze``. Production deployments
keep this flag false; the worker runs naturally on its tick interval
(``BSS_RENEWAL_TICK_SECONDS=60`` by default).
"""

from __future__ import annotations

import os

import structlog
from fastapi import APIRouter, HTTPException, Request

from app.workers.renewal import _sweep_due, _sweep_skipped

log = structlog.get_logger()

router = APIRouter(tags=["admin-renewal"])


def _is_allowed() -> bool:
    """Mirrors ``bss_admin.reset._is_allowed`` — same flag, same semantics."""
    flag = os.environ.get("BSS_ALLOW_ADMIN_RESET", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


@router.post("/renewal/tick-now")
async def tick_now(request: Request) -> dict[str, str]:
    """Run one renewal sweep (active-due + blocked-overdue) on demand."""
    if not _is_allowed():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ADMIN_RENEWAL_DISABLED",
                "message": (
                    "Renewal admin tick is gated behind BSS_ALLOW_ADMIN_RESET. "
                    "Set this env to true in dev/test only; production runs "
                    "the worker on its natural BSS_RENEWAL_TICK_SECONDS interval."
                ),
            },
        )
    log.info("renewal.admin.tick_now.invoked")
    await _sweep_due(request.app)
    await _sweep_skipped(request.app)
    return {"status": "ok"}
