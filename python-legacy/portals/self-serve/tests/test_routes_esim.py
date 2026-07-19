"""v0.10 — read-only eSIM redownload at /esim/<subscription_id>.

V0_10_0.md Track 5. The route is read-only (no step-up, no
portal_action audit row on the success path) but ownership-checked
server-side: cross-customer / unknown id → 403, not 404.

Production-fidelity caveat is documented in DECISIONS 2026-04-27 and
in the route docstring — this is a deliberately simplified
re-display of the activation code minted at signup, not a real
GSMA SGP.22 rearm flow.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "BSS_PORTAL_TOKEN_PEPPER",
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
)
os.environ.setdefault("BSS_PORTAL_EMAIL_ADAPTER", "noop")
os.environ.setdefault("BSS_PORTAL_EMAIL_PROVIDER", "noop")  # v0.14 — both names handled
os.environ.setdefault("BSS_PORTAL_DEV_INSECURE_COOKIE", "1")

from bss_clock.clock import reset_for_tests as _reset_clock  # noqa: E402
from bss_portal_auth.test_helpers import create_test_session  # noqa: E402
from bss_self_serve.config import Settings  # noqa: E402
from bss_self_serve.main import create_app  # noqa: E402
from bss_self_serve.middleware import PORTAL_SESSION_COOKIE  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]


class _DbSettings(BaseSettings):
    BSS_DB_URL: str = ""
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@pytest.fixture(autouse=True)
def _clock():
    _reset_clock()
    yield
    _reset_clock()


@pytest.fixture
def db_url() -> str:
    url = _DbSettings().BSS_DB_URL or os.environ.get("BSS_DB_URL", "")
    if not url:
        pytest.fail("BSS_DB_URL is not set. Export it or add to .env.")
    os.environ["BSS_DB_URL"] = url
    return url


@pytest_asyncio.fixture
async def seed_db(db_url: str):
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _scrub(s):
        await s.execute(text(
            "TRUNCATE portal_auth.portal_action, portal_auth.login_attempt, "
            "portal_auth.session, portal_auth.login_token, "
            "portal_auth.identity RESTART IDENTITY CASCADE"
        ))

    async with factory() as s:
        await _scrub(s)
        await s.commit()
    yield factory
    async with factory() as s:
        await _scrub(s)
        await s.commit()
    await engine.dispose()


def _seed_subscription(fake_clients, *, sub_id, customer_id, iccid="8910010000000000123",
                       msisdn="91234567"):
    fake_clients.subscription.records[sub_id] = {
        "id": sub_id,
        "customerId": customer_id,
        "offeringId": "PLAN_M",
        "msisdn": msisdn,
        "iccid": iccid,
        "state": "active",
        "stateReason": None,
    }
    fake_clients.subscription.by_customer.setdefault(customer_id, []).append(sub_id)
    fake_clients.inventory.activations[iccid] = {
        "iccid": iccid,
        "activation_code": f"LPA:1$smdp.example.com${iccid}-MATCH",
        "smdp_server": "smdp.example.com",
        "matching_id": f"{iccid}-MATCH",
    }


async def _setup(seed_db, customer_id="CUST-042"):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id=customer_id, verified=True
        )
        await db.commit()
        return sess.id


# ── Owner ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_sees_lpa_code_qr_and_last4_meta(seed_db, fake_clients):
    sid = await _setup(seed_db)
    _seed_subscription(
        fake_clients, sub_id="SUB-001", customer_id="CUST-042",
        iccid="8910010000000000123", msisdn="91234567",
    )

    with patch(
        "bss_self_serve.routes.esim.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/esim/SUB-001")
            assert resp.status_code == 200
            body = resp.text

            # LPA activation code shown verbatim, ready to copy.
            assert "LPA:1$smdp.example.com$8910010000000000123-MATCH" in body
            # Inline PNG QR (data URI).
            assert "data:image/png;base64," in body
            # ICCID + IMSI render last-4 only by default.
            assert "…0123" in body
            # MSISDN visible.
            assert "91234567" in body
            # Production-fidelity caveat surfaced to the customer.
            assert "Changing devices?" in body or "doesn&#39;t yet automate" in body


# ── Cross-customer attempt ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_customer_returns_403_not_404(seed_db, fake_clients):
    """Bob can't see Ada's eSIM — 403, not 404 (the resource exists)."""
    sid = await _setup(seed_db, customer_id="CUST-BOB")
    _seed_subscription(
        fake_clients, sub_id="SUB-001", customer_id="CUST-ADA",
        iccid="8910010000000000999",
    )

    with patch(
        "bss_self_serve.routes.esim.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/esim/SUB-001")
            assert resp.status_code == 403
            assert "belong to your account" in resp.text
            # Ada's activation code never appears in Bob's response.
            assert "8910010000000000999" not in resp.text


# ── Unknown id ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_subscription_returns_403_same_as_cross_customer(
    seed_db, fake_clients
):
    """We deliberately do not distinguish 'unknown' from 'not yours'."""
    sid = await _setup(seed_db)
    # No subscription seeded.

    with patch(
        "bss_self_serve.routes.esim.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/esim/SUB-NEVER")
            assert resp.status_code == 403


# ── No session ────────────────────────────────────────────────────────────


def test_no_session_redirects_to_login(seed_db, fake_clients):
    with patch(
        "bss_self_serve.routes.esim.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            resp = c.get("/esim/SUB-001", follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"].startswith("/auth/login")


# ── ?show_full=1 reserved (admin-only; v0.10 silently ignores) ────────────


@pytest.mark.asyncio
async def test_show_full_param_is_silently_ignored_for_customer(
    seed_db, fake_clients
):
    """v0.10 portal sessions have no admin role, so show_full=1 from a
    customer must NOT reveal full ICCID / IMSI in the meta section.
    The phase doc reserves the param for the v0.12 CSR-portal admin
    auth path. (The LPA activation code itself embeds an ICCID-derived
    matching id by design — that's how SGP.22 activation codes work
    and is not the concern this guard covers.)"""
    sid = await _setup(seed_db, customer_id="CUST-042")
    iccid = "8910010000000000123"
    fake_clients.subscription.records["SUB-001"] = {
        "id": "SUB-001",
        "customerId": "CUST-042",
        "offeringId": "PLAN_M",
        "msisdn": "91234567",
        "iccid": iccid,
        "state": "active",
        "stateReason": None,
    }
    fake_clients.subscription.by_customer["CUST-042"] = ["SUB-001"]
    # Use an opaque LPA value so the activation-code string doesn't
    # itself embed the raw ICCID; that lets us assert the meta-section
    # masking specifically.
    fake_clients.inventory.activations[iccid] = {
        "iccid": iccid,
        "activation_code": "LPA:1$smdp.example.com$OPAQUE-MATCH-ID",
        "smdp_server": "smdp.example.com",
        "matching_id": "OPAQUE-MATCH-ID",
    }

    with patch(
        "bss_self_serve.routes.esim.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/esim/SUB-001?show_full=1")
            assert resp.status_code == 200
            # Full ICCID never appears in the meta section even with show_full=1.
            assert iccid not in resp.text
            # Last-4 still rendered.
            assert "…0123" in resp.text


# ── Activation lookup failure renders the page with a banner ──────────────


@pytest.mark.asyncio
async def test_activation_lookup_failure_falls_back_gracefully(
    seed_db, fake_clients
):
    sid = await _setup(seed_db)
    _seed_subscription(fake_clients, sub_id="SUB-001", customer_id="CUST-042")
    fake_clients.inventory.next_error = RuntimeError("simulated upstream failure")

    with patch(
        "bss_self_serve.routes.esim.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/esim/SUB-001")
            assert resp.status_code == 200
            # Customer-facing fallback copy when the activation code can't load.
            assert "couldn&#39;t fetch the activation code" in resp.text or \
                   "couldn't fetch the activation code" in resp.text
