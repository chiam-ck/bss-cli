"""v0.20 — `--data-roaming-mb` end-to-end through the admin add-offering route.

Three assertions:
  1. Flag absent → no `data_roaming` row written (existing v0.7 behaviour preserved).
  2. Flag set → `BA_<id>_ROAM` row with `allowance_type='data_roaming'`, `unit='mb'`.
  3. Flag set to 0 → row with `quantity=0` (the "no included roaming, customer can
     still top up via VAS" pattern, mirrors seeded PLAN_S exactly).

Each test uses a unique offering id so reruns + the seeded fixtures don't conflict.
The `add-offering` admin path is `is_sellable=True`, so cleanup matters — but we
use `ON CONFLICT DO NOTHING`-shaped tests by tagging ids with a test suffix.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _unique(prefix: str) -> str:
    return f"{prefix}_TEST_{uuid.uuid4().hex[:8].upper()}"


async def _allowance_rows(session: AsyncSession, offering_id: str):
    rows = await session.execute(
        text(
            "SELECT allowance_type, quantity, unit "
            "FROM catalog.bundle_allowance "
            "WHERE offering_id = :oid "
            "ORDER BY allowance_type"
        ),
        {"oid": offering_id},
    )
    return [dict(r._mapping) for r in rows]


async def _delete_offering(session: AsyncSession, offering_id: str):
    await session.execute(
        text("DELETE FROM catalog.bundle_allowance WHERE offering_id = :oid"),
        {"oid": offering_id},
    )
    await session.execute(
        text("DELETE FROM catalog.product_offering_price WHERE offering_id = :oid"),
        {"oid": offering_id},
    )
    await session.execute(
        text("DELETE FROM catalog.product_offering WHERE id = :oid"),
        {"oid": offering_id},
    )
    await session.commit()


class TestAdminAddOfferingRoaming:
    async def test_no_roaming_flag_writes_no_roaming_row(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        offering_id = _unique("PLAN_NOROAM")
        try:
            r = await client.post(
                "/admin/catalog/offering",
                json={
                    "offeringId": offering_id,
                    "name": "Test no-roaming",
                    "amount": "5.00",
                    "currency": "SGD",
                    "dataMb": 5120,
                },
                headers={"X-BSS-Actor": "admin"},
            )
            assert r.status_code == 200, r.text

            rows = await _allowance_rows(db_session, offering_id)
            types = {row["allowance_type"] for row in rows}
            assert "data" in types
            assert "data_roaming" not in types
        finally:
            await _delete_offering(db_session, offering_id)

    async def test_roaming_flag_writes_data_roaming_row(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        offering_id = _unique("PLAN_ROAM")
        try:
            r = await client.post(
                "/admin/catalog/offering",
                json={
                    "offeringId": offering_id,
                    "name": "Test with-roaming",
                    "amount": "9.00",
                    "currency": "SGD",
                    "dataMb": 5120,
                    "dataRoamingMb": 1024,
                },
                headers={"X-BSS-Actor": "admin"},
            )
            assert r.status_code == 200, r.text

            rows = await _allowance_rows(db_session, offering_id)
            roam = next((row for row in rows if row["allowance_type"] == "data_roaming"), None)
            assert roam is not None, f"no data_roaming row; got {rows}"
            assert roam["quantity"] == 1024
            assert roam["unit"] == "mb"
        finally:
            await _delete_offering(db_session, offering_id)

    async def test_roaming_flag_zero_writes_zero_row(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Mirrors seeded PLAN_S — included-roaming = 0 mb, but row exists so
        VAS top-up has the row to increment (or so VAS materialisation has a
        consistent shape). Either way, 0 must be a valid allowance quantity."""
        offering_id = _unique("PLAN_ZEROROAM")
        try:
            r = await client.post(
                "/admin/catalog/offering",
                json={
                    "offeringId": offering_id,
                    "name": "Test zero-roaming",
                    "amount": "5.00",
                    "currency": "SGD",
                    "dataMb": 5120,
                    "dataRoamingMb": 0,
                },
                headers={"X-BSS-Actor": "admin"},
            )
            assert r.status_code == 200, r.text

            rows = await _allowance_rows(db_session, offering_id)
            roam = next((row for row in rows if row["allowance_type"] == "data_roaming"), None)
            assert roam is not None, f"no data_roaming row; got {rows}"
            assert roam["quantity"] == 0
        finally:
            await _delete_offering(db_session, offering_id)
