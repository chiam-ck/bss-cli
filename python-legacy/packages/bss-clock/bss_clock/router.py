"""Admin router factory for clock control.

Each service mounts this under ``/admin-api/v1/clock`` so scenarios can
freeze/advance that process's clock. Gated by ``BSS_ALLOW_ADMIN_RESET``
— the same flag that gates ``reset-operational-data``, because both are
operator-only scenario controls and share the same trust boundary.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException

from . import clock


def _is_allowed() -> bool:
    flag = os.environ.get("BSS_ALLOW_ADMIN_RESET", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _guard() -> None:
    if not _is_allowed():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ADMIN_CLOCK_DISABLED",
                "message": (
                    "Clock control is gated behind BSS_ALLOW_ADMIN_RESET. "
                    "Set it to 'true' in the service environment (scenario "
                    "runs and developer REPLs only)."
                ),
            },
        )


def _serialise(s: clock.ClockState) -> dict:
    return {
        "mode": s.mode,
        "now": s.now.isoformat(),
        "offsetSeconds": s.offset_seconds,
        "frozenAt": s.frozen_at.isoformat() if s.frozen_at else None,
    }


def clock_admin_router() -> APIRouter:
    """Build the ``/clock/*`` admin router.

    GET  /now        — public, unguarded (read-only, useful for health views)
    POST /freeze     — guarded
    POST /unfreeze   — guarded
    POST /advance    — guarded
    """
    router = APIRouter(tags=["admin-clock"])

    @router.get("/clock/now")
    async def get_now() -> dict:
        return _serialise(clock.state())

    @router.post("/clock/freeze")
    async def post_freeze(payload: dict = Body(default_factory=dict)) -> dict:
        _guard()
        raw = payload.get("at")
        at: datetime | None = None
        if raw is not None:
            try:
                at = datetime.fromisoformat(str(raw))
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "INVALID_AT",
                        "message": f"'at' must be ISO-8601, got {raw!r}",
                    },
                )
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
        clock.freeze(at)
        return _serialise(clock.state())

    @router.post("/clock/unfreeze")
    async def post_unfreeze() -> dict:
        _guard()
        clock.unfreeze()
        return _serialise(clock.state())

    @router.post("/clock/advance")
    async def post_advance(payload: dict = Body(...)) -> dict:
        _guard()
        duration = payload.get("duration")
        if not isinstance(duration, str):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_DURATION",
                    "message": "'duration' is required (e.g. '30d', '2h').",
                },
            )
        try:
            clock.advance(duration)
        except ValueError as e:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_DURATION", "message": str(e)},
            )
        return _serialise(clock.state())

    return router
