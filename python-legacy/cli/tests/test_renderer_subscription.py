"""Unit test for the subscription hero renderer."""

from __future__ import annotations

from bss_cockpit.renderers.subscription import render_subscription


def _sub() -> dict:
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


def test_render_subscription_includes_core_fields() -> None:
    out = render_subscription(_sub(), customer={"name": "Ck"}, offering={"name": "Plan M", "price": 25})
    assert "SUB-007" in out
    assert "Ck (CUST-007)" in out
    assert "9000 0005" in out
    assert "Plan M (PLAN_M) — SGD 25/mo" in out
    assert "● ACTIVE" in out
    # bundle rows
    assert "Data" in out
    assert "Voice" in out
    assert "Sms" in out
    # unlimited row renders the dash-filled bar
    assert "unlimited" in out


def test_render_subscription_missing_balances_shows_placeholder() -> None:
    sub = _sub()
    sub["balances"] = []
    out = render_subscription(sub)
    assert "(no bundle balances)" in out


def test_render_subscription_frame_is_rectangular() -> None:
    out = render_subscription(_sub())
    lines = out.splitlines()
    widths = {len(line) for line in lines}
    assert len(widths) == 1, f"frame widths inconsistent: {widths}"
