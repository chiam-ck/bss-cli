"""v0.10 PR 9 — paginated charge history at /billing/history.

V0_10_0.md Track 8. Read-only; no step-up; no portal_action audit.
The load-bearing assertions: pagination correctness, server-side
customer scoping (no cross-customer leak), card-removed gracefully
rendered.
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
    async with factory() as s:
        await s.execute(text(
            "TRUNCATE portal_auth.portal_action, portal_auth.login_attempt, "
            "portal_auth.session, portal_auth.login_token, "
            "portal_auth.identity RESTART IDENTITY CASCADE"
        ))
        await s.commit()
    yield factory
    async with factory() as s:
        await s.execute(text(
            "TRUNCATE portal_auth.portal_action, portal_auth.login_attempt, "
            "portal_auth.session, portal_auth.login_token, "
            "portal_auth.identity RESTART IDENTITY CASCADE"
        ))
        await s.commit()
    await engine.dispose()


async def _setup(seed_db, customer_id="CUST-042"):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id=customer_id, verified=True
        )
        await db.commit()
        return sess.id


def _seed_payment(fake_clients, *, customer_id, idx, status="succeeded",
                  amount="15.00", purpose="subscription_renewal",
                  payment_method_id="PM-0001"):
    fake_clients.payment.payments_by_customer.setdefault(customer_id, []).append({
        "id": f"PAY-{idx:06d}",
        "customerId": customer_id,
        "paymentMethodId": payment_method_id,
        "amount": amount,
        "currency": "SGD",
        "purpose": purpose,
        "status": status,
        "attemptedAt": f"2026-04-{idx + 1:02d}T10:00:00+00:00",
        "declineReason": None,
        "gatewayRef": f"gtw-{idx:06d}",
    })


def _seed_method(fake_clients, *, customer_id, pm_id="PM-0001", last4="4242"):
    fake_clients.payment.methods_by_customer.setdefault(customer_id, []).append({
        "id": pm_id,
        "customerId": customer_id,
        "brand": "visa",
        "last4": last4,
        "expMonth": 12,
        "expYear": 2030,
        "isDefault": True,
    })


# ── Empty state ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_renders_empty_state(seed_db, fake_clients):
    sid = await _setup(seed_db)
    with patch(
        "bss_self_serve.routes.billing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/billing/history")
            assert resp.status_code == 200
            assert "No charges on file yet" in resp.text


# ── Single page ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_renders_single_page_with_method_last4(seed_db, fake_clients):
    sid = await _setup(seed_db)
    _seed_method(fake_clients, customer_id="CUST-042", pm_id="PM-0001", last4="4242")
    for i in range(3):
        _seed_payment(fake_clients, customer_id="CUST-042", idx=i)

    with patch(
        "bss_self_serve.routes.billing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/billing/history")
            assert resp.status_code == 200
            body = resp.text
            assert "3 charges on file" in body
            assert "Page 1 of 1" in body
            for i in range(3):
                assert f"PAY-{i:06d}" in body
            assert "•••• 4242" in body
            # Customer-facing description from the purpose mapping.
            assert "Subscription renewal" in body


# ── Pagination ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_paginates_at_20_per_page(seed_db, fake_clients):
    sid = await _setup(seed_db)
    _seed_method(fake_clients, customer_id="CUST-042")
    # Seed 25 charges so page 0 has 20, page 1 has 5.
    for i in range(25):
        _seed_payment(fake_clients, customer_id="CUST-042", idx=i)

    with patch(
        "bss_self_serve.routes.billing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)

            # Page 0 — 20 rows + Next link to page=1.
            r = c.get("/billing/history?page=0")
            assert r.status_code == 200
            assert "25 charges on file" in r.text
            assert "Page 1 of 2" in r.text
            assert 'href="/billing/history?page=1"' in r.text
            # Previous is hidden on page 0.
            assert 'href="/billing/history?page=-1"' not in r.text
            # First-page rows present, last-page rows NOT yet.
            assert "PAY-000000" in r.text
            assert "PAY-000019" in r.text
            assert "PAY-000024" not in r.text

            # Page 1 — 5 rows + Previous link, no Next.
            r = c.get("/billing/history?page=1")
            assert r.status_code == 200
            assert "Page 2 of 2" in r.text
            assert 'href="/billing/history?page=0"' in r.text
            assert 'href="/billing/history?page=2"' not in r.text
            assert "PAY-000020" in r.text
            assert "PAY-000024" in r.text


# ── Server-side scoping (no leak) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_does_not_leak_other_customers_payments(
    seed_db, fake_clients
):
    """Bob has charges, Ada has none — Ada's history must be empty."""
    sid = await _setup(seed_db, customer_id="CUST-ADA")
    _seed_payment(fake_clients, customer_id="CUST-BOB", idx=42)
    _seed_method(fake_clients, customer_id="CUST-BOB")

    with patch(
        "bss_self_serve.routes.billing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            r = c.get("/billing/history")
            assert r.status_code == 200
            assert "PAY-000042" not in r.text
            assert "No charges on file" in r.text


# ── Removed-method last-4 fallback ───────────────────────────────────────


@pytest.mark.asyncio
async def test_history_renders_card_removed_when_method_no_longer_active(
    seed_db, fake_clients
):
    """A charge against a since-removed PM still renders, with "(removed)"
    in the card column instead of last-4."""
    sid = await _setup(seed_db)
    # Seed payment but NO active method for that PM id.
    _seed_payment(
        fake_clients,
        customer_id="CUST-042",
        idx=0,
        payment_method_id="PM-DELETED",
    )

    with patch(
        "bss_self_serve.routes.billing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            r = c.get("/billing/history")
            assert r.status_code == 200
            assert "(removed)" in r.text


# ── Decline reason surfaced ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_renders_decline_reason_for_failed_charges(
    seed_db, fake_clients
):
    sid = await _setup(seed_db)
    _seed_method(fake_clients, customer_id="CUST-042")
    fake_clients.payment.payments_by_customer["CUST-042"] = [{
        "id": "PAY-FAIL-1",
        "customerId": "CUST-042",
        "paymentMethodId": "PM-0001",
        "amount": "25.00",
        "currency": "SGD",
        "purpose": "subscription_renewal",
        "status": "declined",
        "attemptedAt": "2026-04-15T10:00:00+00:00",
        "declineReason": "insufficient_funds",
        "gatewayRef": "gtw-FAIL-1",
    }]

    with patch(
        "bss_self_serve.routes.billing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            r = c.get("/billing/history")
            assert r.status_code == 200
            assert "billing-status--declined" in r.text
            assert "insufficient_funds" in r.text


# ── No session ───────────────────────────────────────────────────────────


def test_no_session_redirects_to_login(seed_db, fake_clients):
    with patch(
        "bss_self_serve.routes.billing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            r = c.get("/billing/history", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"].startswith("/auth/login")
