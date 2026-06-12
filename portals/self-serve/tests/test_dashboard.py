"""Login-gated `/` — the v0.10 dashboard with state-aware line cards.

V0_10_0.md Track 2:

* No session                          -> 303 to /auth/login.
* Verified email, no customer_id      -> empty dashboard ("Browse plans").
* Linked customer, zero lines         -> empty dashboard ("any lines yet").
* Linked customer, ≥1 line            -> one line_card per subscription
                                         with state-aware CTAs and
                                         proportional balance bars.
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

from datetime import datetime  # noqa: E402

from bss_clock.clock import freeze as _freeze_clock  # noqa: E402
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
            "TRUNCATE portal_auth.login_attempt, portal_auth.session, "
            "portal_auth.login_token, portal_auth.identity RESTART IDENTITY CASCADE"
        ))
        await s.commit()
    yield factory
    async with factory() as s:
        await s.execute(text(
            "TRUNCATE portal_auth.login_attempt, portal_auth.session, "
            "portal_auth.login_token, portal_auth.identity RESTART IDENTITY CASCADE"
        ))
        await s.commit()
    await engine.dispose()


def _build_sub(
    sub_id: str,
    *,
    customer_id: str,
    state: str,
    offering_id: str = "PLAN_M",
    msisdn: str = "91234567",
    period_end: str | None = "2026-05-27T00:00:00+00:00",
    pending_offering_id: str | None = None,
    pending_effective_at: str | None = None,
    terminated_at: str | None = None,
) -> dict:
    """Mirror the camelCase shape of the BSS subscription response."""
    return {
        "id": sub_id,
        "href": f"/subscription-api/v1/subscription/{sub_id}",
        "customerId": customer_id,
        "offeringId": offering_id,
        "msisdn": msisdn,
        "iccid": "8910010000000000123",
        "state": state,
        "stateReason": None,
        "currentPeriodStart": "2026-04-27T00:00:00+00:00" if period_end else None,
        "currentPeriodEnd": period_end,
        "nextRenewalAt": period_end,
        "terminatedAt": terminated_at,
        "balances": [],
        "pendingOfferingId": pending_offering_id,
        "pendingEffectiveAt": pending_effective_at,
    }


def _balance(allowance_type: str, total: int, consumed: int, unit: str = "mb") -> dict:
    return {
        "id": f"BAL-{allowance_type}",
        "subscriptionId": "SUB-001",
        "allowanceType": allowance_type,
        "total": total,
        "consumed": consumed,
        "remaining": total - consumed if total >= 0 else -1,
        "unit": unit,
        "periodStart": "2026-04-27T00:00:00+00:00",
        "periodEnd": "2026-05-27T00:00:00+00:00",
    }


# ── No session ──────────────────────────────────────────────────────────


def test_root_redirects_to_login_when_no_session(seed_db, fake_clients):
    """No cookie → bounce to /auth/login with `next=/`."""
    with patch(
        "bss_self_serve.routes.landing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            resp = c.get("/", follow_redirects=False)
            assert resp.status_code == 303
            loc = resp.headers["location"]
            assert loc.startswith("/auth/login")
            assert "next=" in loc


# ── Verified-but-unlinked identity ──────────────────────────────────────


@pytest.mark.asyncio
async def test_root_renders_empty_dashboard_for_unlinked_identity(seed_db, fake_clients):
    async with seed_db() as db:
        sess, identity = await create_test_session(
            db, email="ada@example.sg", customer_id=None, verified=True
        )
        await db.commit()
        sid = sess.id
        assert identity.customer_id is None

    with patch(
        "bss_self_serve.routes.landing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/")
            assert resp.status_code == 200
            body = resp.text
            assert "ada@example.sg" in body
            assert "any lines yet" in body
            assert "/plans" in body


# ── Linked customer, zero lines ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_renders_empty_dashboard_for_linked_customer_with_no_lines(
    seed_db, fake_clients
):
    """A linked customer with no subscriptions still sees the empty state.

    The empty state's CTA into /plans makes the second-line / re-signup
    flow continuous; the dashboard does not invent a "you used to have
    a line" placeholder.
    """
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id

    fake_clients.subscription.records = {}
    fake_clients.subscription.by_customer = {"CUST-042": []}

    with patch(
        "bss_self_serve.routes.landing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/")
            assert resp.status_code == 200
            assert "any lines yet" in resp.text


# ── Linked customer, one active line ────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_renders_active_line_card_with_balance_bars(
    seed_db, fake_clients
):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id

    sub = _build_sub("SUB-001", customer_id="CUST-042", state="active")
    fake_clients.subscription.records = {"SUB-001": sub}
    fake_clients.subscription.by_customer = {"CUST-042": ["SUB-001"]}
    fake_clients.subscription.balances = {
        "SUB-001": [
            _balance("data", total=20480, consumed=3000, unit="mb"),  # ~85%
            _balance("voice", total=300, consumed=270, unit="min"),  # 10% (low)
            _balance("sms", total=-1, consumed=0, unit="sms"),  # unlimited
        ],
    }

    _freeze_clock(datetime.fromisoformat("2026-04-27T00:00:00+00:00"))

    with patch(
        "bss_self_serve.routes.landing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/")
            assert resp.status_code == 200
            body = resp.text

            # Line card identifiers.
            assert 'data-subscription-id="SUB-001"' in body
            assert "91234567" in body
            assert "Mainline" in body  # offering name from SAMPLE_OFFERINGS

            # Active CTAs.
            assert 'href="/top-up?subscription=SUB-001"' in body
            assert 'href="/plan/change?subscription=SUB-001"' in body
            assert 'href="/esim/SUB-001"' in body
            assert 'href="/subscription/SUB-001/cancel"' in body

            # Proportional bar — data ~85%, voice 10% (low), sms unlimited.
            assert "width: 85%" in body
            assert "width: 10%" in body
            assert "line-card-bar--low" in body
            assert "line-card-bar--unlimited" in body
            # Days remaining (period_end - now = 30 days).
            assert "Renews in 30 days" in body


# ── Linked customer, blocked line ───────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_renders_blocked_line_with_unblock_cta(
    seed_db, fake_clients
):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id

    sub = _build_sub("SUB-002", customer_id="CUST-042", state="blocked")
    fake_clients.subscription.records = {"SUB-002": sub}
    fake_clients.subscription.by_customer = {"CUST-042": ["SUB-002"]}
    fake_clients.subscription.balances = {
        "SUB-002": [_balance("data", total=20480, consumed=20480, unit="mb")],
    }

    with patch(
        "bss_self_serve.routes.landing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/")
            assert resp.status_code == 200
            body = resp.text

            assert "line-card--blocked" in body
            # Top-up-to-unblock is the prominent primary CTA.
            assert (
                'href="/top-up?subscription=SUB-002&amp;context=blocked"' in body
                or 'href="/top-up?subscription=SUB-002&context=blocked"' in body
            )
            assert "Top up to unblock" in body
            # Bar is exhausted (0%) — confirms proportional rendering.
            assert "line-card-bar--exhausted" in body


# ── Linked customer, pending plan change ────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_renders_pending_plan_change_banner(
    seed_db, fake_clients
):
    """Customer scheduled a switch — banner shows on every dashboard view.

    V0_10_0.md "Do not let plan change forget pending state": the
    pending banner must surface every visit, not just on the
    confirmation page. This test asserts it.
    """
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id

    sub = _build_sub(
        "SUB-003",
        customer_id="CUST-042",
        state="active",
        offering_id="PLAN_M",
        pending_offering_id="PLAN_L",
        pending_effective_at="2026-05-27T00:00:00+00:00",
    )
    fake_clients.subscription.records = {"SUB-003": sub}
    fake_clients.subscription.by_customer = {"CUST-042": ["SUB-003"]}
    fake_clients.subscription.balances = {"SUB-003": []}

    with patch(
        "bss_self_serve.routes.landing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/")
            assert resp.status_code == 200
            body = resp.text

            assert "line-card-pending-banner" in body
            # Pending offering's *name* (resolved through catalog), not just id.
            assert "Long Haul" in body
            # Effective date rendered (date portion only — keeps the
            # template free of jinja datetime gymnastics).
            assert "2026-05-27" in body
            # Cancel-pending form posts to the plan-change cancel route.
            assert 'action="/plan/change/cancel"' in body


# ── Linked customer, terminated line ────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_renders_terminated_line_with_cancelled_badge(
    seed_db, fake_clients
):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@example.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sid = sess.id

    sub = _build_sub(
        "SUB-004",
        customer_id="CUST-042",
        state="terminated",
        period_end=None,
        terminated_at="2026-04-20T12:00:00+00:00",
    )
    fake_clients.subscription.records = {"SUB-004": sub}
    fake_clients.subscription.by_customer = {"CUST-042": ["SUB-004"]}
    fake_clients.subscription.balances = {"SUB-004": []}

    with patch(
        "bss_self_serve.routes.landing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/")
            assert resp.status_code == 200
            body = resp.text

            assert "line-card--terminated" in body
            assert "Cancelled on 2026-04-20" in body
            # No actionable CTAs on a terminated line.
            assert 'href="/top-up?subscription=SUB-004"' not in body
            assert 'href="/subscription/SUB-004/cancel"' not in body


# ── Public surfaces still untouched ─────────────────────────────────────


def test_welcome_still_public_after_dashboard_lands(seed_db, fake_clients):
    """Sanity: v0.10's `/` does not gate /welcome or /plans."""
    with patch(
        "bss_self_serve.routes.welcome.get_clients", return_value=fake_clients
    ), patch(
        "bss_self_serve.routes.landing.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            assert c.get("/welcome").status_code == 200
            assert c.get("/plans").status_code == 200
