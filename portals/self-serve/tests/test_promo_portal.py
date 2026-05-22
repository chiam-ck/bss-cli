"""v1.1 — portal promo surface: signup live preview + dashboard discount/offers.

The portal holds NO loyalty token; everything routes through the catalog
client (faked here). Mirrors test_dashboard.py's session + patch setup.
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
os.environ.setdefault("BSS_PORTAL_EMAIL_PROVIDER", "noop")
os.environ.setdefault("BSS_PORTAL_DEV_INSECURE_COOKIE", "1")

from bss_portal_auth.test_helpers import create_test_session  # noqa: E402
from bss_self_serve.config import Settings  # noqa: E402
from bss_self_serve.main import create_app  # noqa: E402
from bss_self_serve.middleware import PORTAL_SESSION_COOKIE  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TRUNCATE = (
    "TRUNCATE portal_auth.login_attempt, portal_auth.session, "
    "portal_auth.login_token, portal_auth.identity RESTART IDENTITY CASCADE"
)


class _DbSettings(BaseSettings):
    BSS_DB_URL: str = ""
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )


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
        await s.execute(text(_TRUNCATE))
        await s.commit()
    yield factory
    async with factory() as s:
        await s.execute(text(_TRUNCATE))
        await s.commit()
    await engine.dispose()


def _active_sub_with_discount(**discount) -> dict:
    return {
        "id": "SUB-001",
        "href": "/subscription-api/v1/subscription/SUB-001",
        "customerId": "CUST-042",
        "offeringId": "PLAN_M",
        "msisdn": "91234567",
        "iccid": "8910010000000000123",
        "state": "active",
        "stateReason": None,
        "currentPeriodStart": "2026-04-27T00:00:00+00:00",
        "currentPeriodEnd": "2026-05-27T00:00:00+00:00",
        "nextRenewalAt": "2026-05-27T00:00:00+00:00",
        "terminatedAt": None,
        "balances": [],
        "pendingOfferingId": None,
        "pendingEffectiveAt": None,
        "priceAmount": "25.00",
        "priceCurrency": "SGD",
        **discount,
    }


@pytest.mark.asyncio
async def test_dashboard_shows_applied_discount_badge(seed_db, fake_clients):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id

    fake_clients.subscription.records = {
        "SUB-001": _active_sub_with_discount(
            discountType="percent",
            discountValue="20",
            discountPeriodsRemaining=2,
            effectiveAmount="20.00",
            promoCode="SUMMER",
        )
    }
    fake_clients.subscription.by_customer = {"CUST-042": ["SUB-001"]}

    with patch("bss_self_serve.routes.landing.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            body = c.get("/").text
    assert "20% off" in body
    assert "SGD 20.00/mo" in body
    assert "2 more renewals" in body
    assert "then SGD 25.00/mo" in body


@pytest.mark.asyncio
async def test_dashboard_hides_badge_when_discount_exhausted(seed_db, fake_clients):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id
    # remaining 0 → promo done, no badge
    fake_clients.subscription.records = {
        "SUB-001": _active_sub_with_discount(
            discountType="percent", discountValue="20",
            discountPeriodsRemaining=0, effectiveAmount="25.00",
        )
    }
    fake_clients.subscription.by_customer = {"CUST-042": ["SUB-001"]}
    with patch("bss_self_serve.routes.landing.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            body = c.get("/").text
    assert "line-card-discount" not in body


@pytest.mark.asyncio
async def test_dashboard_shows_assigned_offer(seed_db, fake_clients):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id
    fake_clients.subscription.records = {"SUB-001": _active_sub_with_discount()}
    fake_clients.subscription.by_customer = {"CUST-042": ["SUB-001"]}
    fake_clients.catalog.customer_offers = {
        "offers": [
            {
                "offer_id": "OFF-1",
                "state": "issued",
                "offer_definition_id": "OD_PROMO_VIP",
                "promotion": {"promotion_id": "PROMO_VIP", "label": "20% off"},
            }
        ]
    }
    with patch("bss_self_serve.routes.landing.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            body = c.get("/").text
    assert "You have a" in body  # unnamed promo → falls back to the label phrasing
    assert "20% off" in body
    assert "applies automatically to your next order" in body


@pytest.mark.asyncio
async def test_dashboard_shows_promo_friendly_name(seed_db, fake_clients):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id
    fake_clients.subscription.records = {"SUB-001": _active_sub_with_discount()}
    fake_clients.subscription.by_customer = {"CUST-042": ["SUB-001"]}
    fake_clients.catalog.customer_offers = {
        "offers": [{
            "offer_id": "OFF-1", "state": "eligible", "offer_definition_id": "OD_VIP",
            "promotion": {"promotion_id": "PROMO_VIP", "name": "VIP Welcome", "label": "20% off"},
        }]
    }
    with patch("bss_self_serve.routes.landing.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            body = c.get("/").text
    assert "VIP Welcome" in body  # the friendly name, not just "20% off"
    assert "20% off" in body


@pytest.mark.asyncio
async def test_signup_form_shows_assigned_offer_preapplied(seed_db, fake_clients):
    # a linked customer with an assigned offer sees it pre-applied + a remove toggle
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id
    fake_clients.catalog.assigned_offer = {
        "valid": True, "label": "20% off", "base": "25.00", "effective": "20.00",
    }
    with patch("bss_self_serve.routes.signup.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            body = c.get("/signup/PLAN_M", params={"msisdn": "91234567"}).text
    assert "applied" in body  # unnamed → "20% off applied"
    assert "20% off" in body
    assert 'name="apply_offer"' in body  # the remove/keep toggle
    assert 'name="offer_shown"' in body  # the hidden marker for opt-out detection
    assert "checked" in body  # pre-applied by default
    assert "Use a different code instead" in body  # replace-framed, not stack


@pytest.mark.asyncio
async def test_signup_form_no_offer_no_block(seed_db, fake_clients):
    # a new customer (no linked id) sees no offer block
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="new@example.sg", customer_id=None, verified=True
        )
        await db.commit()
        sid = sess.id
    with patch("bss_self_serve.routes.signup.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            body = c.get("/signup/PLAN_M", params={"msisdn": "91234567"}).text
    assert 'name="offer_shown"' not in body


@pytest.mark.asyncio
async def test_promo_preview_valid_renders_discounted_price(seed_db, fake_clients):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id=None, verified=True
        )
        await db.commit()
        sid = sess.id
    fake_clients.catalog.promo_preview = {
        "valid": True, "label": "20% off", "base": "25.00",
        "effective": "20.00", "reason": None,
    }
    with patch("bss_self_serve.routes.signup.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/signup/promo/preview", params={"code": "SUMMER", "offering": "PLAN_M"})
    assert resp.status_code == 200
    assert "20% off" in resp.text
    assert "SGD 20.00/mo" in resp.text


@pytest.mark.asyncio
async def test_promo_preview_invalid_renders_inline_note(seed_db, fake_clients):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id=None, verified=True
        )
        await db.commit()
        sid = sess.id
    fake_clients.catalog.promo_preview = {"valid": False, "reason": "expired"}
    with patch("bss_self_serve.routes.signup.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/signup/promo/preview", params={"code": "OLD", "offering": "PLAN_M"})
    assert resp.status_code == 200
    assert "expired" in resp.text.lower()
    assert "full price" in resp.text.lower()


@pytest.mark.asyncio
async def test_promo_preview_valid_with_offer_says_replaces(seed_db, fake_clients):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id
    fake_clients.catalog.promo_preview = {
        "valid": True, "label": "30% off", "base": "25.00", "effective": "17.50",
    }
    with patch("bss_self_serve.routes.signup.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/signup/promo/preview",
                         params={"code": "SUMMER", "offering": "PLAN_M", "has_offer": "1"})
    assert resp.status_code == 200
    assert "replaces your auto-applied offer" in resp.text.lower()


@pytest.mark.asyncio
async def test_promo_preview_invalid_with_offer_says_offer_stays(seed_db, fake_clients):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id
    fake_clients.catalog.promo_preview = {"valid": False, "reason": "expired"}
    with patch("bss_self_serve.routes.signup.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/signup/promo/preview",
                         params={"code": "OLD", "offering": "PLAN_M", "has_offer": "1"})
    assert resp.status_code == 200
    txt = resp.text.lower()
    assert "your auto-applied offer still applies" in txt
    assert "full price" not in txt  # don't threaten full price when an offer holds


@pytest.mark.asyncio
async def test_promo_preview_empty_code_renders_nothing(seed_db, fake_clients):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id=None, verified=True
        )
        await db.commit()
        sid = sess.id
    with patch("bss_self_serve.routes.signup.get_clients", return_value=fake_clients):
        with TestClient(create_app(Settings())) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/signup/promo/preview", params={"code": "  ", "offering": "PLAN_M"})
    assert resp.status_code == 200
    assert resp.text.strip() == ""
