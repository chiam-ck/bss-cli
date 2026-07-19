"""Tests for the shared audit-events router.

We don't spin up Postgres — the point is to prove the handler
translates query params into the correct SQL and shapes the
response body. A stand-in session captures ``execute`` calls and
returns a canned row set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from bss_events import audit_events_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeSession:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.last_sql: str | None = None
        self.last_params: dict[str, Any] | None = None

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        self.last_sql = str(stmt)
        self.last_params = params or {}
        rows = self._rows
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))


@dataclass
class _FakeSessionCtx:
    session: _FakeSession

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.session = _FakeSession(rows or [])

    def __call__(self) -> _FakeSessionCtx:
        return _FakeSessionCtx(self.session)


def _row(
    *,
    event_type: str = "order.created",
    aggregate_type: str = "Order",
    aggregate_id: str = "ORD-001",
    occurred_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
    event_id: UUID | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id or uuid4(),
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at or datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
        "trace_id": "4a8f9e2c0123456789abcdef01234567",
        "actor": "system",
        "channel": "cli",
        "tenant_id": "DEFAULT",
        "service_identity": "default",
        "payload": payload or {"note": "hello"},
        "schema_version": 1,
        "published_to_mq": False,
    }


def _mk_app(rows: list[dict[str, Any]]) -> tuple[FastAPI, _FakeSessionFactory]:
    app = FastAPI()
    app.include_router(audit_events_router(), prefix="/audit-api/v1")
    factory = _FakeSessionFactory(rows)
    app.state.session_factory = factory
    return app, factory


def test_get_events_returns_rows_shaped_to_camel_case() -> None:
    eid = uuid4()
    app, _ = _mk_app([_row(event_id=eid, payload={"orderId": "ORD-001"})])
    client = TestClient(app)
    r = client.get("/audit-api/v1/events")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    e = body["events"][0]
    assert e["eventId"] == str(eid)
    assert e["eventType"] == "order.created"
    assert e["aggregateType"] == "Order"
    assert e["aggregateId"] == "ORD-001"
    assert e["occurredAt"].startswith("2026-04-01T10:00:00")
    assert e["traceId"] == "4a8f9e2c0123456789abcdef01234567"
    assert e["payload"] == {"orderId": "ORD-001"}
    assert e["publishedToMq"] is False


def test_no_filters_issues_bare_select_with_default_limit() -> None:
    app, factory = _mk_app([])
    client = TestClient(app)
    r = client.get("/audit-api/v1/events")
    assert r.status_code == 200
    sql = factory.session.last_sql or ""
    # No WHERE clause when no filters supplied.
    assert "WHERE" not in sql
    assert "ORDER BY occurred_at ASC, id ASC" in sql
    assert factory.session.last_params["limit"] == 100


def test_service_identity_filter_binds_param() -> None:
    """v0.9 — ?serviceIdentity=portal_self_serve scopes the audit query."""
    app, factory = _mk_app([])
    client = TestClient(app)
    r = client.get(
        "/audit-api/v1/events",
        params={"serviceIdentity": "portal_self_serve"},
    )
    assert r.status_code == 200
    sql = factory.session.last_sql or ""
    assert "service_identity = :service_identity" in sql
    assert factory.session.last_params["service_identity"] == "portal_self_serve"


def test_service_identity_response_field_camel_case() -> None:
    """v0.9 — the row exposes serviceIdentity in the JSON shape."""
    app, _ = _mk_app([_row()])
    client = TestClient(app)
    r = client.get("/audit-api/v1/events")
    assert r.status_code == 200
    e = r.json()["events"][0]
    assert e["serviceIdentity"] == "default"


def test_aggregate_and_event_type_filters_bind_params() -> None:
    app, factory = _mk_app([])
    client = TestClient(app)
    r = client.get(
        "/audit-api/v1/events",
        params={
            "aggregateType": "Order",
            "aggregateId": "ORD-042",
            "eventType": "order.completed",
            "limit": 5,
        },
    )
    assert r.status_code == 200
    sql = factory.session.last_sql or ""
    assert "aggregate_type = :aggregate_type" in sql
    assert "aggregate_id = :aggregate_id" in sql
    assert "event_type = :event_type" in sql
    p = factory.session.last_params
    assert p["aggregate_type"] == "Order"
    assert p["aggregate_id"] == "ORD-042"
    assert p["event_type"] == "order.completed"
    assert p["limit"] == 5


def test_event_type_prefix_filter_binds_with_trailing_percent() -> None:
    app, factory = _mk_app([])
    client = TestClient(app)
    r = client.get("/audit-api/v1/events", params={"eventTypePrefix": "order."})
    assert r.status_code == 200
    assert "event_type LIKE :event_type_prefix" in (factory.session.last_sql or "")
    assert factory.session.last_params["event_type_prefix"] == "order.%"


def test_occurred_since_and_until_parse_iso() -> None:
    app, factory = _mk_app([])
    client = TestClient(app)
    r = client.get(
        "/audit-api/v1/events",
        params={
            "occurredSince": "2026-04-01T00:00:00+00:00",
            "occurredUntil": "2026-04-02T00:00:00+00:00",
        },
    )
    assert r.status_code == 200
    sql = factory.session.last_sql or ""
    assert "occurred_at >= :since" in sql
    assert "occurred_at <= :until" in sql
    assert factory.session.last_params["since"] == datetime(
        2026, 4, 1, tzinfo=timezone.utc
    )
    assert factory.session.last_params["until"] == datetime(
        2026, 4, 2, tzinfo=timezone.utc
    )


def test_bad_occurred_since_returns_422() -> None:
    app, _ = _mk_app([])
    client = TestClient(app)
    r = client.get("/audit-api/v1/events", params={"occurredSince": "not-a-date"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_TIMESTAMP"


@pytest.mark.parametrize("bad_limit", [0, -1, 1001, 10_000])
def test_limit_out_of_bounds_is_422(bad_limit: int) -> None:
    app, _ = _mk_app([])
    client = TestClient(app)
    r = client.get("/audit-api/v1/events", params={"limit": bad_limit})
    assert r.status_code == 422


def test_empty_result_set_returns_zero_count() -> None:
    app, _ = _mk_app([])
    client = TestClient(app)
    r = client.get("/audit-api/v1/events")
    assert r.status_code == 200
    assert r.json() == {"events": [], "count": 0}
