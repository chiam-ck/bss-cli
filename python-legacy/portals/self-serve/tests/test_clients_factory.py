"""v0.9 — bss_self_serve.clients.get_clients factory.

Locks in:
- The factory uses ``NamedTokenAuthProvider("portal_self_serve",
  "BSS_PORTAL_SELF_SERVE_API_TOKEN", fallback_env_var="BSS_API_TOKEN")``.
- It returns a cached PortalClients bundle.
- The auth provider on every constructed client is the same named
  provider instance (so rotation propagates uniformly).
- Outbound headers carry the named token's value when set; fall back
  to the default when not.
"""

from __future__ import annotations

import pytest
from bss_clients import NamedTokenAuthProvider
from bss_self_serve import clients as portal_clients

PORTAL_TOKEN = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
DEFAULT_TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _clear_clients_cache():
    """Each test gets a fresh factory state (the lru_cache is process-wide)."""
    portal_clients.get_clients.cache_clear()
    yield
    portal_clients.get_clients.cache_clear()


def test_factory_uses_named_token_provider(monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
    bundle = portal_clients.get_clients()
    # The auth provider on each client is a NamedTokenAuthProvider
    # carrying the portal_self_serve identity. We poke at the private
    # ``_auth`` attribute on BSSClient — the contract test is that the
    # factory wires the right provider, not what the provider's public
    # API looks like.
    for c in (
        bundle.catalog,
        bundle.crm,
        bundle.inventory,
        bundle.com,
        bundle.subscription,
        bundle.payment,
        bundle.provisioning,
    ):
        provider = c._auth
        assert isinstance(provider, NamedTokenAuthProvider)
        assert provider.identity == "portal_self_serve"


def test_factory_caches_bundle(monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
    a = portal_clients.get_clients()
    b = portal_clients.get_clients()
    assert a is b


def test_factory_uses_named_env_when_set(monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
    monkeypatch.setenv("BSS_API_TOKEN", DEFAULT_TOKEN)
    bundle = portal_clients.get_clients()
    provider = bundle.catalog._auth
    assert provider.source_env == "BSS_PORTAL_SELF_SERVE_API_TOKEN"


def test_factory_falls_back_to_default_token(monkeypatch):
    """When BSS_PORTAL_SELF_SERVE_API_TOKEN is unset, fall back to BSS_API_TOKEN.

    Receiving service will resolve service_identity=default for these
    calls, but the portal still functions during a staged rollout.
    """
    monkeypatch.delenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", raising=False)
    monkeypatch.setenv("BSS_API_TOKEN", DEFAULT_TOKEN)
    bundle = portal_clients.get_clients()
    provider = bundle.catalog._auth
    assert provider.source_env == "BSS_API_TOKEN"
    assert provider.identity == "portal_self_serve"  # informational label


def test_factory_raises_when_neither_env_set(monkeypatch):
    monkeypatch.delenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", raising=False)
    monkeypatch.delenv("BSS_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        portal_clients.get_clients()


@pytest.mark.asyncio
async def test_factory_outbound_headers_carry_named_token(monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
    bundle = portal_clients.get_clients()
    provider = bundle.catalog._auth
    headers = await provider.get_headers()
    assert headers == {"X-BSS-API-Token": PORTAL_TOKEN}
    # No service-identity header sent — that would be forgeable.
    assert "X-BSS-Service-Identity" not in headers


def test_portal_clients_bundle_includes_required_clients(monkeypatch):
    """Document the read + write surface the portal currently uses.

    v0.10 adds payment + provisioning so post-login routes can call
    them directly (charge history, COF management, eSIM ownership
    lookup). The catalog covers VAS list. The subscription client
    covers terminate / purchase_vas / schedule_plan_change /
    cancel_plan_change.
    """
    monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
    bundle = portal_clients.get_clients()
    assert hasattr(bundle, "catalog")
    assert hasattr(bundle, "crm")
    assert hasattr(bundle, "inventory")
    assert hasattr(bundle, "com")
    assert hasattr(bundle, "subscription")
    assert hasattr(bundle, "payment")
    assert hasattr(bundle, "provisioning")
