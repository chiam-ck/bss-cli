"""Health and readiness endpoint tests."""

from bss_models import BSS_RELEASE
from httpx import AsyncClient


class TestHealth:
    async def test_health(self, client: AsyncClient):
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "crm"
        # v0.18.1 — service version is sourced from bss_models.BSS_RELEASE
        # so a single bump propagates everywhere.
        assert body["version"] == BSS_RELEASE

    async def test_ready(self, client: AsyncClient):
        r = await client.get("/ready")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"

    async def test_request_id_header(self, client: AsyncClient):
        r = await client.get("/health")
        assert "x-request-id" in r.headers
