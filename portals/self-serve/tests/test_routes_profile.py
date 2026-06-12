"""v0.10 PR 8 — contact medium update + email-change two-step.

V0_10_0.md Track 7 + Track 10. The load-bearing test in this file is
``test_email_change_verify_rolls_back_on_partial_failure`` — it
plants a synthetic failure between the CRM update and the
``portal_auth.identity.email`` update and asserts that NEITHER
side has been updated. Without this guarantee, the doctrine
"atomic" claim is just words.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "BSS_PORTAL_TOKEN_PEPPER",
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
)
os.environ.setdefault("BSS_PORTAL_EMAIL_ADAPTER", "noop")
os.environ.setdefault("BSS_PORTAL_EMAIL_PROVIDER", "noop")  # v0.14 — both names handled
os.environ.setdefault("BSS_PORTAL_DEV_INSECURE_COOKIE", "1")

from bss_clock.clock import reset_for_tests as _reset_clock  # noqa: E402
from bss_models import (  # noqa: E402
    ContactMedium,
    Customer,
    EmailChangePending,
    Identity,
    Party,
    PortalAction,
)
from bss_portal_auth.test_helpers import (  # noqa: E402
    create_test_session,
    mint_step_up_grant,
)
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
            "TRUNCATE portal_auth.email_change_pending, "
            "portal_auth.portal_action, portal_auth.login_attempt, "
            "portal_auth.session, portal_auth.login_token, "
            "portal_auth.identity RESTART IDENTITY CASCADE"
        ))
        # Clean any test-leftover CRM rows.
        await s.execute(text(
            "DELETE FROM crm.contact_medium WHERE party_id LIKE 'PRT-T-%'"
        ))
        await s.execute(text(
            "DELETE FROM crm.customer WHERE id LIKE 'CUST-T-%'"
        ))
        await s.execute(text(
            "DELETE FROM crm.individual WHERE party_id LIKE 'PRT-T-%'"
        ))
        await s.execute(text(
            "DELETE FROM crm.party WHERE id LIKE 'PRT-T-%'"
        ))

    async with factory() as s:
        await _scrub(s)
        await s.commit()
    yield factory
    async with factory() as s:
        await _scrub(s)
        await s.commit()
    await engine.dispose()


async def _seed_customer_with_email(
    seed_db,
    *,
    customer_id: str,
    party_id: str,
    email: str,
):
    """Seed a CRM customer + party + active email contact_medium row.

    The email-change cross-schema test needs a real CRM row to update;
    the fakes-only approach can't exercise the atomicity invariant.
    """

    async with seed_db() as s:
        s.add(Party(id=party_id, party_type="individual"))
        await s.flush()
        s.add(
            Customer(
                id=customer_id,
                party_id=party_id,
                status="active",
                kyc_status="verified",
                customer_since=__import__("datetime").datetime(
                    2026, 1, 1, tzinfo=__import__("datetime").timezone.utc
                ),
            )
        )
        s.add(
            ContactMedium(
                id=f"CM-T-{customer_id[-6:]}-EMAIL",
                party_id=party_id,
                medium_type="email",
                value=email,
                is_primary=True,
                valid_from=__import__("datetime").datetime(
                    2026, 1, 1, tzinfo=__import__("datetime").timezone.utc
                ),
            )
        )
        await s.commit()


async def _setup(seed_db, *, customer_id="CUST-T-A1B2C3", email="ada@example.sg",
                 with_grant_for=None, seed_crm=False):
    if seed_crm:
        await _seed_customer_with_email(
            seed_db,
            customer_id=customer_id,
            party_id=f"PRT-T-{customer_id[-6:]}",
            email=email,
        )

    async with seed_db() as db:
        sess, identity = await create_test_session(
            db, email=email, customer_id=customer_id, verified=True
        )
        await db.commit()
        sid, iid = sess.id, identity.id

    grant = None
    if with_grant_for is not None:
        async with seed_db() as db:
            grant = await mint_step_up_grant(
                db, session_id=sid, action_label=with_grant_for
            )
            await db.commit()
    return sid, iid, grant


async def _portal_actions(seed_db) -> list[PortalAction]:
    async with seed_db() as s:
        rows = (await s.execute(select(PortalAction))).scalars().all()
        return list(rows)


def _seed_phone(fake_clients, *, customer_id, cm_id="CM-PHONE", value="+6590001111"):
    fake_clients.crm.mediums_by_customer.setdefault(customer_id, []).append({
        "id": cm_id,
        "mediumType": "mobile",
        "value": value,
        "isPrimary": False,
    })


def _seed_email(fake_clients, *, customer_id, cm_id="CM-EMAIL", value="ada@example.sg"):
    fake_clients.crm.mediums_by_customer.setdefault(customer_id, []).append({
        "id": cm_id,
        "mediumType": "email",
        "value": value,
        "isPrimary": True,
    })


# ── GET /profile/contact ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_lists_owned_mediums(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_email(fake_clients, customer_id="CUST-T-A1B2C3")
    _seed_phone(fake_clients, customer_id="CUST-T-A1B2C3")

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/profile/contact")
            assert resp.status_code == 200
            body = resp.text
            assert "ada@example.sg" in body
            assert "+6590001111" in body
            # The email-change form is rendered (no pending change yet).
            assert 'action="/profile/contact/email/change"' in body


# ── Phone update ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phone_update_requires_step_up(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_phone(fake_clients, customer_id="CUST-T-A1B2C3")

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/profile/contact/phone/update",
                data={"cm_id": "CM-PHONE", "value": "+6599998888"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/auth/step-up" in resp.headers["location"]
            assert fake_clients.crm.update_calls == []


@pytest.mark.asyncio
async def test_phone_update_succeeds_and_audits(seed_db, fake_clients):
    sid, _, grant = await _setup(seed_db, with_grant_for="phone_update")
    _seed_phone(fake_clients, customer_id="CUST-T-A1B2C3")

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/profile/contact/phone/update",
                data={"cm_id": "CM-PHONE", "value": "+6599998888"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/profile/contact?flash=phone_update"

    assert fake_clients.crm.update_calls == [
        ("CUST-T-A1B2C3", "CM-PHONE", "+6599998888")
    ]
    rows = await _portal_actions(seed_db)
    assert rows[0].action == "phone_update"
    assert rows[0].success is True


@pytest.mark.asyncio
async def test_phone_update_targeting_email_medium_returns_403(seed_db, fake_clients):
    """Trying to update an email row through /phone/update bounces out — the
    expected_type guard catches it before any BSS write."""
    sid, _, grant = await _setup(seed_db, with_grant_for="phone_update")
    _seed_email(fake_clients, customer_id="CUST-T-A1B2C3", cm_id="CM-EMAIL")

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/profile/contact/phone/update",
                data={"cm_id": "CM-EMAIL", "value": "+6599998888"},
            )
            assert resp.status_code == 403

    assert fake_clients.crm.update_calls == []
    rows = await _portal_actions(seed_db)
    assert rows[0].success is False


@pytest.mark.asyncio
async def test_phone_update_cross_customer_returns_403_and_audits(
    seed_db, fake_clients
):
    """Bob can't update Ada's phone."""
    sid, _, grant = await _setup(
        seed_db, customer_id="CUST-T-BOB001", with_grant_for="phone_update"
    )
    _seed_phone(fake_clients, customer_id="CUST-T-ADA001")

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/profile/contact/phone/update",
                data={"cm_id": "CM-PHONE", "value": "+6599998888"},
            )
            assert resp.status_code == 403

    assert fake_clients.crm.update_calls == []
    rows = await _portal_actions(seed_db)
    assert rows[0].success is False


# ── Email change: start ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_change_start_creates_pending_and_audits(seed_db, fake_clients):
    sid, iid, grant = await _setup(
        seed_db, with_grant_for="email_change", seed_crm=True
    )

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/profile/contact/email/change",
                data={"new_email": "ada-new@example.sg"},
            )
            assert resp.status_code == 200
            assert "Check your new inbox" in resp.text
            assert "ada-new@example.sg" in resp.text

    # A pending row exists; CRM email is unchanged.
    async with seed_db() as s:
        pending = (
            await s.execute(
                select(EmailChangePending).where(
                    EmailChangePending.identity_id == iid,
                    EmailChangePending.status == "pending",
                )
            )
        ).scalar_one()
        assert pending.new_email == "ada-new@example.sg"

        cm_email = (
            await s.execute(
                select(ContactMedium).where(
                    ContactMedium.medium_type == "email",
                    ContactMedium.valid_to.is_(None),
                    ContactMedium.party_id.like("PRT-T-%"),
                )
            )
        ).scalar_one()
        assert cm_email.value == "ada@example.sg"  # NOT yet updated

        identity = (
            await s.execute(select(Identity).where(Identity.id == iid))
        ).scalar_one()
        assert identity.email == "ada@example.sg"  # NOT yet updated

    rows = await _portal_actions(seed_db)
    assert rows[0].action == "email_change"
    assert rows[0].route == "/profile/contact/email/change"
    assert rows[0].success is True


# ── Email change: verify happy path — atomic across schemas ──────────────


@pytest.mark.asyncio
async def test_email_change_verify_commits_atomically_across_schemas(
    seed_db, fake_clients
):
    """Single Postgres transaction covers crm.contact_medium AND
    portal_auth.identity.email. After verify, both have flipped."""
    sid, iid, grant = await _setup(
        seed_db, with_grant_for="email_change", seed_crm=True
    )

    # Drive start → grab the OTP from the noop adapter records →
    # POST verify with the OTP.
    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            r = c.post(
                "/profile/contact/email/change",
                data={"new_email": "ada-new@example.sg"},
            )
            assert r.status_code == 200

            # The noop email adapter recorded the OTP keyed by
            # (email, "step_up:email_change"); pull it out.
            adapter = c.app.state.email_adapter
            rec = adapter.records[("ada-new@example.sg", "step_up:email_change")]
            otp = rec["otp"]

            r = c.post(
                "/profile/contact/email/verify",
                data={"code": otp},
                follow_redirects=False,
            )
            assert r.status_code == 303, r.text
            assert r.headers["location"] == (
                "/profile/contact?flash=email_change"
            )

    # Both rows flipped.
    async with seed_db() as s:
        identity = (
            await s.execute(select(Identity).where(Identity.id == iid))
        ).scalar_one()
        assert identity.email == "ada-new@example.sg"

        cm_email = (
            await s.execute(
                select(ContactMedium).where(
                    ContactMedium.medium_type == "email",
                    ContactMedium.valid_to.is_(None),
                    ContactMedium.party_id.like("PRT-T-%"),
                )
            )
        ).scalar_one()
        assert cm_email.value == "ada-new@example.sg"

        pending = (
            await s.execute(
                select(EmailChangePending).where(
                    EmailChangePending.identity_id == iid,
                )
            )
        ).scalar_one()
        assert pending.status == "consumed"
        assert pending.consumed_at is not None


# ── Email change: verify wrong code ──────────────────────────────────────


@pytest.mark.asyncio
async def test_email_change_verify_rejects_wrong_code(seed_db, fake_clients):
    sid, iid, grant = await _setup(
        seed_db, with_grant_for="email_change", seed_crm=True
    )

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            c.post(
                "/profile/contact/email/change",
                data={"new_email": "ada-new@example.sg"},
            )
            r = c.post(
                "/profile/contact/email/verify",
                data={"code": "000000"},  # wrong
            )
            assert r.status_code == 400
            assert "code doesn" in r.text.lower()

    # No row flipped.
    async with seed_db() as s:
        cm = (
            await s.execute(
                select(ContactMedium).where(
                    ContactMedium.medium_type == "email",
                    ContactMedium.valid_to.is_(None),
                    ContactMedium.party_id.like("PRT-T-%"),
                )
            )
        ).scalar_one()
        assert cm.value == "ada@example.sg"
        identity = (
            await s.execute(select(Identity).where(Identity.id == iid))
        ).scalar_one()
        assert identity.email == "ada@example.sg"


# ── ★ The atomicity rollback test ★ ──────────────────────────────────────


@pytest.mark.asyncio
async def test_email_change_verify_rolls_back_on_partial_failure(
    seed_db, fake_clients, monkeypatch
):
    """Plant a synthetic failure between the CRM update and the
    portal_auth.identity update. Assert NEITHER row was flipped.

    This is the load-bearing test for the v0.10 PR 8 atomicity
    claim. If it doesn't pass, the cross-schema "atomic" doctrine
    is just words. The trap V0_10_0.md calls out is exactly the
    half-committed state where CRM has the new email but
    portal_auth.identity.email still has the old one.

    Synthetic failure shape: monkeypatch ``ContactMedium.value`` to
    raise on assignment partway. We do that by patching the
    bss_portal_auth.email_change.verify_email_change function to
    insert an explicit raise after the CRM update_flush but before
    the identity update — using a probe wrapper.
    """
    sid, iid, grant = await _setup(
        seed_db, with_grant_for="email_change", seed_crm=True
    )

    # Drive start to get an OTP, then patch the cross-schema function
    # to raise between the two updates.

    async def _verify_with_synthetic_failure(db, *, identity_id, code):
        """Mirror real_verify up to and including the CRM update,
        then raise. The session rollback should leave nothing committed."""
        from bss_models import (
            ContactMedium as _CM,
        )
        from bss_models import (
            Customer as _C,
        )
        from bss_models import (
            EmailChangePending as _ECP,
        )
        from bss_models import (
            Identity as _I,
        )
        from bss_portal_auth.tokens import verify_token as _verify_token
        from sqlalchemy import select as _select

        pending = (
            await db.execute(
                _select(_ECP).where(
                    _ECP.identity_id == identity_id,
                    _ECP.status == "pending",
                )
            )
        ).scalar_one_or_none()
        assert pending is not None and _verify_token(code.strip(), pending.code_hash)

        identity = (
            await db.execute(_select(_I).where(_I.id == identity_id))
        ).scalar_one()
        customer = (
            await db.execute(_select(_C).where(_C.id == identity.customer_id))
        ).scalar_one()
        cm = (
            await db.execute(
                _select(_CM).where(
                    _CM.party_id == customer.party_id,
                    _CM.medium_type == "email",
                    _CM.valid_to.is_(None),
                )
            )
        ).scalar_one()

        # Apply CRM update, then explode BEFORE updating
        # portal_auth.identity.email or marking pending consumed.
        cm.value = pending.new_email
        await db.flush()
        raise RuntimeError("synthetic mid-transaction failure")

    monkeypatch.setattr(
        "bss_self_serve.routes.profile.verify_email_change",
        _verify_with_synthetic_failure,
    )

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            c.post(
                "/profile/contact/email/change",
                data={"new_email": "ada-new@example.sg"},
            )
            adapter = c.app.state.email_adapter
            otp = adapter.records[
                ("ada-new@example.sg", "step_up:email_change")
            ]["otp"]

            r = c.post(
                "/profile/contact/email/verify",
                data={"code": otp},
            )
            # The route catches the synthetic failure and renders 500.
            assert r.status_code == 500

    # ★ The rollback assertion ★ — neither row is updated.
    async with seed_db() as s:
        cm = (
            await s.execute(
                select(ContactMedium).where(
                    ContactMedium.medium_type == "email",
                    ContactMedium.valid_to.is_(None),
                    ContactMedium.party_id.like("PRT-T-%"),
                )
            )
        ).scalar_one()
        assert cm.value == "ada@example.sg", (
            "CRM email rolled back — must be the original value"
        )

        identity = (
            await s.execute(select(Identity).where(Identity.id == iid))
        ).scalar_one()
        assert identity.email == "ada@example.sg", (
            "portal_auth identity rolled back — must be the original value"
        )

        pending = (
            await s.execute(
                select(EmailChangePending).where(
                    EmailChangePending.identity_id == iid,
                )
            )
        ).scalar_one()
        assert pending.status == "pending", (
            "Pending row rolled back — should still be pending so the "
            "customer can retry"
        )
        assert pending.consumed_at is None

    # Failure was audited (on a separate session, so it survived rollback).
    rows = await _portal_actions(seed_db)
    failures = [r for r in rows if not r.success]
    assert any(r.action == "email_change" for r in failures)


# ── Name update (Party.individual.given_name / family_name) ─────────────


@pytest.mark.asyncio
async def test_get_renders_name_field_above_contact_mediums(seed_db, fake_clients):
    """Name lives on the customer record, not on a contact medium —
    the page must surface it as a separate section."""
    sid, _, _ = await _setup(seed_db)
    fake_clients.crm.individual_by_customer["CUST-T-A1B2C3"] = {
        "given_name": "Ada",
        "family_name": "Lovelace",
    }
    _seed_email(fake_clients, customer_id="CUST-T-A1B2C3")

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/profile/contact")
            assert resp.status_code == 200
            body = resp.text
            assert "Ada" in body
            assert "Lovelace" in body
            # Name-update form is the maintenance area for the field.
            assert 'action="/profile/contact/name/update"' in body
            # Copy explicitly tells the customer they won't re-enter at signup.
            assert "won&#39;t be asked to re-enter" in body or \
                   "won't be asked to re-enter" in body


@pytest.mark.asyncio
async def test_name_update_requires_step_up(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    fake_clients.crm.individual_by_customer["CUST-T-A1B2C3"] = {
        "given_name": "Ada", "family_name": "Lovelace",
    }

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/profile/contact/name/update",
                data={"given_name": "Augusta", "family_name": "Byron"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/auth/step-up" in resp.headers["location"]
            assert fake_clients.crm.individual_update_calls == []


@pytest.mark.asyncio
async def test_name_update_step_up_bounces_back_to_form_page(seed_db, fake_clients):
    """Regression: a POST that needs step-up must bounce the customer back
    to the form page (Referer), not to the POST URL itself. Step-up
    completes with a 303, which forces GET — and the contact-medium
    update routes are POST-only, so a GET bounce-back yielded 405.
    """
    sid, _, _ = await _setup(seed_db)
    fake_clients.crm.individual_by_customer["CUST-T-A1B2C3"] = {
        "given_name": "Ada", "family_name": "Lovelace",
    }

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/profile/contact/name/update",
                data={"given_name": "Augusta", "family_name": "Byron"},
                headers={"referer": "http://testserver/profile/contact"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            loc = resp.headers["location"]
            assert loc.startswith("/auth/step-up")
            # The next= param must be the form page (GET-able), not the
            # POST URL — otherwise the step-up bounce-back 405s.
            assert "next=%2Fprofile%2Fcontact" in loc
            assert "next=%2Fprofile%2Fcontact%2Fname%2Fupdate" not in loc


@pytest.mark.asyncio
async def test_name_update_bounce_stashes_form_body_for_replay(seed_db, fake_clients):
    """The customer's typed values must be stashed at StepUpRequired time
    so /auth/step-up can replay them. Without this, the bounce lands
    on the form page with the values lost and the customer types twice.
    """
    from bss_models import StepUpPendingAction

    sid, _, _ = await _setup(seed_db)
    fake_clients.crm.individual_by_customer["CUST-T-A1B2C3"] = {
        "given_name": "Ada", "family_name": "Lovelace",
    }

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/profile/contact/name/update",
                data={"given_name": "Augusta", "family_name": "Byron"},
                follow_redirects=False,
            )
            assert resp.status_code == 303

    async with seed_db() as s:
        rows = (
            await s.execute(
                select(StepUpPendingAction).where(
                    StepUpPendingAction.session_id == sid,
                    StepUpPendingAction.action_label == "name_update",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.target_url == "/profile/contact/name/update"
        assert row.payload_json == {
            "given_name": "Augusta",
            "family_name": "Byron",
        }
        assert row.consumed_at is None


@pytest.mark.asyncio
async def test_name_update_step_up_ignores_external_referer(seed_db, fake_clients):
    """A Referer pointing at a different origin must not influence the
    bounce-back target. We fall back to the current URL (the original
    behaviour), which keeps Referer from being a redirect-target oracle.
    """
    sid, _, _ = await _setup(seed_db)
    fake_clients.crm.individual_by_customer["CUST-T-A1B2C3"] = {
        "given_name": "Ada", "family_name": "Lovelace",
    }

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/profile/contact/name/update",
                data={"given_name": "Augusta", "family_name": "Byron"},
                headers={"referer": "https://evil.example/phish"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            loc = resp.headers["location"]
            assert "evil.example" not in loc
            assert "next=%2Fprofile%2Fcontact%2Fname%2Fupdate" in loc


@pytest.mark.asyncio
async def test_name_update_succeeds_and_audits(seed_db, fake_clients):
    sid, _, grant = await _setup(seed_db, with_grant_for="name_update")
    fake_clients.crm.individual_by_customer["CUST-T-A1B2C3"] = {
        "given_name": "Ada", "family_name": "Lovelace",
    }

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/profile/contact/name/update",
                data={"given_name": "Augusta", "family_name": "King"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == (
                "/profile/contact?flash=name_update"
            )

    assert fake_clients.crm.individual_update_calls == [
        ("CUST-T-A1B2C3", "Augusta", "King")
    ]
    rows = await _portal_actions(seed_db)
    assert rows[0].action == "name_update"
    assert rows[0].success is True


# ── Email change: cancel pending ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_change_cancel_clears_pending(seed_db, fake_clients):
    sid, iid, grant = await _setup(
        seed_db, with_grant_for="email_change", seed_crm=True
    )

    with patch(
        "bss_self_serve.routes.profile.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            c.post(
                "/profile/contact/email/change",
                data={"new_email": "ada-new@example.sg"},
            )
            r = c.post(
                "/profile/contact/email/cancel",
                follow_redirects=False,
            )
            assert r.status_code == 303

    async with seed_db() as s:
        rows = (
            await s.execute(
                select(EmailChangePending).where(
                    EmailChangePending.identity_id == iid,
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "cancelled"
