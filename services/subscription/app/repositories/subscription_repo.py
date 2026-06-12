"""Subscription repository — CRUD + sequence IDs."""

from datetime import datetime

from bss_models.subscription import BundleBalance, Subscription, SubscriptionStateHistory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


class SubscriptionRepository:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def next_id(self) -> str:
        result = await self._s.execute(
            text("SELECT nextval('subscription.subscription_id_seq')")
        )
        seq = result.scalar_one()
        return f"SUB-{seq:04d}"

    async def create(self, sub: Subscription) -> Subscription:
        self._s.add(sub)
        await self._s.flush()
        return sub

    async def get(self, sub_id: str) -> Subscription | None:
        from sqlalchemy import select

        stmt = (
            select(Subscription)
            .where(Subscription.id == sub_id)
            .options(
                selectinload(Subscription.balances),
                selectinload(Subscription.vas_purchases),
                selectinload(Subscription.state_history),
            )
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_msisdn(self, msisdn: str) -> Subscription | None:
        from sqlalchemy import select

        stmt = (
            select(Subscription)
            .where(Subscription.msisdn == msisdn)
            .options(
                selectinload(Subscription.balances),
                selectinload(Subscription.vas_purchases),
            )
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_commercial_order(self, order_id: str) -> Subscription | None:
        """v1.2 — idempotency lookup for create(). Returns the subscription
        already created for this commercial order, if any."""
        from sqlalchemy import select

        stmt = (
            select(Subscription)
            .where(Subscription.commercial_order_id == order_id)
            .options(
                selectinload(Subscription.balances),
                selectinload(Subscription.vas_purchases),
                selectinload(Subscription.state_history),
            )
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_customer(self, customer_id: str) -> list[Subscription]:
        from sqlalchemy import select

        stmt = (
            select(Subscription)
            .where(Subscription.customer_id == customer_id)
            .options(selectinload(Subscription.balances))
            .order_by(Subscription.created_at.desc())
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def list_active_for_offering(self, offering_id: str) -> list[Subscription]:
        """Subscriptions on `offering_id` that are still in a renewable state."""
        from sqlalchemy import select

        stmt = (
            select(Subscription)
            .where(Subscription.offering_id == offering_id)
            .where(Subscription.state.in_(["active", "blocked"]))
            .order_by(Subscription.id)
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def update(self, sub: Subscription) -> Subscription:
        await self._s.flush()
        return sub

    async def add_state_history(self, entry: SubscriptionStateHistory) -> None:
        self._s.add(entry)
        await self._s.flush()

    async def add_balance(self, balance: BundleBalance) -> None:
        self._s.add(balance)
        await self._s.flush()

    async def get_balances(self, sub_id: str) -> list[BundleBalance]:
        from sqlalchemy import select

        stmt = select(BundleBalance).where(BundleBalance.subscription_id == sub_id)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def get_balance_for_update(
        self, sub_id: str, allowance_type: str
    ) -> BundleBalance | None:
        """SELECT ... FOR UPDATE on the balance row.

        Serializes concurrent decrement attempts per (subscription, allowance).
        See DECISIONS.md Phase 8 for why Option A was chosen over B/C.

        ``populate_existing=True`` is load-bearing here: the caller almost
        always reaches this method via ``handle_usage_rated``, which first
        calls ``self._repo.get(sub_id)`` — and that method ``selectinload``s
        ``Subscription.balances``, populating the session's identity map
        with this same row. Without ``populate_existing``, SQLAlchemy
        returns the CACHED Python object (with ``consumed`` from before
        any concurrent transaction's commit), even though the SQL query
        DOES re-hit Postgres and the DB lock IS held. Result: two
        concurrent ``handle_usage_rated`` events for the same balance
        each read ``consumed=0``, both write their delta on top of zero,
        and the second commit overwrites the first instead of accumulating.
        See ``customer_signup_and_exhaust`` flake history for the
        reproducer pattern (back-to-back usage events, prefetch_count=5).
        """
        from sqlalchemy import select

        stmt = (
            select(BundleBalance)
            .where(BundleBalance.subscription_id == sub_id)
            .where(BundleBalance.allowance_type == allowance_type)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def update_balance(self, balance: BundleBalance) -> None:
        await self._s.flush()

    # ── v0.18 — renewal worker sweeps ───────────────────────────────────
    #
    # Two methods backing the in-process renewal worker. Both run
    # `FOR UPDATE SKIP LOCKED` so a peer subscription replica grabs
    # disjoint rows when we eventually scale out. The caller MUST commit
    # the marking UPDATE inside this same transaction so the dedup
    # column is visible the moment the row lock releases.

    async def due_for_renewal(
        self, *, now: datetime, limit: int = 100, tenant_id: str = "DEFAULT"
    ) -> list[str]:
        """IDs of `active` subs whose period has elapsed and not yet attempted this period.

        Filtered by `last_renewal_attempted_at < next_renewal_at` so a row
        the worker has already marked in this period is skipped — when
        `next_renewal_at` advances after a successful renewal the dedup
        flips back to "due" naturally on the next period.
        """
        result = await self._s.execute(
            text(
                """
                SELECT id FROM subscription.subscription
                WHERE state = 'active'
                  AND next_renewal_at IS NOT NULL
                  AND next_renewal_at <= :now
                  AND (last_renewal_attempted_at IS NULL
                       OR last_renewal_attempted_at < next_renewal_at)
                  AND tenant_id = :tenant
                ORDER BY next_renewal_at
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"now": now, "limit": limit, "tenant": tenant_id},
        )
        return [row[0] for row in result.all()]

    async def overdue_blocked(
        self, *, now: datetime, limit: int = 100, tenant_id: str = "DEFAULT"
    ) -> list[str]:
        """IDs of `blocked` subs whose renewal boundary has passed.

        Reuses `last_renewal_attempted_at` as the dedup column so the same
        blocked sub doesn't re-emit `subscription.renewal_skipped` on every
        tick. If the customer tops up (state → active) and renews, the
        next renewal advances `next_renewal_at` past the dedup column
        value naturally — the row reappears in `due_for_renewal` for the
        following period without any cleanup.
        """
        result = await self._s.execute(
            text(
                """
                SELECT id FROM subscription.subscription
                WHERE state = 'blocked'
                  AND next_renewal_at IS NOT NULL
                  AND next_renewal_at <= :now
                  AND (last_renewal_attempted_at IS NULL
                       OR last_renewal_attempted_at < next_renewal_at)
                  AND tenant_id = :tenant
                ORDER BY next_renewal_at
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"now": now, "limit": limit, "tenant": tenant_id},
        )
        return [row[0] for row in result.all()]

    async def mark_renewal_attempted(
        self, *, ids: list[str], at: datetime
    ) -> None:
        """Bulk-set `last_renewal_attempted_at` for a batch of subscription ids.

        Called by both sweeps inside the same SELECT-FOR-UPDATE-SKIP-LOCKED
        transaction so the mark commits atomically with the lock release.
        """
        if not ids:
            return
        await self._s.execute(
            text(
                """
                UPDATE subscription.subscription
                SET last_renewal_attempted_at = :at,
                    updated_at = :at
                WHERE id = ANY(:ids)
                """
            ),
            {"at": at, "ids": ids},
        )
        await self._s.flush()

    # ── v0.18 — upcoming-renewal reminder sweep ─────────────────────────
    #
    # Selects active subs whose period boundary is within the lookahead
    # window (default 24h before next_renewal_at). Same FOR UPDATE SKIP
    # LOCKED + dedup-column-in-SELECT-txn pattern as the renewal sweep.

    async def due_for_reminder(
        self,
        *,
        now: datetime,
        lookahead_seconds: int,
        limit: int = 100,
        tenant_id: str = "DEFAULT",
    ) -> list[dict]:
        """IDs + customer/plan info for active subs whose next renewal
        falls inside the lookahead window AND we haven't reminded
        them yet for this period.

        Returns a list of dicts so a single round trip carries
        everything the worker needs to compose the reminder
        (no per-id CRM call needed if the customer email isn't
        cached on the row — caller still needs that lookup).
        """
        result = await self._s.execute(
            text(
                """
                SELECT
                    id,
                    customer_id,
                    offering_id,
                    msisdn,
                    next_renewal_at,
                    price_amount,
                    price_currency
                FROM subscription.subscription
                WHERE state = 'active'
                  AND next_renewal_at IS NOT NULL
                  AND next_renewal_at > :now
                  AND next_renewal_at <= :now + make_interval(secs => :lookahead_seconds)
                  AND (renewal_reminder_sent_at IS NULL
                       OR renewal_reminder_sent_at < next_renewal_at)
                  AND tenant_id = :tenant
                ORDER BY next_renewal_at
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
            ),
            {
                "now": now,
                "lookahead_seconds": lookahead_seconds,
                "limit": limit,
                "tenant": tenant_id,
            },
        )
        return [
            {
                "id": row[0],
                "customer_id": row[1],
                "offering_id": row[2],
                "msisdn": row[3],
                "next_renewal_at": row[4],
                "price_amount": row[5],
                "price_currency": row[6],
            }
            for row in result.all()
        ]

    async def mark_reminder_sent(
        self, *, ids: list[str], at: datetime
    ) -> None:
        """Bulk-set `renewal_reminder_sent_at` (committed in same SELECT txn)."""
        if not ids:
            return
        await self._s.execute(
            text(
                """
                UPDATE subscription.subscription
                SET renewal_reminder_sent_at = :at,
                    updated_at = :at
                WHERE id = ANY(:ids)
                """
            ),
            {"at": at, "ids": ids},
        )
        await self._s.flush()
