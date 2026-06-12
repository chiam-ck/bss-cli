"""Tests for context header propagation via bss-clients.

Verifies X-BSS-Actor, X-BSS-Channel, X-Request-ID are propagated
from the calling service's context to outgoing HTTP requests.
"""

import pytest
import respx
from bss_clients import CRMClient, set_context
from httpx import Response

CRM_URL = "http://crm:8000"


class TestHeaderPropagation:
    @pytest.mark.asyncio
    @respx.mock
    async def test_context_headers_propagated(self):
        """set_context() values appear as headers on outgoing requests."""
        set_context(actor="alice", channel="cli", request_id="req-42")

        client = CRMClient(base_url=CRM_URL)
        route = respx.get(f"{CRM_URL}/tmf-api/customerManagement/v4/customer/CUST-001").mock(
            return_value=Response(200, json={"id": "CUST-001", "status": "active"})
        )

        await client.get_customer("CUST-001")

        assert route.called
        req = route.calls[0].request
        assert req.headers["x-bss-actor"] == "alice"
        assert req.headers["x-bss-channel"] == "cli"
        assert req.headers["x-request-id"] == "req-42"

    @pytest.mark.asyncio
    @respx.mock
    async def test_default_context_values(self):
        """Without set_context(), defaults to system/system."""
        # Reset to defaults
        set_context(actor="system", channel="system", request_id="")

        client = CRMClient(base_url=CRM_URL)
        route = respx.get(f"{CRM_URL}/tmf-api/customerManagement/v4/customer/CUST-001").mock(
            return_value=Response(200, json={"id": "CUST-001", "status": "active"})
        )

        await client.get_customer("CUST-001")

        req = route.calls[0].request
        assert req.headers["x-bss-actor"] == "system"
        assert req.headers["x-bss-channel"] == "system"
        # X-Request-ID should be auto-generated when empty
        assert len(req.headers["x-request-id"]) > 0
