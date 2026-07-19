"""Tests for BSSClient base: typed errors, timeouts, no auto-retry."""

import pytest
import respx
from bss_clients import BSSClient, ClientError, NotFound, PolicyViolationFromServer, ServerError, Timeout
from httpx import Response

BASE_URL = "http://test-service:8000"


@pytest.fixture
def client():
    return BSSClient(base_url=BASE_URL)


class TestTypedErrors:
    @pytest.mark.asyncio
    @respx.mock
    async def test_404_raises_not_found(self, client):
        respx.get(f"{BASE_URL}/thing/123").mock(
            return_value=Response(404, text="Not found")
        )
        with pytest.raises(NotFound) as exc_info:
            await client._request("GET", "/thing/123")
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    @respx.mock
    async def test_422_policy_violation(self, client):
        body = {
            "code": "POLICY_VIOLATION",
            "reason": "test.rule",
            "message": "Test violation",
            "context": {"key": "value"},
        }
        respx.post(f"{BASE_URL}/thing").mock(
            return_value=Response(422, json=body)
        )
        with pytest.raises(PolicyViolationFromServer) as exc_info:
            await client._request("POST", "/thing")
        assert exc_info.value.rule == "test.rule"
        assert exc_info.value.context == {"key": "value"}
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    @respx.mock
    async def test_422_non_policy_raises_client_error(self, client):
        respx.post(f"{BASE_URL}/thing").mock(
            return_value=Response(422, json={"detail": "validation error"})
        )
        with pytest.raises(ClientError) as exc_info:
            await client._request("POST", "/thing")
        assert exc_info.value.status_code == 422
        assert not isinstance(exc_info.value, PolicyViolationFromServer)

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_raises_server_error(self, client):
        respx.get(f"{BASE_URL}/thing").mock(
            return_value=Response(500, text="Internal server error")
        )
        with pytest.raises(ServerError) as exc_info:
            await client._request("GET", "/thing")
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    @respx.mock
    async def test_503_raises_server_error(self, client):
        respx.get(f"{BASE_URL}/thing").mock(
            return_value=Response(503, text="Service unavailable")
        )
        with pytest.raises(ServerError) as exc_info:
            await client._request("GET", "/thing")
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    @respx.mock
    async def test_200_returns_response(self, client):
        respx.get(f"{BASE_URL}/thing").mock(
            return_value=Response(200, json={"ok": True})
        )
        resp = await client._request("GET", "/thing")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


class TestTimeout:
    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_raises(self):
        import httpx

        client = BSSClient(base_url=BASE_URL, timeout=0.01)
        respx.get(f"{BASE_URL}/slow").mock(side_effect=httpx.ReadTimeout("timeout"))
        with pytest.raises(Timeout):
            await client._request("GET", "/slow")


class TestNoAutoRetry:
    @pytest.mark.asyncio
    @respx.mock
    async def test_503_not_retried(self, client):
        """A 503 is raised once — no automatic retries."""
        route = respx.get(f"{BASE_URL}/flaky").mock(
            return_value=Response(503, text="down")
        )
        with pytest.raises(ServerError):
            await client._request("GET", "/flaky")
        assert route.call_count == 1
