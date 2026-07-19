"""Tests for the shared admin reset router.

We don't spin up Postgres here — the purpose of these tests is to prove
the router *behaves correctly at its boundaries*: it enforces the env gate
before touching any state, composes schema-qualified SQL, and records the
audit marker. The SQL shape is asserted against a stand-in session that
captures ``execute()`` calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from bss_admin import ResetPlan, TableReset, admin_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeSession:
    """Captures ``execute`` calls and supports ``async with session.begin()``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def begin(self) -> "_FakeTxn":
        return _FakeTxn()

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        # ``stmt`` is a ``TextClause``; ``str(stmt)`` gives the raw SQL.
        self.calls.append((str(stmt), params))
        return SimpleNamespace(rowcount=0)


class _FakeTxn:
    async def __aenter__(self) -> "_FakeTxn":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.session = _FakeSession()

    def __call__(self) -> "_FakeSessionCtx":
        return _FakeSessionCtx(self.session)


@dataclass
class _FakeSessionCtx:
    session: _FakeSession

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _mk_app(
    plans: list[ResetPlan], allow: bool
) -> tuple[FastAPI, _FakeSessionFactory]:
    app = FastAPI()
    app.include_router(
        admin_router(service_name="test", plans=plans), prefix="/admin-api/v1"
    )
    factory = _FakeSessionFactory()
    app.state.session_factory = factory
    if allow:
        os.environ["BSS_ALLOW_ADMIN_RESET"] = "true"
    else:
        os.environ.pop("BSS_ALLOW_ADMIN_RESET", None)
    return app, factory


def test_returns_403_when_flag_unset() -> None:
    app, _ = _mk_app([ResetPlan(schema="x", tables=(TableReset("t"),))], allow=False)
    client = TestClient(app)
    r = client.post("/admin-api/v1/reset-operational-data")
    assert r.status_code == 403
    body = r.json()
    assert body["detail"]["code"] == "ADMIN_RESET_DISABLED"


def test_truncates_each_listed_table_with_schema_prefix() -> None:
    plan = ResetPlan(
        schema="crm",
        tables=(TableReset("customer"), TableReset("case")),
    )
    app, factory = _mk_app([plan], allow=True)
    client = TestClient(app)
    r = client.post("/admin-api/v1/reset-operational-data")
    assert r.status_code == 200
    sqls = [sql for sql, _ in factory.session.calls]
    # Every TRUNCATE is schema-qualified and uses RESTART IDENTITY CASCADE.
    assert any('TRUNCATE TABLE "crm"."customer" RESTART IDENTITY CASCADE' in s for s in sqls)
    assert any('TRUNCATE TABLE "crm"."case" RESTART IDENTITY CASCADE' in s for s in sqls)
    # And an audit marker insert is issued at the end.
    assert any("INSERT INTO audit.domain_event" in s for s in sqls)


def test_update_mode_runs_the_provided_sql_verbatim() -> None:
    plan = ResetPlan(
        schema="inventory",
        tables=(
            TableReset(
                "msisdn_pool",
                mode="update",
                update_sql='UPDATE "inventory"."msisdn_pool" SET status = \'available\'',
            ),
        ),
    )
    app, factory = _mk_app([plan], allow=True)
    client = TestClient(app)
    r = client.post("/admin-api/v1/reset-operational-data")
    assert r.status_code == 200
    sqls = [sql for sql, _ in factory.session.calls]
    assert any(
        "UPDATE \"inventory\".\"msisdn_pool\" SET status = 'available'" in s for s in sqls
    )


def test_response_body_lists_truncated_and_updated_per_schema() -> None:
    plans = [
        ResetPlan(schema="crm", tables=(TableReset("customer"),)),
        ResetPlan(
            schema="inventory",
            tables=(
                TableReset(
                    "msisdn_pool",
                    mode="update",
                    update_sql='UPDATE "inventory"."msisdn_pool" SET status=\'available\'',
                ),
            ),
        ),
    ]
    app, _ = _mk_app(plans, allow=True)
    client = TestClient(app)
    r = client.post("/admin-api/v1/reset-operational-data")
    body = r.json()
    assert body["service"] == "test"
    assert len(body["schemas"]) == 2
    crm = next(s for s in body["schemas"] if s["schema"] == "crm")
    inv = next(s for s in body["schemas"] if s["schema"] == "inventory")
    assert crm["truncated"] == ["customer"]
    assert crm["updated"] == []
    assert inv["truncated"] == []
    assert inv["updated"] == ["msisdn_pool"]


def test_update_mode_without_sql_raises() -> None:
    plan = ResetPlan(schema="x", tables=(TableReset("t", mode="update"),))
    app, _ = _mk_app([plan], allow=True)
    client = TestClient(app)
    with pytest.raises(RuntimeError, match="update_sql"):
        client.post("/admin-api/v1/reset-operational-data")
