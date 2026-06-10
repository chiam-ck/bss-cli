"""v1.6 cockpit CRM screens — customers / cases / orders / catalog /
subscription routes, with mocked clients (pattern from
test_routes_case.py).

Doctrine pins at the bottom (v1.6.1, operator directive): destructive
and money-moving verbs ARE direct CRUD, but every such POST must carry
``confirm=yes`` (the expanded two-step confirm panel) — routes refuse
without it. The policy layer stays the server-side gate; only
cockpit.py talks to the orchestrator.
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
            close_case=CASE,
            open_ticket={"id": "TKT-NEW"},
            assign_ticket=TICKET,
            transition_ticket=TICKET,
            resolve_ticket=TICKET,
            cancel_ticket=TICKET,
            get_chat_transcript={},
            update_individual=CUSTOMER,
            add_contact_medium=CUSTOMER,
            update_contact_medium=CUSTOMER,
            remove_contact_medium=CUSTOMER,
            close_customer=CUSTOMER,
        )
        self.subscription = _Stub(
            list_for_customer=[SUBSCRIPTION],
            get=SUBSCRIPTION,
            get_esim_activation={
                "iccid": "8965012026000000017",
                "activationCode": "LPA:1$smdp.example$TOKEN",
            },
            schedule_plan_change=SUBSCRIPTION,
            cancel_plan_change=SUBSCRIPTION,
            renew=SUBSCRIPTION,
            purchase_vas={"id": "VP-1"},
            terminate=SUBSCRIPTION,
        )
        self.com = _Stub(
            list_orders=[ORDER],
            get_order=ORDER,
            create_order={"id": "ORD-NEW", "state": "acknowledged"},
            submit_order=ORDER,
            cancel_order=ORDER,
        )
        self.som = _Stub(
            list_for_order=[SERVICE_ORDER],
            get_service=SERVICE,
            list_services_for_subscription=[SERVICE],
        )
        self.payment = _Stub(list_methods=[PAYMENT_METHOD])
        self.catalog = _Stub(
            list_offerings=[OFFERING],
            list_active_offerings=[OFFERING],
            get_offering=OFFERING,
            get_active_price={"id": "POP-1"},
            list_vas=[{"id": "VAS_1GB", "name": "1GB booster", "currency": "SGD",
                       "priceAmount": "6", "allowanceQuantity": 1024,
                       "allowanceUnit": "mb", "expiryHours": 72}],
            list_promotions=[{"id": "PROMO-1", "displayName": "Launch deal",
                              "code": "LAUNCH10", "state": "active",
                              "discountType": "percent", "discountValue": "10",
                              "audience": "public"}],
            admin_add_offering={"id": "PLAN_XL"},
            admin_add_price={"id": "POP-2"},
            admin_set_offering_window=OFFERING,
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
    # v1.6.1 — close is direct CRUD behind the two-step confirm panel
    assert "/case/CASE-042/close" in body
    assert 'name="confirm" value="yes"' in body


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
    # v1.6.1 — terminate is direct CRUD behind the two-step confirm
    assert "/subscriptions/SUB-007/terminate" in body
    assert "LPA:1$smdp.example$TOKEN" in body


# ─── CRUD (v1.6.1, operator directive) ───────────────────────────────


def test_order_create_redirects_to_new_order(crm_client) -> None:
    r = crm_client.post(
        "/orders/create",
        data={"customer_id": "CUST-001", "offering_id": "PLAN_M"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/orders/ORD-NEW")


def test_order_create_policy_violation_flashes_on_queue(crm_client, stub) -> None:
    stub.com.create_order = _Stub(
        x=PolicyViolationFromServer(
            rule="order.create.no_payment_method",
            message="Customer CUST-001 has no payment method on file",
        )
    ).x
    r = crm_client.post(
        "/orders/create",
        data={"customer_id": "CUST-001", "offering_id": "PLAN_M"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/orders?")
    assert "err=" in r.headers["location"]


def test_catalog_add_offering(crm_client) -> None:
    r = crm_client.post(
        "/catalog/offering",
        data={"offering_id": "PLAN_XL", "name": "Mobile XL", "amount": "42",
              "data_mb": "20480"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/catalog/PLAN_XL?flash=offering_added"


def test_catalog_add_price_and_window(crm_client) -> None:
    r = crm_client.post(
        "/catalog/PLAN_M/price",
        data={"price_id": "POP-2", "amount": "24",
              "valid_from": "2026-07-01T00:00", "retire_current": "yes"},
        follow_redirects=False,
    )
    assert "flash=price_added" in r.headers["location"]
    r = crm_client.post(
        "/catalog/PLAN_M/window",
        data={"valid_from": "2026-07-01T00:00", "valid_to": ""},
        follow_redirects=False,
    )
    assert "flash=window_set" in r.headers["location"]


def test_customer_name_and_contact_crud(crm_client) -> None:
    r = crm_client.post(
        "/customers/CUST-001/name",
        data={"given_name": "Ada", "family_name": "Tan-Lim"},
        follow_redirects=False,
    )
    assert "flash=name_updated" in r.headers["location"]
    r = crm_client.post(
        "/customers/CUST-001/contact",
        data={"medium_type": "mobile", "value": "6590001111"},
        follow_redirects=False,
    )
    assert "flash=contact_added" in r.headers["location"]
    r = crm_client.post(
        "/customers/CUST-001/contact/CM-1",
        data={"value": "ada.new@example.com"},
        follow_redirects=False,
    )
    assert "flash=contact_updated" in r.headers["location"]


def test_subscription_plan_change_schedule_and_cancel(crm_client) -> None:
    r = crm_client.post(
        "/subscriptions/SUB-007/plan-change",
        data={"new_offering_id": "PLAN_L"},
        follow_redirects=False,
    )
    assert "flash=plan_change_scheduled" in r.headers["location"]
    r = crm_client.post(
        "/subscriptions/SUB-007/plan-change/cancel", follow_redirects=False
    )
    assert "flash=plan_change_cancelled" in r.headers["location"]


# ─── Doctrine pins ───────────────────────────────────────────────────

_ROUTES_DIR = Path(__file__).resolve().parents[1] / "bss_csr" / "routes"

# v1.6.1 — destructive + money-moving POSTs are direct CRUD, but every
# one requires the two-step confirm field. The expanded danger panel
# carries confirm=yes; a bare POST must bounce with an error flash and
# MUST NOT execute. (LLM path keeps propose-then-/confirm separately.)
_CONFIRM_GATED: list[tuple[str, dict[str, str]]] = [
    ("/customers/CUST-001/close", {}),
    ("/customers/CUST-001/contact/CM-1/remove", {}),
    ("/case/CASE-042/close", {"resolution_code": "no_fault_found"}),
    ("/case/CASE-042/ticket/TKT-101/cancel", {}),
    ("/orders/ORD-014/submit", {}),
    ("/orders/ORD-014/cancel", {}),
    ("/subscriptions/SUB-007/renew", {}),
    ("/subscriptions/SUB-007/vas", {"vas_offering_id": "VAS_1GB"}),
    ("/subscriptions/SUB-007/terminate", {}),
]


def test_destructive_posts_refuse_without_confirm(crm_client) -> None:
    for path, data in _CONFIRM_GATED:
        r = crm_client.post(path, data=data, follow_redirects=False)
        assert r.status_code == 303, path
        assert "err=" in r.headers["location"], path


def test_destructive_posts_execute_with_confirm(crm_client) -> None:
    for path, data in _CONFIRM_GATED:
        r = crm_client.post(
            path, data={**data, "confirm": "yes"}, follow_redirects=False
        )
        assert r.status_code == 303, path
        assert "flash=" in r.headers["location"], path


def test_crm_routes_never_touch_orchestrator_stream() -> None:
    # Complements test_no_staff_auth — the CRM screens are plain
    # reads/writes via bss-clients; only cockpit.py drives the agent.
    for py in _ROUTES_DIR.glob("*.py"):
        if py.name == "cockpit.py":
            continue
        assert "astream_once" not in py.read_text(), py.name
