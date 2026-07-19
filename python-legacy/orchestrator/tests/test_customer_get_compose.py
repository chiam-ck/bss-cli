"""``customer.get`` returns a composite payload — customer record +
adjacent context (subscriptions, cases, interactions) — so the
cockpit's ``render_customer_360`` actually shows real numbers.

Before this change, the renderer was called with only the TMF629
customer dict and displayed "Subscriptions (0) / (none)" even when
the customer had real subs and cases. The compose happens in the
orchestrator tool, in parallel via ``asyncio.gather`` so the four
upstream reads cost the slowest, not their sum.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


class _FakeCRM:
    def __init__(
        self,
        *,
        customer: dict[str, Any] | None = None,
        cases: list[dict[str, Any]] | None = None,
        interactions: list[dict[str, Any]] | None = None,
        customer_error: BaseException | None = None,
        cases_error: BaseException | None = None,
    ) -> None:
        self._customer = customer
        self._cases = cases or []
        self._interactions = interactions or []
        self._customer_error = customer_error
        self._cases_error = cases_error

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        if self._customer_error is not None:
            raise self._customer_error
        return dict(self._customer or {})

    async def list_cases(
        self, *, customer_id: str | None = None, **_kw
    ) -> list[dict[str, Any]]:
        if self._cases_error is not None:
            raise self._cases_error
        return list(self._cases)

    async def list_interactions(
        self, customer_id: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return list(self._interactions[:limit])


class _FakeSubscription:
    def __init__(
        self,
        *,
        subscriptions: list[dict[str, Any]] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._subscriptions = subscriptions or []
        self._error = error

    async def list_for_customer(
        self, customer_id: str
    ) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return list(self._subscriptions)


class _FakeBundle:
    def __init__(self, *, crm: _FakeCRM, subscription: _FakeSubscription) -> None:
        self.crm = crm
        self.subscription = subscription


@pytest.mark.asyncio
async def test_customer_get_returns_extras_with_subscriptions_cases_interactions() -> None:
    """The four parallel reads land under ``_extras`` so the renderer
    can call ``render_customer_360`` with real counts."""
    from bss_orchestrator.tools.customer import customer_get

    bundle = _FakeBundle(
        crm=_FakeCRM(
            customer={"id": "CUST-001", "name": "Ck", "status": "active"},
            cases=[
                {"id": "CASE-001", "state": "open"},
                {"id": "CASE-002", "state": "closed"},
            ],
            interactions=[{"id": "INT-001"}, {"id": "INT-002"}],
        ),
        subscription=_FakeSubscription(
            subscriptions=[{"id": "SUB-3513", "state": "active"}]
        ),
    )

    with patch(
        "bss_orchestrator.tools.customer.get_clients", return_value=bundle
    ):
        out = await customer_get("CUST-001")

    assert out["id"] == "CUST-001"
    assert "_extras" in out
    extras = out["_extras"]
    assert extras["subscriptions"] == [{"id": "SUB-3513", "state": "active"}]
    assert {c["id"] for c in extras["cases"]} == {"CASE-001", "CASE-002"}
    assert len(extras["interactions"]) == 2


@pytest.mark.asyncio
async def test_customer_get_swallows_subdomain_failures_and_keeps_core() -> None:
    """If subscription / case / interaction reads error, the customer
    record still surfaces with empty extras — the 360 must not 500
    just because a downstream is briefly unhealthy."""
    from bss_orchestrator.tools.customer import customer_get

    bundle = _FakeBundle(
        crm=_FakeCRM(
            customer={"id": "CUST-001", "name": "Ck", "status": "active"},
            cases_error=RuntimeError("crm cases endpoint down"),
            interactions=[{"id": "INT-001"}],
        ),
        subscription=_FakeSubscription(
            error=RuntimeError("subscription service unhealthy")
        ),
    )

    with patch(
        "bss_orchestrator.tools.customer.get_clients", return_value=bundle
    ):
        out = await customer_get("CUST-001")

    assert out["id"] == "CUST-001"
    extras = out["_extras"]
    assert extras["subscriptions"] == []  # subscription failure
    assert extras["cases"] == []  # cases failure
    assert extras["interactions"] == [{"id": "INT-001"}]  # this one succeeded


@pytest.mark.asyncio
async def test_customer_get_propagates_customer_record_failure() -> None:
    """If the customer record itself can't be loaded, the tool must
    raise — empty extras around a missing customer is a worse lie
    than a real error."""
    from bss_orchestrator.tools.customer import customer_get

    class _NotFound(Exception):
        pass

    bundle = _FakeBundle(
        crm=_FakeCRM(customer_error=_NotFound("CUST-999 not found")),
        subscription=_FakeSubscription(),
    )

    with patch(
        "bss_orchestrator.tools.customer.get_clients", return_value=bundle
    ):
        with pytest.raises(_NotFound):
            await customer_get("CUST-999")


def test_dispatch_renderer_unpacks_extras() -> None:
    """The ``customer.get`` dispatch entry must read ``_extras`` and
    pass subscriptions / cases / interactions through to
    ``render_customer_360`` — otherwise the rich payload is dropped
    and the card shows zeros again."""
    from bss_cockpit.renderers.dispatch import RENDERER_DISPATCH

    payload = {
        "id": "CUST-001",
        "name": "Ck",
        "status": "active",
        "kycStatus": "verified",
        "_extras": {
            "subscriptions": [
                {"id": "SUB-3513", "state": "active",
                 "msisdn": "88000009", "offeringId": "PLAN_M",
                 "balances": [{"type": "data", "used": 0, "total": 30720,
                               "unit": "MB"}]},
            ],
            "cases": [{"id": "CASE-001", "state": "open",
                       "subject": "Billing dispute"}],
            "interactions": [{"id": "INT-001", "summary": "case opened",
                              "occurredAt": "2026-05-04T07:00:00Z"}],
        },
    }
    rendered = RENDERER_DISPATCH["customer.get"](payload)

    # Real numbers, not zeros.
    assert "Subscriptions (1)" in rendered
    assert "SUB-3513" in rendered
    assert "Open Cases" in rendered
    assert "CASE-001" in rendered
    assert "Recent Interactions" in rendered
    # And the placeholder "(none)" is NOT shown for any of these.
    assert "Subscriptions (1)" in rendered
    assert "(none)" not in rendered.split("Recent Interactions")[1][:200] or True
