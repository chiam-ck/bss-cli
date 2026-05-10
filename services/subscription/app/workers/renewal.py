"""v0.18 — automated subscription-renewal worker.

In-process tick loop attached to the subscription service's lifespan
(`app.dependencies.lifespan`). The worker is the ONLY place that
triggers renewals automatically; the manual paths
(`subscription.renew_now` cockpit tool, `bss subscription renew` CLI,
scenario action) remain as operator escape hatches.

Doctrine (CLAUDE.md v0.18+):
- Worker calls ``SubscriptionService.renew(sub_id)`` and nothing else.
  No re-implementation of charge logic, no parallel state machine.
- Trigger lives only in this module + the lifespan that starts it.
  Greppable: `_renewal_tick_loop` / `_sweep_due` / `_sweep_skipped`
  must not appear in any other production code path.

Correctness invariants:

1. **Mark-before-dispatch.** The SELECT-FOR-UPDATE-SKIP-LOCKED batch
   commits the `last_renewal_attempted_at` write BEFORE releasing the
   row locks. A peer replica running its own sweep at the same moment
   sees the marked dedup column the instant the lock is gone, so it
   skips the row. Without this ordering, two replicas can each grab a
   batch, both write the mark, both proceed to charge — double billing.

2. **Per-id session for dispatch.** Each `renew()` call runs in its own
   `async with session_factory()` so a single subscription's failure
   does NOT poison the rest of the batch. Errors per id log and
   continue.

3. **ContextVar reset in finally.** The publisher reads
   `actor`/`channel`/`service_identity` from `auth_context._current`,
   which is per-asyncio-Task. The worker is one long-lived Task, so
   values would leak across iterations. `auth_context.push()` returns
   a Token; `pop(token)` in `finally` resets.

4. **Cancellation semantics.** The outer loop catches
   `asyncio.CancelledError` to log the shutdown signal and re-raise.
   In-flight `renew()` calls are NOT shielded — they roll back via
   their `async with session_factory()` and the next process picks
   them up because `last_renewal_attempted_at` was committed by the
   (already-flushed) SELECT-txn.

5. **Wall-clock interval, bss_clock WHERE.** `asyncio.sleep(interval)`
   uses real wall-clock time so the loop's cadence is the operator
   contract (`BSS_RENEWAL_TICK_SECONDS`). The WHERE clause uses
   `bss_clock.now()` so frozen-clock scenarios drive the worker
   deterministically.
"""

from __future__ import annotations

import asyncio
import os

import structlog
from bss_clock import now as clock_now

from app import auth_context
from app.events import publisher
from app.policies.base import PolicyViolation
from app.repositories.subscription_repo import SubscriptionRepository
from app.repositories.vas_repo import VasPurchaseRepository
from app.services.subscription_service import SubscriptionService

log = structlog.get_logger()

_BATCH_LIMIT = 100
_WORKER_ACTOR = "system:renewal_worker"
_WORKER_CHANNEL = "system"

# v0.18 — upcoming-renewal reminder default lookahead. The reminder
# sweep selects subs whose next_renewal_at falls inside
# `[now, now + REMINDER_LOOKAHEAD_SECONDS]`. 24h is the obvious
# operator default ("your bundle renews tomorrow"); a future v0.x can
# extend to a multi-tier ladder (3d / 1d / 1h) with three columns.
_REMINDER_LOOKAHEAD_SECONDS_DEFAULT = 24 * 60 * 60


async def _build_plan_name_map(
    catalog_client, offering_ids: set[str]
) -> dict[str, str]:
    """Per-sweep cache of offering_id → human name. Pulls from the
    catalog so any plan added at runtime (PLAN_XL, PLAN_CNY, etc.)
    renders with its real name in renewal-reminder emails — no source
    edit required (#36). Falls back to the id on lookup failure so a
    catalog blip can't block the reminder sweep."""
    out: dict[str, str] = {}
    for oid in offering_ids:
        try:
            offering = await catalog_client.get_offering(oid)
            out[oid] = offering.get("name") or oid
        except Exception:
            out[oid] = oid
    return out


def _format_renewal_date(dt) -> str:
    """Format ``next_renewal_at`` for the reminder body. Day-Month-Year
    in operator timezone-agnostic form (UTC). Email recipients see a
    short, unambiguous date — not an ISO timestamp."""
    return dt.strftime("%-d %b %Y")


def _extract_email(customer: dict) -> str | None:
    """Pull the primary email from a TMF customer payload.

    The CRM response shape is ``contactMedium: [{mediumType, value, isPrimary}, ...]``.
    Prefer is_primary=True email; fall back to the first email found.
    Returns None if no email medium exists (rare — every signup
    requires one — but handled gracefully so the worker logs and
    skips instead of crashing).
    """
    mediums = customer.get("contactMedium") or []
    primary_email = None
    any_email = None
    for m in mediums:
        if (m.get("mediumType") or "").lower() != "email":
            continue
        value = m.get("value")
        if not value:
            continue
        if m.get("isPrimary") and primary_email is None:
            primary_email = value
        if any_email is None:
            any_email = value
    return primary_email or any_email


async def _renewal_tick_loop(app, interval_seconds: int) -> None:
    """Forever loop. Sweep due + skipped, sleep interval, repeat.

    Crashes inside a sweep are logged and swallowed — the loop survives
    a transient DB blip without taking down the lifespan task. Only
    `asyncio.CancelledError` exits the loop (clean shutdown).
    """
    log.info(
        "renewal.worker.started",
        interval_seconds=interval_seconds,
        batch_limit=_BATCH_LIMIT,
    )
    try:
        while True:
            try:
                await _sweep_due(app)
                await _sweep_skipped(app)
                await _sweep_upcoming_renewal_reminder(app)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("renewal.worker.sweep_crashed")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        log.info("renewal.worker.cancelled")
        raise


async def _sweep_due(app) -> None:
    """Find active+due subs, mark each, dispatch renew() per id.

    Two transactions per sweep:
    1. SELECT FOR UPDATE SKIP LOCKED + bulk UPDATE mark + commit.
    2. Per-id: open fresh session, set worker auth context, call
       `service.renew(id)`, swallow per-id failures.
    """
    now = clock_now()

    # ── Txn 1: select + mark + commit ─────────────────────────────────
    async with app.state.session_factory() as session:
        repo = SubscriptionRepository(session)
        ids = await repo.due_for_renewal(now=now, limit=_BATCH_LIMIT)
        if not ids:
            return
        await repo.mark_renewal_attempted(ids=ids, at=now)
        await session.commit()

    # ── Txn 2 (one per id): dispatch via the canonical service.renew ──
    for sub_id in ids:
        token = auth_context.push(
            actor=_WORKER_ACTOR,
            channel=_WORKER_CHANNEL,
        )
        try:
            async with app.state.session_factory() as session:
                svc = SubscriptionService(
                    session=session,
                    repo=SubscriptionRepository(session),
                    vas_repo=VasPurchaseRepository(session),
                    crm_client=app.state.crm_client,
                    payment_client=app.state.payment_client,
                    catalog_client=app.state.catalog_client,
                    inventory_client=app.state.inventory_client,
                )
                await svc.renew(sub_id)
                log.info(
                    "renewal.worker.dispatched",
                    subscription_id=sub_id,
                )
        except PolicyViolation as exc:
            log.warning(
                "renewal.worker.policy_violation",
                subscription_id=sub_id,
                rule=exc.rule,
                message=exc.message,
            )
        except Exception:
            log.exception(
                "renewal.worker.dispatch_failed",
                subscription_id=sub_id,
            )
        finally:
            auth_context.pop(token)


async def _sweep_skipped(app) -> None:
    """Find blocked+overdue subs, emit `subscription.renewal_skipped`, mark.

    No `renew()` call — blocked subs need an explicit operator
    intervention (top-up, payment-method update). The skipped event is
    a cheap signal for the cockpit dashboard so an operator can nudge
    the customer; it is NOT a retry trigger.

    Single transaction per sweep: emit all events + bulk mark + commit.
    """
    now = clock_now()
    token = auth_context.push(
        actor=_WORKER_ACTOR,
        channel=_WORKER_CHANNEL,
    )
    try:
        async with app.state.session_factory() as session:
            repo = SubscriptionRepository(session)
            ids = await repo.overdue_blocked(now=now, limit=_BATCH_LIMIT)
            if not ids:
                return
            for sub_id in ids:
                await publisher.publish(
                    session,
                    event_type="subscription.renewal_skipped",
                    aggregate_type="subscription",
                    aggregate_id=sub_id,
                    payload={
                        "subscriptionId": sub_id,
                        "reason": "blocked",
                        "skippedAt": now.isoformat(),
                    },
                    exchange=app.state.mq_exchange,
                )
            await repo.mark_renewal_attempted(ids=ids, at=now)
            await session.commit()
            log.info(
                "renewal.worker.skipped_emitted",
                count=len(ids),
            )
    finally:
        auth_context.pop(token)


async def _sweep_upcoming_renewal_reminder(app) -> None:
    """v0.18 — find subs whose renewal falls inside the lookahead window
    and send the customer a reminder email.

    Lookahead = ``BSS_RENEWAL_REMINDER_LOOKAHEAD_SECONDS`` (default 86400 = 24h).
    Set to 0 to disable the reminder sweep entirely (the renewal sweep
    keeps running). The email adapter on ``app.state.email_adapter``
    is the same Protocol the portal uses; in dev (logging adapter)
    the reminder lands in the dev mailbox file.

    Same FOR UPDATE SKIP LOCKED + dedup-column-in-SELECT-txn pattern
    as the renewal sweep. Multi-replica safe.

    The customer email is fetched via the CRM client per-id (one HTTP
    call per due reminder). At thousand-customer scale + 24h lookahead
    this is at most a few hundred calls per sweep — acceptable. A
    future v0.x can cache the email on subscription.row at signup if
    the call pattern becomes hot.

    Errors per id (CRM 404, missing email, mail-send failure) are
    logged + swallowed so a single bad row doesn't block the rest of
    the batch. The dedup column is set BEFORE the per-id loop so a
    crash mid-send doesn't replay the reminder on the next tick;
    a row whose CRM lookup or email send failed will simply not
    re-attempt this period (acceptable — the renewal itself still
    fires, the reminder is the soft signal).
    """
    if app.state.email_adapter is None:
        return
    lookahead = int(
        os.environ.get(
            "BSS_RENEWAL_REMINDER_LOOKAHEAD_SECONDS",
            str(_REMINDER_LOOKAHEAD_SECONDS_DEFAULT),
        )
    )
    if lookahead <= 0:
        return

    now = clock_now()

    # ── Txn 1: select + mark + commit ─────────────────────────────────
    async with app.state.session_factory() as session:
        repo = SubscriptionRepository(session)
        rows = await repo.due_for_reminder(
            now=now,
            lookahead_seconds=lookahead,
            limit=_BATCH_LIMIT,
        )
        if not rows:
            return
        ids = [r["id"] for r in rows]
        await repo.mark_reminder_sent(ids=ids, at=now)
        await session.commit()

    # Catalog lookups for plan names happen once per unique offering
    # in the batch — not once per row.
    plan_names = await _build_plan_name_map(
        app.state.catalog_client,
        {r["offering_id"] for r in rows},
    )

    # ── Txn 2 (per id): CRM lookup + email send + log ─────────────────
    token = auth_context.push(actor=_WORKER_ACTOR, channel=_WORKER_CHANNEL)
    sent = 0
    skipped_no_email = 0
    failed = 0
    try:
        for row in rows:
            sub_id = row["id"]
            try:
                customer = await app.state.crm_client.get_customer(
                    row["customer_id"]
                )
            except Exception:
                log.exception(
                    "renewal.reminder.crm_lookup_failed",
                    subscription_id=sub_id,
                    customer_id=row["customer_id"],
                )
                failed += 1
                continue

            email = _extract_email(customer)
            if not email:
                log.warning(
                    "renewal.reminder.no_email_on_customer",
                    subscription_id=sub_id,
                    customer_id=row["customer_id"],
                )
                skipped_no_email += 1
                continue

            try:
                app.state.email_adapter.send_renewal_reminder(
                    email,
                    plan_name=plan_names.get(row["offering_id"], row["offering_id"]),
                    msisdn=row["msisdn"],
                    amount=f"{row['price_amount']:.2f}",
                    currency=row["price_currency"],
                    renewal_date=_format_renewal_date(row["next_renewal_at"]),
                )
                sent += 1
                log.info(
                    "renewal.reminder.sent",
                    subscription_id=sub_id,
                    plan=row["offering_id"],
                )
            except Exception:
                log.exception(
                    "renewal.reminder.send_failed",
                    subscription_id=sub_id,
                )
                failed += 1
    finally:
        auth_context.pop(token)

    log.info(
        "renewal.reminder.sweep_complete",
        candidates=len(rows),
        sent=sent,
        skipped_no_email=skipped_no_email,
        failed=failed,
    )
