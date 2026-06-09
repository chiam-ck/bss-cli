"""v1.6 cockpit CRM screens — customers / cases / orders / catalog /
subscription routes, with mocked clients (pattern from
test_routes_case.py).

Doctrine pins at the bottom: destructive verbs (case.close,
ticket.cancel, order.cancel, subscription.terminate, customer.close)
must NOT have direct POST routes on the CRM screens — they hand off to
chat where propose-then-/confirm applies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from bss_clients.errors import ClientError, PolicyViolationFromServer
from bss_csr.config import Settings
from bss_csr.main import create_app
from fastapi.testclient import TestClient

# ─── Stub clients ────────────────────────────────────────────────────


class _Stub:
    """Attribute bag whose async methods return canned payloads.

    ``_Stub(foo=x)`` gives ``await stub.foo(*a, **kw) == x``; pass an
    Exception instance to make the method raise instead.
    """

    def __init__(self, **methods: Any) -> None:
        for name, result in methods.items():
            setattr(self, name, self._make(result))

    @staticmethod
    def _make(result: Any):
        async def call(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0)
            if isinstance(result, Exception):
                raise result
            return result

        return call


CUSTOMER = {
    "id": "CUST-001",
    "name": "Ada Tan",
    "status": "active",
    "kycStatus": "verified",
    "createdAt": "2026-01-10T08:00:00Z",
    "individual": {"givenName": "Ada", "familyName": "Tan"},
    "contactMedium": [
        {"id": "CM-1", "mediumType": "email", "value": "ada@example.com"},
        {"id": "CM-2", "mediumType": "mobile", "value": "6591110001"},
    ],
}

SUBSCRIPTION = {
    "id": "SUB-007",
    "customerId": "CUST-001",
    "offeringId": "PLAN_M",
    "msisdn": "6591110001",
    "iccid": "8965012026000000017",
    "state": "active",
    "activatedAt": "2026-02-01T03:00:00Z",
    "nextRenewalAt": "2026-07-01T03:00:00Z",
    "priceAmount": "22",
    "priceCurrency": "SGD",
    "balances": [
        {"allowanceType": "data", "total": 8192, "consumed": 2048,
         "remaining": 6144, "unit": "mb"},
        {"allowanceType": "voice", "total": -1, "consumed": 0,
         "remaining": -1, "unit": "min"},
    ],
}

ORDER = {
    "id": "ORD-014",
    "customerId": "CUST-001",
    "state": "completed",
    "orderDate": "2026-02-01T02:58:00Z",
    "completedDate": "2026-02-01T03:00:05Z",
    "items": [{"id": "OI-1", "offeringId": "PLAN_M", "msisdn": "6591110001"}],
}

# The Case API speaks the internal snake_case DTO — keep this fixture
# snake_case on purpose; it pins the lenient-key rendering.
CASE = {
    "id": "CASE-042",
    "customer_id": "CUST-001",
    "subject": "Data not working",
    "state": "in_progress",
    "priority": "high",
    "category": "technical",
    "opened_at": "2026-06-01T01:00:00Z",
    "ticket_ids": ["TKT-101"],
    "notes": [
        {"id": "NOTE-1", "body": "Investigating.", "author_agent_id": "AGT-001",
         "created_at": "2026-06-01T01:05:00Z"},
    ],
}

TICKET = {
    "id": "TKT-101",
    "ticketType": "technical",
    "subject": "Bundle exhausted",
    "state": "in_progress",
    "customerId": "CUST-001",
    "caseId": "CASE-042",
    "assignedToAgentId": "AGT-001",
}

OFFERING = {
    "id": "PLAN_M",
    "name": "Mobile M",
    "isBundle": True,
    "isSellable": True,
    "lifecycleStatus": "active",
    "productOfferingPrice": [
        {"id": "POP-1",
         "price": {"taxIncludedAmount": {"unit": "SGD", "value": 22}},
         "validFrom": "2026-01-01T00:00:00Z"},
    ],
    "bundleAllowance": [
        {"allowanceType": "data", "quantity": 8192, "unit": "mb"},
        {"allowanceType": "voice", "quantity": 300, "unit": "min"},
        {"allowanceType": "sms", "quantity": 100, "unit": "sms"},
    ],
}

SERVICE_ORDER = {
    "id": "SO-022",
    "commercialOrderId": "ORD-014",
    "state": "completed",
    "startedAt": "2026-02-01T02:58:10Z",
    "completedAt": "2026-02-01T03:00:00Z",
    "items": [
        {"id": "SOI-1", "action": "add", "serviceSpecId": "CFS_MBB",
         "targetServiceId": "SVC-101"},
    ],
}

SERVICE = {"id": "SVC-101", "type": "CFS", "specId": "CFS_MBB", "state": "active"}

USAGE = {
    "id": "UE-1", "eventType": "data", "quantity": 512, "unit": "mb",
    "eventTime": "2026-06-09T10:00:00Z", "roamingIndicator": False,
}

PAYMENT_METHOD = {
    "id": "PM-1", "customerId": "CUST-001", "isDefault": True, "status": "active",
    "cardSummary": {"brand": "visa", "last4": "4242", "expMonth": 12, "expYear": 2030},
}

INTERACTION = {
    "channel": "portal-csr", "direction": "inbound",
    "summary": "Called about data", "occurredAt": "2026-06-08T09:00:00Z",
}


class StubBundle:
    def __init__(self, **overrides: Any) -> None:
        self.crm = _Stub(
            list_customers=[CUSTOMER],
            find_customer_by_msisdn=CUSTOMER,
            get_customer=CUSTOMER,
            get_kyc_status={"status": "verified"},
            list_cases=[CASE],
            get_case=CASE,
            list_tickets=[TICKET],
            list_agents=[{"id": "AGT-001", "name": "Sam", "status": "active"}],
            list_interactions=[INTERACTION],
            log_interaction={"id": "INT-9"},
            open_case={"id": "CASE-NEW"},
            add_case_note={"id": "NOTE-2"},
            transition_case=CASE,
            update_case_priority=CASE,
            open_ticket={"id": "TKT-NEW"},
            assign_ticket=TICKET,
            transition_ticket=TICKET,
            resolve_ticket=TICKET,
            get_chat_transcript={},
        )
        self.subscription = _Stub(
            list_for_customer=[SUBSCRIPTION],
            get=SUBSCRIPTION,
            get_esim_activation={
                "iccid": "8965012026000000017",
                "activationCode": "LPA:1$smdp.example$TOKEN",
            },
        )
        self.com = _Stub(list_orders=[ORDER], get_order=ORDER)
        self.som = _Stub(
            list_for_order=[SERVICE_ORDER],
            get_service=SERVICE,
            list_services_for_subscription=[SERVICE],
        )
        self.payment = _Stub(list_methods=[PAYMENT_METHOD])
        self.catalog = _Stub(
            list_offerings=[OFFERING],
            get_offering=OFFERING,
            get_active_price={"id": "POP-1"},
            list_vas=[{"id": "VAS_1GB", "name": "1GB booster", "currency": "SGD",
                       "priceAmount": "6", "allowanceQuantity": 1024,
                       "allowanceUnit": "mb", "expiryHours": 72}],
            list_promotions=[{"id": "PROMO-1", "displayName": "Launch deal",
                              "code": "LAUNCH10", "state": "active",
                              "discountType": "percent", "discountValue": "10",
                              "audience": "public"}],
        )
        self.mediation = _Stub(list_usage=[USAGE])
        for name, value in overrides.items():
            setattr(self, name, value)


_ROUTE_MODULES = [
    "bss_csr.routes.customers",
    "bss_csr.routes.cases",
    "bss_csr.routes.case",
    "bss_csr.routes.orders",
    "bss_csr.routes.catalog",
    "bss_csr.routes.subscriptions",
]


@pytest.fixture
def stub() -> StubBundle:
    return StubBundle()


@pytest.fixture
def crm_client(stub: StubBundle):
    patches = [
        patch(f"{mod}.get_clients", return_value=stub) for mod in _ROUTE_MODULES
    ]
    for p in patches:
        p.start()
    try:
        app = create_app(Settings())
        with TestClient(app) as c:
            yield c
    finally:
        for p in patches:
            p.stop()


# ─── Customers ───────────────────────────────────────────────────────


def test_customers_list_renders_rows(crm_client) -> None:
    r = crm_client.get("/customers")
    assert r.status_code == 200
    assert "CUST-001" in r.text
    assert "Ada Tan" in r.text


def test_customers_list_msisdn_query(crm_client) -> None:
    r = crm_client.get("/customers?q=6591110001")
    assert r.status_code == 200
    assert "CUST-001" in r.text


def test_customer_detail_renders_all_sections(crm_client) -> None:
    r = crm_client.get("/customers/CUST-001")
    assert r.status_code == 200
    body = r.text
    assert "Ada Tan" in body
    assert "SUB-007" in body          # subscriptions
    assert "ORD-014" in body          # orders
    assert "CASE-042" in body         # cases
    assert "4242" in body             # payment method last4
    assert "Called about data" in body  # interaction
    assert "ada@example.com" in body  # contact medium


def test_customer_detail_section_degrades_not_500s(crm_client, stub) -> None:
    stub.subscription = _Stub(
        list_for_customer=ClientError(503, "subscription down"),
        get=SUBSCRIPTION,
        get_esim_activation={},
    )
    r = crm_client.get("/customers/CUST-001")
    assert r.status_code == 200
    assert "unavailable" in r.text


def test_customer_detail_404(crm_client, stub) -> None:
    stub.crm.get_customer = _Stub(x=ClientError(404, "nope")).x
    r = crm_client.get("/customers/CUST-404")
    assert r.status_code == 404


def test_log_interaction_redirects_with_flash(crm_client) -> None:
    r = crm_client.post(
        "/customers/CUST-001/interaction",
        data={"summary": "Outbound follow-up", "direction": "outbound"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash=interaction_logged" in r.headers["location"]


def test_open_case_redirects_to_case_page(crm_client) -> None:
    r = crm_client.post(
        "/customers/CUST-001/case",
        data={"subject": "Roaming question", "category": "technical",
              "priority": "normal"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/case/CASE-NEW")


def test_open_case_policy_violation_flashes_back(crm_client, stub) -> None:
    stub.crm.open_case = _Stub(
        x=PolicyViolationFromServer(
            rule="case.open.customer_must_be_active",
            message="Customer CUST-001 is not active (status=closed)",
        )
    ).x
    r = crm_client.post(
        "/customers/CUST-001/case",
        data={"subject": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    assert "/customers/CUST-001" in r.headers["location"]


# ─── Cases ───────────────────────────────────────────────────────────


def test_cases_queue_renders_snake_case_payload(crm_client) -> None:
    r = crm_client.get("/cases")
    assert r.status_code == 200
    body = r.text
    assert "CASE-042" in body
    assert "Data not working" in body
    assert "CUST-001" in body  # snake_case customer_id resolved


def test_case_page_shows_workbench_for_in_progress(crm_client) -> None:
    r = crm_client.get("/case/CASE-042")
    assert r.status_code == 200
    body = r.text
    assert "Await customer" in body
    assert "Resolve" in body
    assert "Add note" in body
    # destructive — handoff only, never a direct form action
    assert "/case/CASE-042/close" not in body


def test_case_note_post_redirects(crm_client) -> None:
    r = crm_client.post(
        "/case/CASE-042/note", data={"body": "called back"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash=note_added" in r.headers["location"]


def test_case_transition_rejects_unknown_trigger(crm_client) -> None:
    r = crm_client.post(
        "/case/CASE-042/transition", data={"trigger": "cancel"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=" in r.headers["location"]


def test_case_note_policy_violation_flashes(crm_client, stub) -> None:
    stub.crm.add_case_note = _Stub(
        x=PolicyViolationFromServer(
            rule="case.add_note.case_is_closed",
            message="Case CASE-042 is closed; cannot add notes",
        )
    ).x
    r = crm_client.post(
        "/case/CASE-042/note", data={"body": "x"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "err=" in r.headers["location"]


def test_ticket_resolve_post(crm_client) -> None:
    r = crm_client.post(
        "/case/CASE-042/ticket/TKT-101/resolve",
        data={"resolution_notes": "re-provisioned"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash=ticket_resolved" in r.headers["location"]


# ─── Orders ──────────────────────────────────────────────────────────


def test_orders_list_renders(crm_client) -> None:
    r = crm_client.get("/orders")
    assert r.status_code == 200
    assert "ORD-014" in r.text


def test_orders_jump_redirects(crm_client) -> None:
    r = crm_client.get("/orders/jump?order_id=ORD-014", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/orders/ORD-014"


def test_order_detail_renders_som_decomposition(crm_client) -> None:
    r = crm_client.get("/orders/ORD-014")
    assert r.status_code == 200
    body = r.text
    assert "SO-022" in body
    assert "SVC-101" in body
    assert "PLAN_M" in body


# ─── Catalog ─────────────────────────────────────────────────────────


def test_catalog_index_renders_plans_vas_promos(crm_client) -> None:
    r = crm_client.get("/catalog")
    assert r.status_code == 200
    body = r.text
    assert "PLAN_M" in body
    assert "8 GB" in body         # 8192 mb prettified
    assert "VAS_1GB" in body
    assert "LAUNCH10" in body


def test_offering_detail_renders_prices(crm_client) -> None:
    r = crm_client.get("/catalog/PLAN_M")
    assert r.status_code == 200
    assert "POP-1" in r.text
    assert "price snapshot" in r.text  # v0.7 doctrine note


# ─── Subscription ────────────────────────────────────────────────────


def test_subscription_detail_renders(crm_client) -> None:
    r = crm_client.get("/subscriptions/SUB-007")
    assert r.status_code == 200
    body = r.text
    assert "SUB-007" in body
    assert "unlimited" in body          # voice balance
    assert "6591110001" in body
    assert "Terminate" in body          # handoff button, not a form POST
    assert "/subscriptions/SUB-007/terminate" not in body
    assert "LPA:1$smdp.example$TOKEN" in body


# ─── Doctrine pins ───────────────────────────────────────────────────

_ROUTES_DIR = Path(__file__).resolve().parents[1] / "bss_csr" / "routes"

# Destructive verbs (orchestrator DESTRUCTIVE_TOOLS) must never be a
# direct CRM-screen write — chat handoff is the single chokepoint.
_FORBIDDEN_CLIENT_CALLS = [
    ".close_case(",
    ".cancel_ticket(",
    ".cancel_order(",
    ".terminate(",
    ".close_customer(",
    ".remove_method(",
    ".remove_contact_medium(",
]


def test_crm_routes_never_call_destructive_clients() -> None:
    offenders: list[str] = []
    for py in _ROUTES_DIR.glob("*.py"):
        src = py.read_text()
        for needle in _FORBIDDEN_CLIENT_CALLS:
            if needle in src:
                offenders.append(f"{py.name}: {needle}")
    assert not offenders, (
        "Destructive verbs must hand off to chat (propose-then-/confirm), "
        f"not run from CRM screens: {offenders}"
    )


def test_crm_routes_never_touch_orchestrator_stream() -> None:
    # Complements test_no_staff_auth — the CRM screens are plain
    # reads/writes via bss-clients; only cockpit.py drives the agent.
    for py in _ROUTES_DIR.glob("*.py"):
        if py.name == "cockpit.py":
            continue
        assert "astream_once" not in py.read_text(), py.name
