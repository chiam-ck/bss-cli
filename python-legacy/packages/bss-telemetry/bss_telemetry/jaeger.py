"""Minimal Jaeger HTTP API client for `bss trace`.

Reads ``BSS_JAEGER_UI_URL`` from env (default ``http://tech-vm:16686``).
The CLI talks to Jaeger's UI port for the JSON API — same host that
serves the web UI also exposes ``/api/services`` and ``/api/traces``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


def _ui_url() -> str:
    return os.environ.get("BSS_JAEGER_UI_URL", "http://tech-vm:16686").rstrip("/")


class JaegerError(Exception):
    """Raised when Jaeger returns a non-2xx or unparseable response."""


class JaegerClient:
    """Async wrapper for the few Jaeger v1 HTTP endpoints `bss trace` needs."""

    def __init__(self, base_url: str | None = None, timeout: float = 5.0) -> None:
        self.base_url = (base_url or _ui_url()).rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "JaegerClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def list_services(self) -> list[str]:
        resp = await self._client.get(f"{self.base_url}/api/services")
        if resp.status_code != 200:
            raise JaegerError(f"GET /api/services -> {resp.status_code}")
        body = resp.json()
        return list(body.get("data", []))

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        """Fetch a single trace by ID. Returns the raw Jaeger v1 trace shape."""
        resp = await self._client.get(f"{self.base_url}/api/traces/{trace_id}")
        if resp.status_code == 404:
            raise JaegerError(f"trace {trace_id} not found in Jaeger")
        if resp.status_code != 200:
            raise JaegerError(f"GET /api/traces/{trace_id} -> {resp.status_code}")
        body = resp.json()
        traces = body.get("data", [])
        if not traces:
            raise JaegerError(f"trace {trace_id} returned empty data")
        return traces[0]

    async def find_traces(
        self,
        *,
        service: str,
        operation: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find recent traces by service (+ optional operation)."""
        params: dict[str, str | int] = {"service": service, "limit": limit}
        if operation:
            params["operation"] = operation
        resp = await self._client.get(
            f"{self.base_url}/api/traces", params=params
        )
        if resp.status_code != 200:
            raise JaegerError(f"GET /api/traces -> {resp.status_code}")
        return list(resp.json().get("data", []))

    async def latest_ask_trace_id(self) -> str | None:
        """Return the trace_id of the most recent ``bss.ask`` invocation, if any."""
        traces = await self.find_traces(
            service="bss-orchestrator", operation="bss.ask", limit=1
        )
        if not traces:
            return None
        return traces[0].get("traceID")
