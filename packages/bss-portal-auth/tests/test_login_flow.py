"""End-to-end-ish unit tests for the email login flow.

Drives the public service surface against a real Postgres + the
``portal_auth`` schema. No HTTP, no portal app — just the flow.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from bss_clock import advance, freeze
from bss_models import Identity, LoginAttempt, LoginToken
from bss_portal_auth import (
    LoginChallenge,
    LoginFailed,
    SessionView,
    start_email_login,
    verify_email_login,
)
from sqlalchemy import select


@pytest.mark.asyncio
async def test_start_email_login_creates_identity_and_two_tokens(db_session, email_adapter):
    challenge = await start_email_login(
        db_session, email="ada@example.sg", ip="127.0.0.1", email_adapter=email_adapter
    )
    await db_session.commit()

    assert isinstance(challenge, LoginChallenge)
    assert challenge.identity_id.startswith("IDN-")

    identity = (
        await db_session.execute(select(Identity).where(Identity.email == "ada@example.sg"))
    ).scalar_one()
    assert identity.status == "unverified"
    assert identity.email_verified_at is None

    tokens = (
        await db_session.execute(
            select(LoginToken).where(LoginToken.identity_id == identity.id)
        )
    ).scalars().all()
    kinds = sorted(t.kind for t in tokens)
    assert kinds == ["magic_link", "otp"]
    # Tokens stored hashed — never plaintext.
    for t in tokens:
        assert len(t.code_hash) == 64  # hex sha256
        assert all(c in "0123456789abcdef" for c in t.code_hash)


@pytest.mark.asyncio
async def test_start_email_login_handed_codes_to_adapter(db_session, email_adapter):
    await start_email_login(
        db_session, email="ada@example.sg", email_adapter=email_adapter
    )
    rec = email_adapter.records[("ada@example.sg", "login")]
    assert len(rec["otp"]) == 6 and rec["otp"].isdigit()
    # v0.14: magic_link is the bare token when BSS_PORTAL_PUBLIC_URL is
    # unset (NoopEmailAdapter test default), or a full URL otherwise.
    # Either way, the token portion is 32 chars.
    magic_link = rec["magic_link"]
    if magic_link.startswith("http"):
        # Full URL — extract the ``token=`` query param.
        from urllib.parse import parse_qs, urlparse
        token = parse_qs(urlparse(magic_link).query)["token"][0]
        assert len(token) == 32
    else:
        assert len(magic_link) == 32


@pytest.mark.asyncio
async def test_start_email_login_idempotent_on_known_email(db_session, email_adapter):
    a = await start_email_login(db_session, email="ada@x.sg", email_adapter=email_adapter)
    b = await start_email_login(db_session, email="ada@x.sg", email_adapter=email_adapter)
    assert a.identity_id == b.identity_id


@pytest.mark.asyncio
async def test_verify_with_correct_otp_mints_session_and_stamps_verified_at(
    db_session, email_adapter
):
    await start_email_login(
        db_session, email="ada@x.sg", ip="1.2.3.4", email_adapter=email_adapter
    )
    otp = email_adapter.records[("ada@x.sg", "login")]["otp"]

    result = await verify_email_login(
        db_session, email="ada@x.sg", code=otp, ip="1.2.3.4"
    )
    assert isinstance(result, SessionView)
    assert result.identity_id.startswith("IDN-")

    identity = (
        await db_session.execute(select(Identity).where(Identity.email == "ada@x.sg"))
    ).scalar_one()
    assert identity.email_verified_at is not None
    assert identity.last_login_at is not None


@pytest.mark.asyncio
async def test_verify_with_correct_magic_link_also_works(db_session, email_adapter):
    await start_email_login(db_session, email="ada@x.sg", email_adapter=email_adapter)
    ml = email_adapter.records[("ada@x.sg", "login")]["magic_link"]

    result = await verify_email_login(db_session, email="ada@x.sg", code=ml)
    assert isinstance(result, SessionView)


@pytest.mark.asyncio
async def test_verify_wrong_code_returns_failure_and_audits(db_session, email_adapter):
    await start_email_login(db_session, email="ada@x.sg", email_adapter=email_adapter)
    result = await verify_email_login(db_session, email="ada@x.sg", code="000000")
    assert isinstance(result, LoginFailed)
    assert result.reason == "wrong_code"

    rows = (
        await db_session.execute(
            select(LoginAttempt).where(LoginAttempt.stage == "login_verify")
        )
    ).scalars().all()
    assert any(r.outcome == "wrong_code" for r in rows)


@pytest.mark.asyncio
async def test_verify_consumed_code_cannot_be_reused(db_session, email_adapter):
    await start_email_login(db_session, email="ada@x.sg", email_adapter=email_adapter)
    otp = email_adapter.records[("ada@x.sg", "login")]["otp"]
    first = await verify_email_login(db_session, email="ada@x.sg", code=otp)
    assert isinstance(first, SessionView)

    second = await verify_email_login(db_session, email="ada@x.sg", code=otp)
    assert isinstance(second, LoginFailed)
    # The matched token is consumed; the other token (magic_link) is still
    # active and unconsumed, so this is wrong_code (no match), not no_active_token.
    assert second.reason == "wrong_code"


@pytest.mark.asyncio
async def test_verify_expired_code_returns_expired(db_session, email_adapter):
    freeze()  # snapshot a frozen instant; advance shifts it
    await start_email_login(db_session, email="ada@x.sg", email_adapter=email_adapter)
    otp = email_adapter.records[("ada@x.sg", "login")]["otp"]

    advance(timedelta(minutes=20))  # default TTL is 15 min
    result = await verify_email_login(db_session, email="ada@x.sg", code=otp)
    assert isinstance(result, LoginFailed)
    assert result.reason == "expired"


@pytest.mark.asyncio
async def test_verify_unknown_email_returns_no_such_identity(db_session):
    result = await verify_email_login(db_session, email="nobody@x.sg", code="123456")
    assert isinstance(result, LoginFailed)
    assert result.reason == "no_such_identity"


@pytest.mark.asyncio
async def test_verify_no_active_token_when_all_consumed(db_session, email_adapter):
    await start_email_login(db_session, email="ada@x.sg", email_adapter=email_adapter)
    otp = email_adapter.records[("ada@x.sg", "login")]["otp"]
    ml = email_adapter.records[("ada@x.sg", "login")]["magic_link"]

    # Consume both tokens.
    await verify_email_login(db_session, email="ada@x.sg", code=otp)
    # The magic_link is still active for one more shot, so consume it too.
    await verify_email_login(db_session, email="ada@x.sg", code=ml)

    # Now there is genuinely no active token left.
    result = await verify_email_login(db_session, email="ada@x.sg", code="anything")
    assert isinstance(result, LoginFailed)
    assert result.reason == "no_active_token"


@pytest.mark.asyncio
async def test_verify_auto_links_identity_to_existing_crm_customer(
    db_session, email_adapter
):
    """v0.10 fix — a returning customer whose CRM record predates the
    portal identity should land on a linked identity at first login,
    not on an empty-dashboard NULL-customer state.

    Pre-seed a Party + Individual + Customer + email contact_medium in
    crm, then run start_email_login + verify_email_login with that
    email. After verify, identity.customer_id should be set to the
    pre-existing CUST-* id.
    """
    from datetime import datetime, timezone

    from bss_models import ContactMedium, Customer, Identity, Individual, Party

    # Seed a CRM customer with the email already on file.
    db_session.add(Party(id="PRT-T-LINK01", party_type="individual"))
    await db_session.flush()
    db_session.add(
        Individual(
            party_id="PRT-T-LINK01",
            given_name="Existing",
            family_name="Customer",
        )
    )
    db_session.add(
        Customer(
            id="CUST-T-LINK01",
            party_id="PRT-T-LINK01",
            status="active",
            kyc_status="verified",
            customer_since=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    db_session.add(
        ContactMedium(
            id="CM-T-LINK01",
            party_id="PRT-T-LINK01",
            medium_type="email",
            value="returning@example.sg",
            is_primary=True,
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    await db_session.commit()

    try:
        # Driver: full login flow.
        await start_email_login(
            db_session, email="returning@example.sg", email_adapter=email_adapter
        )
        await db_session.commit()
        otp = email_adapter.records[("returning@example.sg", "login")]["otp"]

        result = await verify_email_login(
            db_session, email="returning@example.sg", code=otp
        )
        await db_session.commit()
        assert isinstance(result, SessionView)

        # Identity is now linked to the pre-existing customer.
        identity = (
            await db_session.execute(
                select(Identity).where(Identity.email == "returning@example.sg")
            )
        ).scalar_one()
        assert identity.customer_id == "CUST-T-LINK01"
        # Status promoted to "registered" (linked) instead of "verified" (unlinked).
        assert identity.status == "registered"
    finally:
        # Clean up CRM rows seeded for this test (the conftest only
        # truncates portal_auth.*; CRM rows persist across tests).
        from sqlalchemy import text

        await db_session.execute(text(
            "DELETE FROM crm.contact_medium WHERE id = 'CM-T-LINK01'"
        ))
        await db_session.execute(text(
            "DELETE FROM crm.customer WHERE id = 'CUST-T-LINK01'"
        ))
        await db_session.execute(text(
            "DELETE FROM crm.individual WHERE party_id = 'PRT-T-LINK01'"
        ))
        await db_session.execute(text(
            "DELETE FROM crm.party WHERE id = 'PRT-T-LINK01'"
        ))
        await db_session.commit()
