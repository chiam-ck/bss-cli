"""Step-up flow — start, verify (action_label scoping), consume one-shot grant."""

from __future__ import annotations

from datetime import timedelta

import pytest
from bss_clock import advance, freeze
from bss_portal_auth import (
    StepUpFailed,
    StepUpToken,
    consume_step_up_token,
    start_step_up,
    verify_step_up,
)
from bss_portal_auth.test_helpers import create_test_session, last_step_up_code


@pytest.mark.asyncio
async def test_start_step_up_emits_otp_via_email_adapter(db_session, email_adapter):
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    challenge = await start_step_up(
        db_session,
        session_id=sess.id,
        action_label="subscription.terminate",
        email_adapter=email_adapter,
    )
    assert challenge.action_label == "subscription.terminate"
    otp = last_step_up_code(email_adapter, "ada@x.sg", "subscription.terminate")
    assert otp is not None and len(otp) == 6


@pytest.mark.asyncio
async def test_verify_step_up_with_correct_code_returns_one_shot_grant(
    db_session, email_adapter
):
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    await start_step_up(
        db_session,
        session_id=sess.id,
        action_label="subscription.terminate",
        email_adapter=email_adapter,
    )
    otp = last_step_up_code(email_adapter, "ada@x.sg", "subscription.terminate")

    result = await verify_step_up(
        db_session,
        session_id=sess.id,
        code=otp,
        action_label="subscription.terminate",
    )
    assert isinstance(result, StepUpToken)
    assert result.action_label == "subscription.terminate"
    assert len(result.token) == 32


@pytest.mark.asyncio
async def test_verify_step_up_rejects_wrong_code(db_session, email_adapter):
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    await start_step_up(
        db_session,
        session_id=sess.id,
        action_label="subscription.terminate",
        email_adapter=email_adapter,
    )
    result = await verify_step_up(
        db_session,
        session_id=sess.id,
        code="000000",
        action_label="subscription.terminate",
    )
    assert isinstance(result, StepUpFailed)
    assert result.reason == "wrong_code"


@pytest.mark.asyncio
async def test_step_up_token_does_not_validate_for_different_action(
    db_session, email_adapter
):
    """Doctrine — step-up tokens are scoped to a single action_label."""
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    await start_step_up(
        db_session,
        session_id=sess.id,
        action_label="subscription.terminate",
        email_adapter=email_adapter,
    )
    otp = last_step_up_code(email_adapter, "ada@x.sg", "subscription.terminate")

    # Try to verify it as if it were a step-up for a *different* action.
    result = await verify_step_up(
        db_session,
        session_id=sess.id,
        code=otp,
        action_label="payment.remove_method",
    )
    assert isinstance(result, StepUpFailed)
    assert result.reason == "no_active_token"


@pytest.mark.asyncio
async def test_consume_step_up_token_is_one_shot(db_session, email_adapter):
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    await start_step_up(
        db_session,
        session_id=sess.id,
        action_label="subscription.terminate",
        email_adapter=email_adapter,
    )
    otp = last_step_up_code(email_adapter, "ada@x.sg", "subscription.terminate")

    grant = await verify_step_up(
        db_session,
        session_id=sess.id,
        code=otp,
        action_label="subscription.terminate",
    )
    assert isinstance(grant, StepUpToken)

    first = await consume_step_up_token(
        db_session,
        session_id=sess.id,
        token=grant.token,
        action_label="subscription.terminate",
    )
    assert first is True

    # Replay must fail.
    second = await consume_step_up_token(
        db_session,
        session_id=sess.id,
        token=grant.token,
        action_label="subscription.terminate",
    )
    assert second is False


@pytest.mark.asyncio
async def test_consume_step_up_rejects_wrong_action_label(db_session, email_adapter):
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    await start_step_up(
        db_session,
        session_id=sess.id,
        action_label="subscription.terminate",
        email_adapter=email_adapter,
    )
    otp = last_step_up_code(email_adapter, "ada@x.sg", "subscription.terminate")
    grant = await verify_step_up(
        db_session,
        session_id=sess.id,
        code=otp,
        action_label="subscription.terminate",
    )
    assert isinstance(grant, StepUpToken)

    # Token is valid only for terminate; a payment route can't consume it.
    ok = await consume_step_up_token(
        db_session,
        session_id=sess.id,
        token=grant.token,
        action_label="payment.remove_method",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_step_up_grant_expires_after_60_seconds(db_session, email_adapter):
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    freeze()
    await start_step_up(
        db_session,
        session_id=sess.id,
        action_label="subscription.terminate",
        email_adapter=email_adapter,
    )
    otp = last_step_up_code(email_adapter, "ada@x.sg", "subscription.terminate")
    grant = await verify_step_up(
        db_session,
        session_id=sess.id,
        code=otp,
        action_label="subscription.terminate",
    )
    assert isinstance(grant, StepUpToken)

    advance(timedelta(seconds=120))
    ok = await consume_step_up_token(
        db_session,
        session_id=sess.id,
        token=grant.token,
        action_label="subscription.terminate",
    )
    assert ok is False
