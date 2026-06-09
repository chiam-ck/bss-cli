"""v1.6 chat handoff — CRM screens → focused session with a drafted
message prefilled (never auto-sent).

Same DB-backed style as test_cockpit_routes: boots create_app against
the dev DB and truncates cockpit.* around each test.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from bss_csr.config import Settings
from bss_csr.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def db_url() -> str:
    url = os.environ.get("BSS_DB_URL", "")
    if not url:
        pytest.skip("BSS_DB_URL not set; skipping handoff DB tests.")
    return url


@pytest.fixture
def cockpit_client(db_url: str):
    async def _truncate():
        engine = create_async_engine(db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            await s.execute(text(
                "TRUNCATE cockpit.pending_destructive, cockpit.message, "
                "cockpit.session RESTART IDENTITY CASCADE"
            ))
            await s.commit()
        await engine.dispose()

    asyncio.run(_truncate())
    app = create_app(Settings())
    with TestClient(app) as c:
        yield c
    asyncio.run(_truncate())


def test_handoff_opens_focused_session_with_draft(cockpit_client) -> None:
    r = cockpit_client.post(
        "/cockpit/handoff",
        data={
            "customer_id": "CUST-001",
            "draft": "Terminate subscription SUB-007 — reason: ",
            "label": "from customer page",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/cockpit/SES-")
    assert "draft=" in location

    # The thread page prefills the compose box with the draft; nothing
    # is sent — the transcript stays empty until the operator submits.
    r2 = cockpit_client.get(location)
    assert r2.status_code == 200
    assert "Terminate subscription SUB-007" in r2.text
    assert "chat-bubble-user" not in r2.text


def test_handoff_without_customer_or_draft_still_opens_session(
    cockpit_client,
) -> None:
    r = cockpit_client.post("/cockpit/handoff", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/cockpit/SES-")
    assert "draft=" not in r.headers["location"]
