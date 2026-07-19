"""v0.3 — every Catalog endpoint requires X-BSS-API-Token."""

from __future__ import annotations

import pytest
from bss_catalog.app import create_app
from bss_catalog.config import Settings
from bss_middleware import AUTH_INVALID_TOKEN, AUTH_MISSING_TOKEN
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def naked_client(settings: Settings):
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_exempt_without_token(naked_client: AsyncClient) -> None:
    r = await naked_client.get("/health")
    assert r.status_code == 200


async def test_no_token_returns_401_missing(naked_client: AsyncClient) -> None:
    r = await naked_client.get("/any-path-the-middleware-rejects-first")
    assert r.status_code == 401
    assert r.json()["code"] == AUTH_MISSING_TOKEN


async def test_wrong_token_returns_401_invalid(naked_client: AsyncClient) -> None:
    r = await naked_client.get(
        "/any-path-the-middleware-rejects-first",
        headers={"X-BSS-API-Token": "definitely-wrong"},
    )
    assert r.status_code == 401
    assert r.json()["code"] == AUTH_INVALID_TOKEN
