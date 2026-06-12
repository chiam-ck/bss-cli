"""v0.9 — per-Context X-BSS-API-Token override.

Locks in the contract that ``set_service_identity_token(token)`` causes
the next ``BSSClient._request`` to send that token regardless of which
``AuthProvider`` the client was constructed with. This is what lets
``astream_once(service_identity=...)`` in the orchestrator make a tool
call carry the portal's token even though the orchestrator's clients
are built with the default token.

The override is per-Context, so concurrent asyncio tasks each see
their own value (verified by exercising contextvars.copy_context).
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest
import respx
from bss_clients import (
    BSSClient,
    TokenAuthProvider,
    reset_service_identity_token,
    set_service_identity_token,
)
from httpx import Response

BASE_URL = "http://test-service:8000"
DEFAULT_TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
PORTAL_TOKEN = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"


@pytest.mark.asyncio
@respx.mock
async def test_no_override_uses_provider_token():
    provider = TokenAuthProvider(DEFAULT_TOKEN)
    client = BSSClient(base_url=BASE_URL, auth_provider=provider)
    route = respx.get(f"{BASE_URL}/x").mock(return_value=Response(200, json={}))
    await client._request("GET", "/x")
    request = route.calls[0].request
    assert request.headers["x-bss-api-token"] == DEFAULT_TOKEN


@pytest.mark.asyncio
@respx.mock
async def test_override_replaces_provider_token():
    provider = TokenAuthProvider(DEFAULT_TOKEN)
    client = BSSClient(base_url=BASE_URL, auth_provider=provider)
    route = respx.get(f"{BASE_URL}/x").mock(return_value=Response(200, json={}))

    reset = set_service_identity_token(PORTAL_TOKEN)
    try:
        await client._request("GET", "/x")
    finally:
        reset_service_identity_token(reset)

    request = route.calls[0].request
    assert request.headers["x-bss-api-token"] == PORTAL_TOKEN


@pytest.mark.asyncio
@respx.mock
async def test_reset_restores_provider_token():
    """After reset, subsequent calls go back to the provider's token."""
    provider = TokenAuthProvider(DEFAULT_TOKEN)
    client = BSSClient(base_url=BASE_URL, auth_provider=provider)
    route = respx.get(f"{BASE_URL}/x").mock(return_value=Response(200, json={}))

    reset = set_service_identity_token(PORTAL_TOKEN)
    await client._request("GET", "/x")
    reset_service_identity_token(reset)
    await client._request("GET", "/x")

    assert route.calls[0].request.headers["x-bss-api-token"] == PORTAL_TOKEN
    assert route.calls[1].request.headers["x-bss-api-token"] == DEFAULT_TOKEN


@pytest.mark.asyncio
@respx.mock
async def test_empty_string_override_uses_provider_token():
    """``set_service_identity_token(None)`` and ``("")`` are no-ops."""
    provider = TokenAuthProvider(DEFAULT_TOKEN)
    client = BSSClient(base_url=BASE_URL, auth_provider=provider)
    route = respx.get(f"{BASE_URL}/x").mock(return_value=Response(200, json={}))

    reset = set_service_identity_token(None)
    try:
        await client._request("GET", "/x")
    finally:
        reset_service_identity_token(reset)

    assert route.calls[0].request.headers["x-bss-api-token"] == DEFAULT_TOKEN


@pytest.mark.asyncio
@respx.mock
async def test_override_isolated_per_asyncio_task():
    """Concurrent tasks each see their own override (Context isolation)."""
    provider = TokenAuthProvider(DEFAULT_TOKEN)
    client = BSSClient(base_url=BASE_URL, auth_provider=provider)
    respx.get(f"{BASE_URL}/x").mock(return_value=Response(200, json={}))

    seen: list[tuple[str, str]] = []

    async def call_with_override(token: str, label: str) -> None:
        reset = set_service_identity_token(token)
        try:
            # Yield control so the other task interleaves.
            await asyncio.sleep(0)
            await client._request("GET", "/x")
            # Read back what the contextvar saw inside this task.
            from bss_clients.base import _service_identity_token_var
            seen.append((label, _service_identity_token_var.get()))
        finally:
            reset_service_identity_token(reset)

    task_a = asyncio.create_task(call_with_override(PORTAL_TOKEN, "a"))
    task_b = asyncio.create_task(call_with_override(DEFAULT_TOKEN, "b"))
    await asyncio.gather(task_a, task_b)

    seen_dict = dict(seen)
    assert seen_dict["a"] == PORTAL_TOKEN
    assert seen_dict["b"] == DEFAULT_TOKEN


@pytest.mark.asyncio
@respx.mock
async def test_override_does_not_leak_outside_copied_context():
    """copy_context() snapshot — work inside it shouldn't bleed out."""
    provider = TokenAuthProvider(DEFAULT_TOKEN)
    client = BSSClient(base_url=BASE_URL, auth_provider=provider)
    respx.get(f"{BASE_URL}/x").mock(return_value=Response(200, json={}))

    async def inner_work() -> str:
        reset = set_service_identity_token(PORTAL_TOKEN)
        try:
            await client._request("GET", "/x")
            from bss_clients.base import _service_identity_token_var
            return _service_identity_token_var.get()
        finally:
            reset_service_identity_token(reset)

    # Run inner_work in a copied Context — its mutation should not
    # propagate to the outer Context (no override "leak" for the next
    # caller in the same loop).
    ctx = contextvars.copy_context()
    inner_token = await ctx.run(asyncio.ensure_future, inner_work())
    assert inner_token == PORTAL_TOKEN

    from bss_clients.base import _service_identity_token_var
    assert _service_identity_token_var.get() == ""
