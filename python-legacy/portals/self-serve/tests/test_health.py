"""Scaffold smoke test: /health returns 200 and identifies the portal."""

from __future__ import annotations

from bss_self_serve.config import Settings
from bss_self_serve.main import create_app
from fastapi.testclient import TestClient


def test_health_endpoint_returns_ok() -> None:
    app = create_app(Settings())
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "portal-self-serve"
