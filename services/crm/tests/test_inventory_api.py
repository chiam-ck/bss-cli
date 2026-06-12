"""Inventory API tests — MSISDN + eSIM."""

from bss_models.inventory import MsisdnPool
from httpx import AsyncClient

MSISDN_PREFIX = "/inventory-api/v1"
ESIM_PREFIX = "/inventory-api/v1"


async def _seed_msisdn(db_session, msisdn: str, status: str = "available") -> None:
    """Seed a test-owned MSISDN row inside the rollback transaction.

    Mutation tests must NOT target seed-pool numbers (9000xxxx): the
    dev DB recycles those through signups, so a number that was
    ``available`` at seed time may be ``assigned`` today —
    test_release_available_fails failed exactly that way when 90000008
    got assigned to a live subscription (2026-06-12). The 8000xxxx
    range is outside the seed and exists only inside this test's
    transaction.
    """
    db_session.add(MsisdnPool(msisdn=msisdn, status=status, tenant_id="DEFAULT"))
    await db_session.flush()


class TestMsisdn:
    async def test_list_msisdns(self, client: AsyncClient):
        r = await client.get(f"{MSISDN_PREFIX}/msisdn", params={"status": "available", "limit": 5})
        assert r.status_code == 200
        body = r.json()
        assert len(body) <= 5
        if body:
            assert body[0]["status"] == "available"

    async def test_get_msisdn(self, client: AsyncClient):
        r = await client.get(f"{MSISDN_PREFIX}/msisdn/90000005")
        assert r.status_code == 200
        assert r.json()["msisdn"] == "90000005"

    async def test_get_msisdn_not_found(self, client: AsyncClient):
        r = await client.get(f"{MSISDN_PREFIX}/msisdn/99999999")
        assert r.status_code == 404

    async def test_reserve_msisdn(self, client: AsyncClient, db_session):
        await _seed_msisdn(db_session, "80000001")
        r = await client.post(f"{MSISDN_PREFIX}/msisdn/80000001/reserve")
        assert r.status_code == 200
        assert r.json()["status"] == "reserved"

    async def test_reserve_already_reserved(self, client: AsyncClient, db_session):
        await _seed_msisdn(db_session, "80000002")
        r1 = await client.post(f"{MSISDN_PREFIX}/msisdn/80000002/reserve")
        assert r1.status_code == 200
        r = await client.post(f"{MSISDN_PREFIX}/msisdn/80000002/reserve")
        assert r.status_code == 422

    async def test_release_msisdn(self, client: AsyncClient, db_session):
        await _seed_msisdn(db_session, "80000003")
        r1 = await client.post(f"{MSISDN_PREFIX}/msisdn/80000003/reserve")
        assert r1.status_code == 200
        r = await client.post(f"{MSISDN_PREFIX}/msisdn/80000003/release")
        assert r.status_code == 200
        assert r.json()["status"] == "available"

    async def test_release_available_fails(self, client: AsyncClient, db_session):
        await _seed_msisdn(db_session, "80000004")
        r = await client.post(f"{MSISDN_PREFIX}/msisdn/80000004/release")
        assert r.status_code == 422
        assert r.json()["reason"] == "msisdn.release.only_if_reserved_or_assigned"

    async def test_count_msisdns_returns_canonical_state_keys(
        self, client: AsyncClient
    ):
        r = await client.get(f"{MSISDN_PREFIX}/msisdn/count")
        assert r.status_code == 200
        body = r.json()
        for key in ("available", "reserved", "assigned", "ported_out", "total"):
            assert key in body
            assert isinstance(body[key], int)
        # total must equal the sum of state buckets — invariant the
        # cockpit relies on for "is that all?" follow-ups.
        assert body["total"] == (
            body["available"] + body["reserved"]
            + body["assigned"] + body["ported_out"]
        )
        # Seed has 1000 available numbers — sanity check the floor.
        assert body["total"] >= body["available"] >= 1

    async def test_count_msisdns_with_prefix_narrows(
        self, client: AsyncClient
    ):
        # Seed numbers all start with 9000; an unrelated prefix is empty.
        r_match = await client.get(
            f"{MSISDN_PREFIX}/msisdn/count", params={"prefix": "9000"}
        )
        r_miss = await client.get(
            f"{MSISDN_PREFIX}/msisdn/count", params={"prefix": "1234"}
        )
        assert r_match.status_code == 200 and r_miss.status_code == 200
        assert r_match.json()["total"] >= 1
        assert r_match.json()["prefix"] == "9000"
        assert r_miss.json()["total"] == 0
        assert r_miss.json()["prefix"] == "1234"


class TestEsim:
    async def test_list_esims(self, client: AsyncClient):
        r = await client.get(f"{ESIM_PREFIX}/esim", params={"status": "available", "limit": 5})
        assert r.status_code == 200
        body = r.json()
        assert len(body) <= 5

    async def test_reserve_esim(self, client: AsyncClient):
        r = await client.post(f"{ESIM_PREFIX}/esim/reserve")
        assert r.status_code == 201
        body = r.json()
        assert body["profile_state"] == "reserved"
        assert body["iccid"].startswith("8910")

    async def test_esim_activation_code(self, client: AsyncClient):
        # Reserve first
        r = await client.post(f"{ESIM_PREFIX}/esim/reserve")
        iccid = r.json()["iccid"]

        r = await client.get(f"{ESIM_PREFIX}/esim/{iccid}/activation")
        assert r.status_code == 200
        body = r.json()
        assert body["activation_code"].startswith("LPA:1$smdp.bss-cli.local$")
        assert body["smdp_server"] == "smdp.bss-cli.local"

    async def test_esim_lifecycle(self, client: AsyncClient):
        # Reserve
        r = await client.post(f"{ESIM_PREFIX}/esim/reserve")
        iccid = r.json()["iccid"]

        # Mark downloaded
        r = await client.post(f"{ESIM_PREFIX}/esim/{iccid}/mark-downloaded")
        assert r.status_code == 200
        assert r.json()["profile_state"] == "downloaded"

        # Mark activated
        r = await client.post(f"{ESIM_PREFIX}/esim/{iccid}/mark-activated")
        assert r.status_code == 200
        assert r.json()["profile_state"] == "activated"

    async def test_esim_release_from_reserved(self, client: AsyncClient):
        r = await client.post(f"{ESIM_PREFIX}/esim/reserve")
        iccid = r.json()["iccid"]

        r = await client.post(f"{ESIM_PREFIX}/esim/{iccid}/recycle")
        # Can't recycle from reserved — invalid transition
        assert r.status_code == 422

    async def test_esim_invalid_transition(self, client: AsyncClient):
        # Get an available eSIM
        r = await client.get(f"{ESIM_PREFIX}/esim", params={"status": "available", "limit": 1})
        if r.json():
            iccid = r.json()[0]["iccid"]
            # Can't mark-downloaded from available
            r = await client.post(f"{ESIM_PREFIX}/esim/{iccid}/mark-downloaded")
            assert r.status_code == 422
