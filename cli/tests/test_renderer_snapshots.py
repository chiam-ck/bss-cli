"""Renderer snapshot tests — golden-file diff is the review artifact.

Update with:
    UPDATE_SNAPSHOTS=1 uv run pytest cli/tests/test_renderer_snapshots.py
See docs/runbooks/snapshot-regeneration.md.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the conftest helpers importable without pytest's rootdir magic.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bss_cockpit.renderers.catalog import render_catalog, render_catalog_show
from bss_cockpit.renderers.customer import render_customer_360
from bss_cockpit.renderers.esim import render_esim_activation
from bss_cockpit.renderers.order import render_order
from bss_cockpit.renderers.subscription import render_subscription
from conftest import assert_snapshot  # type: ignore[import-not-found]

# ─── Catalog ─────────────────────────────────────────────────────────


_THREE_PLANS = [
    {
        "id": "PLAN_S",
        "name": "Lite",
        "productOfferingPrice": [
            {"price": {"taxIncludedAmount": {"value": 10}}}
        ],
        "bundleAllowance": [
            {"type": "data", "total": 5120, "unit": "mb"},
            {"type": "voice", "total": 100, "unit": "min"},
            {"type": "sms", "total": 100, "unit": "sms"},
            # v0.17 — PLAN_S has no roaming (0 mb); renders as "—" in
            # the comparison column so the row is still shown for grid
            # alignment but it's clear there's no inclusion.
            {"type": "data_roaming", "total": 0, "unit": "mb"},
        ],
    },
    {
        "id": "PLAN_M",
        "name": "Standard",
        "productOfferingPrice": [
            {"price": {"taxIncludedAmount": {"value": 25}}}
        ],
        "bundleAllowance": [
            {"type": "data", "total": 30720, "unit": "mb"},
            {"type": "voice", "total": -1, "unit": "min"},
            {"type": "sms", "total": -1, "unit": "sms"},
            {"type": "data_roaming", "total": 500, "unit": "mb"},
        ],
    },
    {
        "id": "PLAN_L",
        "name": "Max",
        "productOfferingPrice": [
            {"price": {"taxIncludedAmount": {"value": 45}}}
        ],
        "bundleAllowance": [
            {"type": "data", "total": -1, "unit": "mb"},
            {"type": "voice", "total": -1, "unit": "min"},
            {"type": "sms", "total": -1, "unit": "sms"},
            {"type": "data_roaming", "total": 2048, "unit": "mb"},
        ],
    },
]


def test_catalog_list_compact() -> None:
    assert_snapshot("catalog_list_compact", render_catalog(_THREE_PLANS))


def test_catalog_show_plan_m() -> None:
    plan_m = next(p for p in _THREE_PLANS if p["id"] == "PLAN_M")
    assert_snapshot("catalog_show_plan_m", render_catalog_show(plan_m))


# ─── Subscription ────────────────────────────────────────────────────


def _sub_active() -> dict:
    return {
        "id": "SUB-007",
        "customerId": "CUST-007",
        "msisdn": "90000005",
        "offeringId": "PLAN_M",
        "state": "active",
        "activatedAt": "2026-03-01T00:00:00Z",
        "nextRenewalAt": "2026-05-01T00:00:00Z",
        "balances": [
            {"type": "data", "used": 2.1, "total": 5.0, "unit": "gb"},
            {"type": "voice", "used": 12, "total": 200, "unit": "minutes"},
            {"type": "sms", "used": 0, "total": None, "unit": "count"},
        ],
    }


def _sub_blocked() -> dict:
    s = _sub_active()
    s["state"] = "blocked"
    s["balances"][0]["used"] = 5.0
    return s


# Pinned reference moment so "Renews in: N days" is deterministic across hosts.
# Snapshot was generated with this `now` against `nextRenewalAt=2026-05-01`.
_SNAPSHOT_NOW = datetime(2026, 4, 24, 0, 0, 0, tzinfo=timezone.utc)


def test_subscription_show_active() -> None:
    out = render_subscription(
        _sub_active(),
        customer={"name": "Ck Demo"},
        offering={"name": "Plan M", "price": 25},
        now=_SNAPSHOT_NOW,
    )
    assert_snapshot("subscription_show_active", out)


def test_subscription_show_blocked() -> None:
    out = render_subscription(
        _sub_blocked(),
        customer={"name": "Ck Demo"},
        offering={"name": "Plan M", "price": 25},
        now=_SNAPSHOT_NOW,
    )
    assert_snapshot("subscription_show_blocked", out)


# ─── Customer 360 ────────────────────────────────────────────────────


def _customer() -> dict:
    return {
        "id": "CUST-007",
        "name": "Ck Demo",
        "status": "active",
        "kycStatus": "verified",
        "createdAt": "2026-03-01T10:00:00Z",
        "contactMedium": [
            {"mediumType": "email", "value": "ck@example.com"},
            {"mediumType": "mobile", "value": "+6590001234"},
        ],
    }


def test_customer_show_active() -> None:
    out = render_customer_360(
        _customer(),
        subscriptions=[_sub_active()],
        cases=[],
        interactions=[
            {"createdAt": "2026-04-23T01:00:00Z", "channel": "portal-csr", "action": "viewed customer"},
        ],
    )
    assert_snapshot("customer_show_active", out)


def test_customer_show_with_blocked_sub() -> None:
    out = render_customer_360(
        _customer(),
        subscriptions=[_sub_blocked()],
    )
    assert_snapshot("customer_show_with_blocked_sub", out)


def test_customer_show_with_open_case() -> None:
    out = render_customer_360(
        _customer(),
        subscriptions=[_sub_active()],
        cases=[
            {
                "id": "CASE-042",
                "subject": "Data not working",
                "state": "open",
                "priority": "high",
            },
            {
                "id": "CASE-038",
                "subject": "Old issue",
                "state": "closed",
                "priority": "low",
            },
        ],
        tickets_by_case={
            "CASE-042": [
                {
                    "id": "TKT-101",
                    "ticketType": "subscription",
                    "priority": "high",
                    "state": "open",
                }
            ]
        },
    )
    assert_snapshot("customer_show_with_open_case", out)


# ─── Order decomposition tree ────────────────────────────────────────


def _order_completed() -> dict:
    return {
        "id": "ORD-014",
        "state": "completed",
        "customerId": "CUST-007",
        "orderDate": "2026-04-23T10:00:00Z",
        "completedDate": "2026-04-23T10:00:08Z",
        "items": [{"offeringId": "PLAN_M"}],
    }


def _order_decomposition() -> tuple[list[dict], dict, dict]:
    service_orders = [{"id": "SO-022", "state": "completed"}]
    services_by_so = {
        "SO-022": [
            {"id": "SVC-101", "name": "MobileBroadband", "serviceType": "CFS", "state": "completed"},
            {"id": "SVC-102", "name": "Data", "serviceType": "RFS", "state": "completed"},
            {"id": "SVC-103", "name": "Voice", "serviceType": "RFS", "state": "completed"},
        ]
    }
    tasks_by_service = {
        "SVC-102": [
            {
                "id": "PTK-001",
                "taskType": "hlr.activate",
                "state": "completed",
                "startedAt": "2026-04-23T10:00:00Z",
                "completedAt": "2026-04-23T10:00:01Z",
                "attemptCount": 1,
            },
            {
                "id": "PTK-002",
                "taskType": "pcrf.create_session",
                "state": "completed",
                "startedAt": "2026-04-23T10:00:01Z",
                "completedAt": "2026-04-23T10:00:02Z",
                "attemptCount": 1,
            },
        ],
        "SVC-103": [
            {
                "id": "PTK-003",
                "taskType": "hlr.subscribe",
                "state": "completed",
                "startedAt": "2026-04-23T10:00:02Z",
                "completedAt": "2026-04-23T10:00:03Z",
                "attemptCount": 1,
            },
        ],
    }
    return service_orders, services_by_so, tasks_by_service


def test_order_show_completed() -> None:
    so, svc, tasks = _order_decomposition()
    out = render_order(
        _order_completed(),
        service_orders=so,
        services_by_so=svc,
        tasks_by_service=tasks,
        subscription_id="SUB-007",
    )
    assert_snapshot("order_show_completed", out)


def test_order_show_with_stuck_task() -> None:
    so, svc, tasks = _order_decomposition()
    tasks["SVC-102"][1]["state"] = "failed"
    tasks["SVC-102"][1]["attemptCount"] = 2
    order = _order_completed()
    order["state"] = "in_progress"
    order["completedDate"] = None
    out = render_order(
        order,
        service_orders=so,
        services_by_so=svc,
        tasks_by_service=tasks,
    )
    assert_snapshot("order_show_with_stuck_task", out)


# ─── eSIM activation card ────────────────────────────────────────────


def _esim() -> dict:
    return {
        "iccid": "8910101000000000123",
        "imsi": "525010123456789",
        "msisdn": "90000005",
        "activationCode": "LPA:1$smdp.bss-cli.local$abc-123-def-456",
        "status": "prepared",
    }


def test_esim_prepared() -> None:
    assert_snapshot("esim_prepared", render_esim_activation(_esim()))


def test_esim_activated_show_full() -> None:
    e = _esim()
    e["status"] = "activated"
    assert_snapshot(
        "esim_activated_show_full",
        render_esim_activation(e, show_full=True),
    )


def test_esim_suspended() -> None:
    e = _esim()
    e["status"] = "suspended"
    assert_snapshot("esim_suspended", render_esim_activation(e))
