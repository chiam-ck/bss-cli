"""Shared fixtures for Phase 8 integration tests.

These tests exercise the full usage-flow stack: Mediation (8007) → Rating
(8008) → Subscription (8006), with supporting services CRM (8002), Catalog
(8001), Payment (8003). They require all services to be running and
reachable on localhost, and RabbitMQ + Postgres wired up behind them.

Skipped automatically if any required port isn't open.
"""

from __future__ import annotations

import asyncio
import socket
import uuid

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration

# Service ports (from docker-compose.yml host-side mappings)
CRM = "http://localhost:8002"
PAYMENT = "http://localhost:8003"
SUBSCRIPTION = "http://localhost:8006"
MEDIATION = "http://localhost:8007"

REQUIRED_PORTS = [
    ("crm", 8002),
    ("catalog", 8001),
    ("payment", 8003),
    ("subscription", 8006),
    ("mediation", 8007),
    ("rating", 8008),
]


def _reachable(host: str, port: int) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _require_stack_up():
    missing = [name for name, port in REQUIRED_PORTS if not _reachable("localhost", port)]
    if missing:
        pytest.skip(f"Integration stack not up — missing: {', '.join(missing)}")


async def _create_customer(http: httpx.AsyncClient) -> str:
    resp = await http.post(
        f"{CRM}/tmf-api/customerManagement/v4/customer",
        json={
            "givenName": "Integ",
            "familyName": f"T{uuid.uuid4().hex[:6]}",
            "contactMedium": [
                {
                    "mediumType": "email",
                    "value": f"integ-phase08-{uuid.uuid4().hex[:8]}@test.com",
                    "isPrimary": True,
                }
            ],
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def _register_payment_method(http: httpx.AsyncClient, customer_id: str) -> str:
    resp = await http.post(
        f"{PAYMENT}/tmf-api/paymentMethodManagement/v4/paymentMethod",
        json={
            "customerId": customer_id,
            "type": "card",
            "tokenizationProvider": "mock",
            "providerToken": f"tok_integ_{uuid.uuid4().hex[:10]}",
            "cardSummary": {
                "brand": "visa",
                "last4": "4242",
                "expMonth": 12,
                "expYear": 2030,
            },
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def _reserve_msisdn(http: httpx.AsyncClient) -> str:
    resp = await http.post(f"{CRM}/inventory-api/v1/msisdn/reserve-next", json={})
    resp.raise_for_status()
    return resp.json()["msisdn"]


async def _reserve_esim(http: httpx.AsyncClient) -> str:
    resp = await http.post(f"{CRM}/inventory-api/v1/esim/reserve")
    resp.raise_for_status()
    return resp.json()["iccid"]


async def _create_subscription(
    http: httpx.AsyncClient,
    *,
    customer_id: str,
    msisdn: str,
    iccid: str,
    payment_method_id: str,
    offering_id: str = "PLAN_M",
) -> dict:
    resp = await http.post(
        f"{SUBSCRIPTION}/subscription-api/v1/subscription",
        json={
            "customerId": customer_id,
            "offeringId": offering_id,
            "msisdn": msisdn,
            "iccid": iccid,
            "paymentMethodId": payment_method_id,
        },
    )
    resp.raise_for_status()
    return resp.json()


@pytest_asyncio.fixture
async def active_subscription():
    """Bootstrap an active PLAN_M subscription with full inventory + payment chain.

    Returns dict with: id, customer_id, msisdn, iccid, payment_method_id.
    """
    async with httpx.AsyncClient(timeout=15.0) as http:
        customer_id = await _create_customer(http)
        payment_method_id = await _register_payment_method(http, customer_id)
        msisdn = await _reserve_msisdn(http)
        iccid = await _reserve_esim(http)
        sub = await _create_subscription(
            http,
            customer_id=customer_id,
            msisdn=msisdn,
            iccid=iccid,
            payment_method_id=payment_method_id,
        )

    assert sub["state"] == "active", f"Expected active subscription, got {sub['state']}"
    return {
        "id": sub["id"],
        "customer_id": customer_id,
        "msisdn": msisdn,
        "iccid": iccid,
        "payment_method_id": payment_method_id,
    }


async def poll_until(predicate, *, timeout_s: float = 5.0, interval_s: float = 0.1):
    """Poll an async predicate until it returns truthy or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = None
    while asyncio.get_event_loop().time() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(interval_s)
    return last


async def get_balance(http: httpx.AsyncClient, sub_id: str, allowance_type: str) -> dict | None:
    resp = await http.get(f"{SUBSCRIPTION}/subscription-api/v1/subscription/{sub_id}/balance")
    if resp.status_code != 200:
        return None
    for b in resp.json():
        if b["allowanceType"] == allowance_type:
            return b
    return None


async def get_subscription(http: httpx.AsyncClient, sub_id: str) -> dict | None:
    resp = await http.get(f"{SUBSCRIPTION}/subscription-api/v1/subscription/{sub_id}")
    if resp.status_code != 200:
        return None
    return resp.json()
