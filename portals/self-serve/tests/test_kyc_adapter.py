"""Unit tests for the v0.15 KYC adapter Protocol + impls."""

from __future__ import annotations

import pytest
from bss_self_serve.kyc import (
    KycAttestation,
    KycSession,
    KycVerificationAdapter,
    PrebakedKycAdapter,
    select_kyc_adapter,
)


@pytest.mark.asyncio
async def test_prebaked_initiate_returns_loopback_redirect():
    adapter = PrebakedKycAdapter()
    result = await adapter.initiate(
        email="alice@example.com", return_url="/signup/step/kyc/callback?session=X"
    )
    assert isinstance(result, KycSession)
    # Prebaked loops the customer straight back to the portal callback,
    # no external hop.
    assert result.redirect_url == "/signup/step/kyc/callback?session=X"
    assert result.session_id.startswith("prebaked-")


@pytest.mark.asyncio
async def test_prebaked_fetch_attestation_is_deterministic_per_email():
    adapter = PrebakedKycAdapter()
    s1 = await adapter.initiate(email="alice@example.com", return_url="/x")
    s2 = await adapter.initiate(email="alice@example.com", return_url="/x")
    s3 = await adapter.initiate(email="bob@example.com", return_url="/x")
    a1 = await adapter.fetch_attestation(session_id=s1.session_id)
    a2 = await adapter.fetch_attestation(session_id=s2.session_id)
    a3 = await adapter.fetch_attestation(session_id=s3.session_id)
    # Same email → same hash (uniqueness check passes once); different
    # email → different hash (uniqueness check rejects on second use).
    assert a1.document_number_hash == a2.document_number_hash
    assert a1.document_number_hash != a3.document_number_hash


@pytest.mark.asyncio
async def test_prebaked_attestation_carries_only_reduced_pii():
    adapter = PrebakedKycAdapter()
    s = await adapter.initiate(email="alice@example.com", return_url="/x")
    att = await adapter.fetch_attestation(session_id=s.session_id)
    assert isinstance(att, KycAttestation)
    assert att.provider == "prebaked"
    assert len(att.document_number_last4) == 4
    assert len(att.document_number_hash) == 64  # SHA-256 hex
    assert att.corroboration_id is None  # prebaked has no webhook
    # Critical: the dataclass has no slots for raw doc number, names,
    # addresses, or biometric URLs. This is a structural guarantee.
    fields = set(KycAttestation.__dataclass_fields__)
    forbidden = {
        "document_number",
        "first_name",
        "last_name",
        "full_name",
        "address",
        "place_of_birth",
        "front_image",
        "back_image",
        "portrait_image",
        "reference_image",
        "video_url",
    }
    assert fields & forbidden == set()


def test_select_prebaked():
    adapter = select_kyc_adapter(name="prebaked")
    assert isinstance(adapter, PrebakedKycAdapter)
    assert isinstance(adapter, KycVerificationAdapter)


def test_select_didit_requires_api_key():
    with pytest.raises(RuntimeError, match="BSS_PORTAL_KYC_DIDIT_API_KEY"):
        select_kyc_adapter(name="didit")


def test_select_didit_requires_workflow_id():
    with pytest.raises(RuntimeError, match="BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID"):
        select_kyc_adapter(name="didit", didit_api_key="k_test_xxx")


def test_select_didit_requires_session_factory():
    with pytest.raises(RuntimeError, match="DB session factory"):
        select_kyc_adapter(
            name="didit",
            didit_api_key="k_test_xxx",
            didit_workflow_id="00000000-0000-0000-0000-000000000000",
        )


def test_select_unknown_fails_fast():
    with pytest.raises(RuntimeError, match="Unknown BSS_PORTAL_KYC_PROVIDER"):
        select_kyc_adapter(name="myinfo")
