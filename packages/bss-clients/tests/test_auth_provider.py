"""Tests for AuthProvider protocol, NoAuthProvider, TokenAuthProvider."""

import pytest
import respx
from bss_clients import (
    AuthProvider,
    BSSClient,
    NamedTokenAuthProvider,
    NoAuthProvider,
    TokenAuthProvider,
)
from httpx import Response

BASE_URL = "http://test-service:8000"


class TestNoAuthProvider:
    @pytest.mark.asyncio
    async def test_returns_empty_headers(self):
        provider = NoAuthProvider()
        headers = await provider.get_headers()
        assert headers == {}

    def test_implements_protocol(self):
        assert isinstance(NoAuthProvider(), AuthProvider)


class TestCustomAuthProvider:
    @pytest.mark.asyncio
    @respx.mock
    async def test_custom_auth_headers_injected(self):
        """Custom AuthProvider headers appear on outgoing requests."""

        class BearerAuth:
            async def get_headers(self) -> dict[str, str]:
                return {"Authorization": "Bearer tok_test_123"}

        client = BSSClient(base_url=BASE_URL, auth_provider=BearerAuth())
        route = respx.get(f"{BASE_URL}/protected").mock(
            return_value=Response(200, json={"ok": True})
        )

        await client._request("GET", "/protected")

        assert route.called
        request = route.calls[0].request
        assert request.headers["authorization"] == "Bearer tok_test_123"

    @pytest.mark.asyncio
    @respx.mock
    async def test_auth_called_on_every_request(self):
        """AuthProvider.get_headers() is called per-request, not cached."""
        call_count = 0

        class CountingAuth:
            async def get_headers(self) -> dict[str, str]:
                nonlocal call_count
                call_count += 1
                return {"Authorization": f"Bearer tok_{call_count}"}

        client = BSSClient(base_url=BASE_URL, auth_provider=CountingAuth())
        respx.get(f"{BASE_URL}/thing").mock(
            return_value=Response(200, json={})
        )

        await client._request("GET", "/thing")
        await client._request("GET", "/thing")

        assert call_count == 2


class TestTokenAuthProvider:
    @pytest.mark.asyncio
    async def test_returns_x_bss_api_token_header(self):
        provider = TokenAuthProvider("test-token-32-chars-aaaaaaaaaaaaaa")
        headers = await provider.get_headers()
        assert headers == {"X-BSS-API-Token": "test-token-32-chars-aaaaaaaaaaaaaa"}

    def test_implements_protocol(self):
        assert isinstance(
            TokenAuthProvider("test-token-32-chars-aaaaaaaaaaaaaa"),
            AuthProvider,
        )

    def test_empty_token_rejected_at_construction(self):
        with pytest.raises(ValueError, match="non-empty"):
            TokenAuthProvider("")

    @pytest.mark.asyncio
    async def test_returns_a_copy_each_call(self):
        """Mutation of one returned dict must not poison subsequent calls."""
        provider = TokenAuthProvider("test-token-32-chars-aaaaaaaaaaaaaa")
        first = await provider.get_headers()
        first["X-BSS-API-Token"] = "tampered"
        second = await provider.get_headers()
        assert second["X-BSS-API-Token"] == "test-token-32-chars-aaaaaaaaaaaaaa"

    @pytest.mark.asyncio
    @respx.mock
    async def test_token_injected_on_outgoing_request(self):
        provider = TokenAuthProvider("test-token-32-chars-aaaaaaaaaaaaaa")
        client = BSSClient(base_url=BASE_URL, auth_provider=provider)
        route = respx.get(f"{BASE_URL}/protected").mock(
            return_value=Response(200, json={"ok": True})
        )

        await client._request("GET", "/protected")

        assert route.called
        request = route.calls[0].request
        assert request.headers["x-bss-api-token"] == "test-token-32-chars-aaaaaaaaaaaaaa"


# ─────────────────────────────────────────────────────────────────────────────
# v0.9 — NamedTokenAuthProvider
# ─────────────────────────────────────────────────────────────────────────────


PORTAL_TOKEN = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
DEFAULT_TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class TestNamedTokenAuthProvider:
    def test_loads_from_named_env_var(self, monkeypatch):
        monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
        provider = NamedTokenAuthProvider("portal_self_serve", "BSS_PORTAL_SELF_SERVE_API_TOKEN")
        assert provider.identity == "portal_self_serve"
        assert provider.source_env == "BSS_PORTAL_SELF_SERVE_API_TOKEN"

    @pytest.mark.asyncio
    async def test_headers_carry_named_token(self, monkeypatch):
        monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
        provider = NamedTokenAuthProvider("portal_self_serve", "BSS_PORTAL_SELF_SERVE_API_TOKEN")
        headers = await provider.get_headers()
        assert headers == {"X-BSS-API-Token": PORTAL_TOKEN}

    def test_implements_protocol(self, monkeypatch):
        monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
        provider = NamedTokenAuthProvider("portal_self_serve", "BSS_PORTAL_SELF_SERVE_API_TOKEN")
        assert isinstance(provider, AuthProvider)

    def test_raises_when_named_env_unset_and_no_fallback(self, monkeypatch):
        monkeypatch.delenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="BSS_PORTAL_SELF_SERVE_API_TOKEN is unset"):
            NamedTokenAuthProvider("portal_self_serve", "BSS_PORTAL_SELF_SERVE_API_TOKEN")

    def test_raises_when_both_envs_unset(self, monkeypatch):
        monkeypatch.delenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", raising=False)
        monkeypatch.delenv("BSS_API_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="fallback BSS_API_TOKEN is also unset"):
            NamedTokenAuthProvider(
                "portal_self_serve",
                "BSS_PORTAL_SELF_SERVE_API_TOKEN",
                fallback_env_var="BSS_API_TOKEN",
            )

    def test_fallback_to_default_token_when_named_unset(self, monkeypatch):
        """v0.9 backwards compat — if named token not yet provisioned,
        fall back to the default. Receiving service sees identity=default."""
        monkeypatch.delenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", raising=False)
        monkeypatch.setenv("BSS_API_TOKEN", DEFAULT_TOKEN)
        provider = NamedTokenAuthProvider(
            "portal_self_serve",
            "BSS_PORTAL_SELF_SERVE_API_TOKEN",
            fallback_env_var="BSS_API_TOKEN",
        )
        assert provider.source_env == "BSS_API_TOKEN"
        assert provider.identity == "portal_self_serve"  # informational label

    @pytest.mark.asyncio
    async def test_fallback_carries_default_token_value(self, monkeypatch):
        monkeypatch.delenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", raising=False)
        monkeypatch.setenv("BSS_API_TOKEN", DEFAULT_TOKEN)
        provider = NamedTokenAuthProvider(
            "portal_self_serve",
            "BSS_PORTAL_SELF_SERVE_API_TOKEN",
            fallback_env_var="BSS_API_TOKEN",
        )
        headers = await provider.get_headers()
        assert headers == {"X-BSS-API-Token": DEFAULT_TOKEN}

    def test_named_takes_precedence_over_fallback_when_both_set(self, monkeypatch):
        monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
        monkeypatch.setenv("BSS_API_TOKEN", DEFAULT_TOKEN)
        provider = NamedTokenAuthProvider(
            "portal_self_serve",
            "BSS_PORTAL_SELF_SERVE_API_TOKEN",
            fallback_env_var="BSS_API_TOKEN",
        )
        assert provider.source_env == "BSS_PORTAL_SELF_SERVE_API_TOKEN"

    def test_token_loaded_once_at_construction(self, monkeypatch):
        """Per doctrine: tokens read at startup, cached. No request-time
        os.environ reads. The header dict is built once and reused."""
        monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
        provider = NamedTokenAuthProvider("portal_self_serve", "BSS_PORTAL_SELF_SERVE_API_TOKEN")
        # Even if env changes after construction, the provider keeps its
        # original cached token. Rotation is restart-based.
        monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", "new-rotated-value-32-chars-zzzzzz")
        # Re-await get_headers — must still return the original token.
        import asyncio
        headers = asyncio.run(provider.get_headers())
        assert headers["X-BSS-API-Token"] == PORTAL_TOKEN

    def test_empty_identity_rejected(self):
        with pytest.raises(ValueError, match="non-empty identity"):
            NamedTokenAuthProvider("", "BSS_PORTAL_SELF_SERVE_API_TOKEN")

    def test_empty_env_var_rejected(self):
        with pytest.raises(ValueError, match="env_var"):
            NamedTokenAuthProvider("portal", "")

    @pytest.mark.asyncio
    async def test_returns_a_copy_each_call(self, monkeypatch):
        """Mutation of one returned dict must not poison later calls."""
        monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
        provider = NamedTokenAuthProvider("portal_self_serve", "BSS_PORTAL_SELF_SERVE_API_TOKEN")
        first = await provider.get_headers()
        first["X-BSS-API-Token"] = "tampered"
        second = await provider.get_headers()
        assert second["X-BSS-API-Token"] == PORTAL_TOKEN

    @pytest.mark.asyncio
    @respx.mock
    async def test_named_token_injected_on_outgoing_request(self, monkeypatch):
        monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)
        provider = NamedTokenAuthProvider("portal_self_serve", "BSS_PORTAL_SELF_SERVE_API_TOKEN")
        client = BSSClient(base_url=BASE_URL, auth_provider=provider)
        route = respx.get(f"{BASE_URL}/protected").mock(
            return_value=Response(200, json={"ok": True})
        )

        await client._request("GET", "/protected")

        assert route.called
        request = route.calls[0].request
        # The OUTBOUND header carries the raw named token. The receiving
        # service hashes + lookups it against its TokenMap to resolve
        # service_identity.
        assert request.headers["x-bss-api-token"] == PORTAL_TOKEN
        # No separate X-BSS-Service-Identity header is sent (would be
        # forgeable; doctrine forbids it).
        assert "x-bss-service-identity" not in {h.lower() for h in request.headers}
