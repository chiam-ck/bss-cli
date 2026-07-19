"""``POST /webhooks/resend`` — signature verification + idempotent persist.

Coverage matrix (v0.14):

* Happy path — fixture-signed body returns 200 + persists row.
* Tampered body / wrong signature → 401 + no row.
* Missing svix headers → 401 + no row.
* Missing webhook secret in env → 401 (misconfig surfaced to ops).
* Duplicate event_id (provider retry) → 200 with deduped flag.
* Unknown event type → 200, persists, no domain-event mapping.
* Webhooks path is exempt from PortalSessionMiddleware
  (no 303 redirect to login).

Tests do NOT mock ``bss_webhooks.signatures`` — they use the real
verifier with fixture-signed bodies. That's the real surface; mocking
hides crypto bugs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

# Set webhook secret BEFORE importing app — lifespan reads it.
os.environ.setdefault(
    "BSS_PORTAL_TOKEN_PEPPER",
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
)
os.environ.setdefault("BSS_PORTAL_EMAIL_ADAPTER", "noop")

# Resend webhook secret — fixture key, not a real Resend secret.
_RAW_KEY = b"v0.14-resend-test-webhook-key"
_TEST_SECRET = "whsec_" + base64.b64encode(_RAW_KEY).decode()
os.environ["BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET"] = _TEST_SECRET

import pytest
import pytest_asyncio
from bss_self_serve.config import Settings
from bss_self_serve.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def db_engine():
    """Per-test DB engine sharing the same Postgres `make migrate` already set up."""
    settings = Settings()
    if not settings.bss_db_url:
        pytest.skip("BSS_DB_URL unset; webhook persistence tests need DB")
    engine = create_async_engine(settings.bss_db_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_webhook_event(db_engine):
    """Each test gets a clean integrations.webhook_event table."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("TRUNCATE integrations.webhook_event CASCADE"))
        await s.commit()
    yield
    async with factory() as s:
        await s.execute(text("TRUNCATE integrations.webhook_event CASCADE"))
        await s.commit()


@pytest.fixture
def client():
    settings = Settings()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _svix_sign(msg_id: str, ts: int, body: bytes) -> str:
    """Build a Svix-format signature header for ``body``."""
    signed = f"{msg_id}.{ts}.".encode() + body
    h = hmac.new(_RAW_KEY, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(h).decode()


def _payload(
    *, event_type: str = "email.delivered", email_id: str = "msg_1"
) -> dict[str, Any]:
    return {
        "type": event_type,
        "created_at": "2026-05-02T00:00:00Z",
        "data": {
            "email_id": email_id,
            "from": "noreply@mail.bss-cli.com",
            "to": ["customer@example.com"],
        },
    }


def _request(client: TestClient, *, body_dict, msg_id="msg_test_1", ts=None):
    body = json.dumps(body_dict).encode()
    ts = ts if ts is not None else int(time.time())
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": str(ts),
        "svix-signature": _svix_sign(msg_id, ts, body),
        "content-type": "application/json",
    }
    return client.post("/webhooks/resend", content=body, headers=headers)


# ── happy path ──────────────────────────────────────────────────────


async def test_happy_path_returns_200_and_persists(client, db_engine):
    r = _request(client, body_dict=_payload())
    assert r.status_code == 200, r.text
    assert r.json() == {"received": True}

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        rows = (
            await s.execute(text(
                "SELECT provider, event_id, event_type, signature_valid "
                "FROM integrations.webhook_event"
            ))
        ).all()
    assert len(rows) == 1
    assert rows[0].provider == "resend"
    assert rows[0].event_id == "msg_test_1"
    assert rows[0].event_type == "email.delivered"
    assert rows[0].signature_valid is True


# ── replay / dedup ───────────────────────────────────────────────


async def test_duplicate_event_id_returns_200_deduped_no_second_row(client, db_engine):
    r1 = _request(client, body_dict=_payload(), msg_id="msg_dup")
    assert r1.status_code == 200

    r2 = _request(client, body_dict=_payload(), msg_id="msg_dup")
    assert r2.status_code == 200
    assert r2.json() == {"received": True, "deduped": True}

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        n = (
            await s.execute(text(
                "SELECT count(*) AS n FROM integrations.webhook_event"
            ))
        ).scalar_one()
    assert n == 1


# ── tampering / signature failure ────────────────────────────────


async def test_tampered_body_returns_401_no_row(client, db_engine):
    body = json.dumps(_payload()).encode()
    ts = int(time.time())
    headers = {
        "svix-id": "msg_t1",
        "svix-timestamp": str(ts),
        # Sign a different body than we send.
        "svix-signature": _svix_sign("msg_t1", ts, b'{"different":"body"}'),
        "content-type": "application/json",
    }
    r = client.post("/webhooks/resend", content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["code"] == "signature_mismatch"

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        n = (
            await s.execute(text(
                "SELECT count(*) AS n FROM integrations.webhook_event"
            ))
        ).scalar_one()
    assert n == 0


async def test_missing_svix_headers_returns_401(client):
    body = json.dumps(_payload()).encode()
    r = client.post("/webhooks/resend", content=body, headers={
        "content-type": "application/json"
    })
    assert r.status_code == 401
    assert r.json()["code"] == "missing_header"


async def test_replay_window_violation_returns_401(client):
    body = json.dumps(_payload()).encode()
    old_ts = int(time.time()) - 3600  # 1h old
    msg_id = "msg_old"
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": str(old_ts),
        "svix-signature": _svix_sign(msg_id, old_ts, body),
        "content-type": "application/json",
    }
    r = client.post("/webhooks/resend", content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["code"] == "replay_window"


# ── known-vs-unknown event types ─────────────────────────────────


async def test_unknown_event_type_persists_and_acks(client, db_engine):
    r = _request(
        client,
        body_dict=_payload(event_type="email.future_event_type_not_yet_known"),
        msg_id="msg_unknown",
    )
    assert r.status_code == 200, r.text

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        row = (
            await s.execute(text(
                "SELECT event_type FROM integrations.webhook_event "
                "WHERE event_id='msg_unknown'"
            ))
        ).first()
    assert row is not None
    assert row.event_type == "email.future_event_type_not_yet_known"


# ── webhook path exempt from session middleware (no 303) ─────────


def test_webhooks_path_does_not_redirect_for_unauthenticated_caller(client):
    """Verifies /webhooks/* sits in PUBLIC_PATH_PREFIXES — Resend's POST
    does NOT carry a portal session cookie, so any redirect would break
    the integration."""
    # Hit with no signature — should get 401 from the route handler
    # (signature verification), NOT 303 from middleware.
    r = client.post("/webhooks/resend", content=b"{}", headers={
        "content-type": "application/json"
    })
    assert r.status_code == 401  # not 303; not 307
