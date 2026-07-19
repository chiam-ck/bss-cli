"""MSISDN pool repository."""


from bss_clock import now as clock_now
from bss_models.inventory import MsisdnPool
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


class MsisdnRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, msisdn: str) -> MsisdnPool | None:
        result = await self._s.execute(
            select(MsisdnPool).where(MsisdnPool.msisdn == msisdn)
        )
        return result.scalar_one_or_none()

    async def list_msisdns(
        self,
        *,
        status: str | None = None,
        prefix: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[MsisdnPool]:
        stmt = select(MsisdnPool).limit(limit).offset(offset).order_by(MsisdnPool.msisdn)
        if status:
            stmt = stmt.where(MsisdnPool.status == status)
        if prefix:
            stmt = stmt.where(MsisdnPool.msisdn.startswith(prefix))
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def reserve_atomic(
        self, msisdn: str, tenant_id: str
    ) -> MsisdnPool | None:
        """Atomically reserve a specific MSISDN using FOR UPDATE SKIP LOCKED."""
        result = await self._s.execute(
            text("""
                SELECT msisdn FROM inventory.msisdn_pool
                WHERE msisdn = :m AND status = 'available' AND tenant_id = :t
                FOR UPDATE SKIP LOCKED
            """),
            {"m": msisdn, "t": tenant_id},
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        await self._s.execute(
            text("""
                UPDATE inventory.msisdn_pool
                SET status = 'reserved', reserved_at = :now, updated_at = :now
                WHERE msisdn = :m
            """),
            {"m": msisdn, "now": clock_now()},
        )
        # Re-fetch as ORM object
        return await self.get(msisdn)

    async def reserve_next_available(self, tenant_id: str) -> MsisdnPool | None:
        """Reserve the next available MSISDN atomically."""
        result = await self._s.execute(
            text("""
                SELECT msisdn FROM inventory.msisdn_pool
                WHERE status = 'available' AND tenant_id = :t
                ORDER BY msisdn LIMIT 1
                FOR UPDATE SKIP LOCKED
            """),
            {"t": tenant_id},
        )
        msisdn = result.scalar_one_or_none()
        if not msisdn:
            return None
        await self._s.execute(
            text("""
                UPDATE inventory.msisdn_pool
                SET status = 'reserved', reserved_at = :now, updated_at = :now
                WHERE msisdn = :m
            """),
            {"m": msisdn, "now": clock_now()},
        )
        return await self.get(msisdn)

    async def update_status(self, msisdn: str, status: str) -> MsisdnPool | None:
        row = await self.get(msisdn)
        if row:
            row.status = status
            if status == "available":
                row.reserved_at = None
                row.assigned_to_subscription_id = None
            await self._s.flush()
        return row

    async def add_range(
        self, prefix: str, count: int, tenant_id: str
    ) -> tuple[int, int]:
        """Bulk-insert ``count`` MSISDNs starting at ``{prefix}{0:04d}``.

        Uses ``ON CONFLICT (msisdn) DO NOTHING`` so partially-overlapping
        ranges are safe to re-run without duplicate failures, and so a
        port-in-seeded number inside the prefix's footprint is not
        overwritten. Returns ``(inserted, skipped)``.

        v0.17 doctrine: terminal ``ported_out`` rows inside the prefix
        are skipped (not re-issued) — the ON CONFLICT path handles this
        naturally because the row already exists.
        """
        rows = [
            {
                "msisdn": f"{prefix}{i:04d}",
                "status": "available",
                "tenant_id": tenant_id,
            }
            for i in range(count)
        ]
        result = await self._s.execute(
            text(
                """
                INSERT INTO inventory.msisdn_pool
                    (msisdn, status, tenant_id)
                SELECT msisdn, status, tenant_id FROM jsonb_to_recordset(
                    CAST(:rows AS jsonb)
                ) AS x(msisdn text, status text, tenant_id text)
                ON CONFLICT (msisdn) DO NOTHING
                RETURNING msisdn
                """
            ),
            {"rows": _to_jsonb(rows)},
        )
        inserted = len(result.scalars().all())
        await self._s.flush()
        return inserted, count - inserted

    async def count_available(self, tenant_id: str) -> int:
        result = await self._s.execute(
            text(
                """
                SELECT COUNT(*) FROM inventory.msisdn_pool
                WHERE status = 'available' AND tenant_id = :t
                """
            ),
            {"t": tenant_id},
        )
        return int(result.scalar_one())

    async def count_by_status(
        self, *, tenant_id: str, prefix: str | None = None
    ) -> dict[str, int]:
        """Group-by-status count of the pool, optionally narrowed to a
        ``startswith`` prefix.

        Returns a dict keyed by status string with int values; statuses
        not present resolve to 0. Always includes the synthetic ``total``
        key as the sum across all states. The query joins against the
        canonical state set so callers can rely on the same keys (e.g.
        ``available``, ``reserved``, ``assigned``, ``ported_out``)
        without consulting the rows.
        """
        if prefix is not None:
            result = await self._s.execute(
                text(
                    """
                    SELECT status, COUNT(*) AS n
                    FROM inventory.msisdn_pool
                    WHERE tenant_id = :t
                      AND msisdn LIKE :pfx
                    GROUP BY status
                    """
                ),
                {"t": tenant_id, "pfx": f"{prefix}%"},
            )
        else:
            result = await self._s.execute(
                text(
                    """
                    SELECT status, COUNT(*) AS n
                    FROM inventory.msisdn_pool
                    WHERE tenant_id = :t
                    GROUP BY status
                    """
                ),
                {"t": tenant_id},
            )
        rows = {row.status: int(row.n) for row in result.all()}
        # Canonical key set — present 0 for absent statuses so consumers
        # can render a stable table.
        for canonical in ("available", "reserved", "assigned", "ported_out"):
            rows.setdefault(canonical, 0)
        rows["total"] = sum(v for k, v in rows.items() if k != "total")
        return rows

    async def mark_ported_out(
        self, msisdn: str, *, subscription_id: str | None = None
    ) -> MsisdnPool | None:
        """Set terminal ``ported_out`` status with far-future quarantine.

        v0.17 doctrine: this status is NEVER reversed; the donor
        carrier owns the number now. Reserve-next predicate
        (``status='available'``) skips it by construction.
        """
        await self._s.execute(
            text(
                """
                UPDATE inventory.msisdn_pool
                SET status = 'ported_out',
                    quarantine_until = '9999-12-31'::timestamptz,
                    assigned_to_subscription_id = COALESCE(:sub, assigned_to_subscription_id),
                    updated_at = :now
                WHERE msisdn = :m
                """
            ),
            {"m": msisdn, "sub": subscription_id, "now": clock_now()},
        )
        await self._s.flush()
        return await self.get(msisdn)


def _to_jsonb(rows: list[dict]) -> str:
    import json

    return json.dumps(rows)
