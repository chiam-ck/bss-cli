"""Portal-side audit helper — append a ``portal_auth.portal_action`` row.

v0.10: every direct post-login self-serve write records one row here
after the BSS write resolves (success or failure). The function takes
already-resolved primitives (``customer_id``, ``identity_id``, etc.)
so this module stays free of FastAPI / Request imports — the same
layering as ``_record_attempt`` for ``login_attempt``.

Caller pattern (from a portal route handler):

    try:
        result = await clients.subscription.purchase_vas(...)
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=identity.id,
            action="vas_purchase",
            route="/top-up",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await db.commit()
    except PolicyViolationFromServer as exc:
        await record_portal_action(
            db,
            ...,
            success=False,
            error_rule=exc.rule,
            ...,
        )
        await db.commit()
        raise

The portal owns the transaction; this module flushes but does not
commit. Same pattern as the rest of ``bss_portal_auth``.
"""

from __future__ import annotations

from bss_clock import now as clock_now
from bss_models import PortalAction
from sqlalchemy.ext.asyncio import AsyncSession


async def record_portal_action(
    db: AsyncSession,
    *,
    customer_id: str | None,
    identity_id: str | None,
    action: str,
    route: str,
    method: str,
    success: bool,
    error_rule: str | None = None,
    step_up_consumed: bool = False,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Append a ``portal_action`` row.

    Doctrine: write success AND failure paths. Forensic queries
    ("did customer X authorise this") need the failed-attempt rows
    just as much as the successful ones — a flurry of failures on a
    single customer is a compromise signal.
    """
    db.add(
        PortalAction(
            ts=clock_now(),
            customer_id=customer_id,
            identity_id=identity_id,
            action=action,
            route=route,
            method=method,
            success=success,
            error_rule=error_rule,
            step_up_consumed=step_up_consumed,
            ip=ip,
            user_agent=user_agent,
        )
    )
    await db.flush()
