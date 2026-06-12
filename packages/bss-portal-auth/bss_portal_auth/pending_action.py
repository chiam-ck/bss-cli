"""POST-body stash so a step-up bounce doesn't lose the customer's typed input.

When ``requires_step_up`` raises ``StepUpRequired`` on a POST, the
route handler hasn't run — but the customer already typed their
intended values into the form. Without stashing, the OTP flow's 303
bounce-back lands on a fresh GET form and the customer types again.

Flow:

* On ``StepUpRequired``: portal calls ``stash_pending_action`` with
  the (filtered) form payload, the original POST URL, and the
  ``(session_id, action_label)`` key.
* On ``verify_step_up`` success: portal calls ``consume_pending_action``
  to atomically take the stash and renders an auto-replay page that
  POSTs to ``target_url`` with the stashed fields. The fresh step-up
  cookie rides along with the replay POST.

A second StepUpRequired for the same (session, label) supersedes the
prior unconsumed row — partial unique index enforces "one in-flight".
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

from bss_clock import now as clock_now
from bss_models import Session, StepUpPendingAction
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings

# Form fields we never stash — they are auth-flow internals that would
# either be stale (consumed) or irrelevant on the replay POST.
_STRIP_FIELDS = frozenset({"step_up_token"})


@dataclass(frozen=True)
class PendingActionView:
    """Read-only projection of a stashed pending action."""

    id: str
    session_id: str
    action_label: str
    target_url: str
    payload: Mapping[str, str]
    expires_at: datetime


def _id() -> str:
    return f"SUP-{secrets.token_hex(8)}"


def _filter_payload(payload: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in payload.items() if k not in _STRIP_FIELDS}


async def stash_pending_action(
    db: AsyncSession,
    *,
    session_id: str,
    action_label: str,
    target_url: str,
    payload: Mapping[str, str],
    ttl_s: int | None = None,
) -> str:
    """Stash a POST body for later replay after step-up verification.

    Supersedes any prior unconsumed row for ``(session_id,
    action_label)``: marks the prior row consumed (so the partial
    unique index admits the new insert) and inserts a fresh row.
    Returns the new row id.

    Raises ``ValueError`` if the session does not exist or is revoked
    — there is no point stashing a payload for a session that can't
    consume it.
    """
    sess = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if sess is None or sess.revoked_at is not None:
        raise ValueError("session not found or revoked")

    now = clock_now()
    settings = Settings()
    ttl = ttl_s if ttl_s is not None else settings.BSS_PORTAL_STEPUP_PENDING_TTL_S
    expires = now + timedelta(seconds=ttl)

    # Supersede any prior in-flight stash for this (session, label).
    # Marking consumed_at clears the partial unique index so the
    # new insert can proceed.
    await db.execute(
        update(StepUpPendingAction)
        .where(
            StepUpPendingAction.session_id == session_id,
            StepUpPendingAction.action_label == action_label,
            StepUpPendingAction.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )

    row_id = _id()
    db.add(
        StepUpPendingAction(
            id=row_id,
            session_id=session_id,
            action_label=action_label,
            target_url=target_url,
            payload_json=_filter_payload(payload),
            created_at=now,
            expires_at=expires,
        )
    )
    await db.flush()
    return row_id


async def consume_pending_action(
    db: AsyncSession, *, session_id: str, action_label: str
) -> PendingActionView | None:
    """Atomically take the most recent unconsumed stash for the key.

    Returns ``None`` if nothing is in flight, or if the row has
    expired. On hit, marks the row consumed (one-shot) and returns a
    read-only view the caller can use to render the replay form.
    """
    rows = (
        await db.execute(
            select(StepUpPendingAction).where(
                StepUpPendingAction.session_id == session_id,
                StepUpPendingAction.action_label == action_label,
                StepUpPendingAction.consumed_at.is_(None),
            )
        )
    ).scalars().all()

    now = clock_now()
    for row in rows:
        if row.expires_at <= now:
            continue
        row.consumed_at = now
        await db.flush()
        return PendingActionView(
            id=row.id,
            session_id=row.session_id,
            action_label=row.action_label,
            target_url=row.target_url,
            payload=dict(row.payload_json),
            expires_at=row.expires_at,
        )
    return None
