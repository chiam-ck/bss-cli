"""v0.15 — corroboration check on Didit attestations.

The policy ``check_attestation_signature`` is provider-aware. For
``didit`` attestations it requires a corroboration row in
``integrations.kyc_webhook_corroboration``; the row is the trust anchor
since Didit's decision API has no JWS.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from app.policies.base import PolicyViolation
from app.policies.kyc import check_attestation_signature
from bss_models.integrations import KycWebhookCorroboration, WebhookEvent
from sqlalchemy import text


@pytest.fixture(autouse=True)
async def _truncate_corroboration(db_session):
    await db_session.execute(text("TRUNCATE integrations.webhook_event CASCADE"))
    await db_session.flush()
    yield


async def _seed_webhook_event(db_session, *, provider: str, event_id: str) -> None:
    db_session.add(
        WebhookEvent(
            provider=provider,
            event_id=event_id,
            event_type="status.updated",
            body={"status": "Approved"},
            signature_valid=True,
        )
    )
    await db_session.flush()


async def _seed_corroboration(
    db_session,
    *,
    session_id: str = "abc-123",
    decision_status: str = "Approved",
    age: timedelta = timedelta(seconds=10),
) -> str:
    """Insert a corroboration row dated `age` ago. Returns its UUID."""
    cid = uuid4()
    event_id = f"{session_id}:status.updated"
    await _seed_webhook_event(db_session, provider="didit", event_id=event_id)
    received_at = datetime.now(timezone.utc) - age
    db_session.add(
        KycWebhookCorroboration(
            id=cid,
            provider="didit",
            provider_session_id=session_id,
            webhook_event_provider="didit",
            webhook_event_id=event_id,
            decision_status=decision_status,
            decision_body_digest=hashlib.sha256(b"x").hexdigest(),
            received_at=received_at,
        )
    )
    await db_session.flush()
    return str(cid)


async def test_didit_without_corroboration_id_rejects(db_session):
    with pytest.raises(PolicyViolation, match="requires corroboration_id") as ei:
        await check_attestation_signature(
            provider="didit",
            attestation_payload={},
            corroboration_id=None,
            session=db_session,
        )
    assert ei.value.rule == "kyc.attestation.uncorroborated"


async def test_didit_with_unknown_corroboration_id_rejects(db_session):
    with pytest.raises(PolicyViolation, match="No corroborating webhook"):
        await check_attestation_signature(
            provider="didit",
            attestation_payload={},
            corroboration_id=str(uuid4()),
            session=db_session,
        )


async def test_didit_with_declined_corroboration_rejects(db_session):
    cid = await _seed_corroboration(db_session, decision_status="Declined")
    with pytest.raises(PolicyViolation, match="status 'Declined'"):
        await check_attestation_signature(
            provider="didit",
            attestation_payload={},
            corroboration_id=cid,
            session=db_session,
        )


async def test_didit_with_stale_corroboration_rejects(db_session):
    cid = await _seed_corroboration(
        db_session, age=timedelta(minutes=45)
    )
    with pytest.raises(PolicyViolation, match="freshness window"):
        await check_attestation_signature(
            provider="didit",
            attestation_payload={},
            corroboration_id=cid,
            session=db_session,
        )


async def test_didit_with_fresh_approved_corroboration_passes(db_session):
    cid = await _seed_corroboration(
        db_session,
        decision_status="Approved",
        age=timedelta(seconds=5),
    )
    # Should NOT raise.
    await check_attestation_signature(
        provider="didit",
        attestation_payload={},
        corroboration_id=cid,
        session=db_session,
    )


async def test_didit_with_malformed_corroboration_id_rejects(db_session):
    with pytest.raises(PolicyViolation, match="Malformed corroboration_id"):
        await check_attestation_signature(
            provider="didit",
            attestation_payload={},
            corroboration_id="not-a-uuid",
            session=db_session,
        )


async def test_legacy_provider_with_signature_passes_when_allow_prebaked_true(
    db_session, monkeypatch
):
    monkeypatch.setenv("BSS_KYC_ALLOW_PREBAKED", "true")
    monkeypatch.setenv("BSS_ENV", "production")  # explicit-opt-in even in prod
    await check_attestation_signature(
        provider="myinfo",
        attestation_payload={"signature": "stub-sig"},
        corroboration_id=None,
        session=db_session,
    )


async def test_legacy_provider_rejected_in_production_without_opt_in(
    db_session, monkeypatch
):
    monkeypatch.setenv("BSS_ENV", "production")
    monkeypatch.delenv("BSS_KYC_ALLOW_PREBAKED", raising=False)
    with pytest.raises(PolicyViolation, match="not accepted in this environment"):
        await check_attestation_signature(
            provider="myinfo",
            attestation_payload={"signature": "stub-sig"},
            corroboration_id=None,
            session=db_session,
        )


async def test_legacy_provider_without_signature_rejects(
    db_session, monkeypatch
):
    monkeypatch.setenv("BSS_KYC_ALLOW_PREBAKED", "true")
    with pytest.raises(PolicyViolation, match="missing signature"):
        await check_attestation_signature(
            provider="myinfo",
            attestation_payload={},
            corroboration_id=None,
            session=db_session,
        )


# ── BSS_KYC_ALLOW_DOC_REUSE ─────────────────────────────────────────


async def test_doc_hash_unique_check_skipped_when_reuse_allowed(
    db_session, monkeypatch
):
    """Sandbox affordance: the flag bypasses the uniqueness check so
    repeated sandbox-doc signups can re-link to the latest customer."""
    from app.policies.kyc import check_document_hash_unique
    from app.repositories.kyc_repo import KycRepository

    monkeypatch.setenv("BSS_KYC_ALLOW_DOC_REUSE", "true")
    # Call doesn't even need to hit the repo when the flag is on.
    repo = KycRepository(db_session)
    # Should not raise even if hypothetically a duplicate existed.
    await check_document_hash_unique(
        document_type="nric",
        document_number_hash="a" * 64,
        kyc_repo=repo,
    )


async def test_doc_hash_unique_check_enforces_when_reuse_disabled(
    db_session, monkeypatch
):
    """Default behaviour: reuse flag absent → uniqueness still enforced."""
    from app.policies.kyc import check_document_hash_unique
    from app.repositories.kyc_repo import KycRepository

    monkeypatch.delenv("BSS_KYC_ALLOW_DOC_REUSE", raising=False)
    repo = KycRepository(db_session)
    # Without a duplicate row in DB, it should pass cleanly. The
    # blocking-on-duplicate path is exercised by the existing
    # service-level test (it inserts a row first).
    await check_document_hash_unique(
        document_type="nric",
        document_number_hash="b" * 64,
        kyc_repo=repo,
    )
