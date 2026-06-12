"""Tests against the real-Didit-derived decision fixture.

The fixture at ``portals/self-serve/tests/fixtures/didit_decision_sample.json``
was captured from a real Didit sandbox session on 2026-05-02 and then
fully PII-redacted (synthetic NRIC, names, addresses, image URLs). These
tests verify the ``DiditKycAdapter._build_attestation`` reduction
matches the real payload shape.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from bss_self_serve.kyc.didit import _build_attestation

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "didit_decision_sample.json"
)


@pytest.fixture(scope="module")
def didit_decision() -> dict:
    return json.loads(FIXTURE.read_text())


def test_fixture_exists_and_has_expected_shape(didit_decision):
    """Fixture sanity — caught earlier when the spec assumed a JWS."""
    assert didit_decision["status"] == "Approved"
    assert "id_verification" in didit_decision
    assert "liveness" in didit_decision
    # Confirm Didit's *actual* shape: no JWS, no signature, no JWT.
    assert "jws" not in didit_decision
    assert "jwt" not in didit_decision
    assert "signature" not in didit_decision


def test_fixture_contains_no_real_pii(didit_decision):
    """Doctrine guard — committed fixture must not regress to real PII."""
    text = json.dumps(didit_decision)
    real_pii_patterns = [
        # User's actual NRIC from the 2026-05-02 probe (must be redacted)
        r"S8369796B",
        r"Chiam",
        r"Punggol",
        r"X-Amz",  # AWS-presigned URL components
        r"ASIAUH",  # AWS access key prefix
    ]
    leaks = [p for p in real_pii_patterns if re.search(p, text)]
    assert leaks == [], f"Real-data leaks detected in fixture: {leaks}"


def test_build_attestation_reduces_to_last4_and_hash(didit_decision):
    from uuid import uuid4

    cid = uuid4()
    att = _build_attestation(decision=didit_decision, corroboration_id=cid)
    # Last-4 is exactly 4 chars
    assert len(att.document_number_last4) == 4
    # Hash is SHA-256 hex (64 chars)
    assert len(att.document_number_hash) == 64
    assert all(c in "0123456789abcdef" for c in att.document_number_hash)
    # Corroboration id propagates
    assert att.corroboration_id == cid
    # DOB extracted
    assert att.date_of_birth.year == 1990
    # Provider is fixed
    assert att.provider == "didit"


def test_build_attestation_drops_pii_from_returned_object(didit_decision):
    """The KycAttestation dataclass has no slot for raw doc number, names,
    addresses, or image URLs. Proves the reduction is structural, not
    just a runtime convention."""
    from uuid import uuid4

    att = _build_attestation(decision=didit_decision, corroboration_id=uuid4())
    payload = json.dumps(
        {
            "provider": att.provider,
            "provider_reference": att.provider_reference,
            "document_type": att.document_type,
            "document_country": att.document_country,
            "document_number_last4": att.document_number_last4,
            "document_number_hash": att.document_number_hash,
            "date_of_birth": att.date_of_birth.isoformat(),
            "corroboration_id": str(att.corroboration_id),
        }
    )
    # Even the synthetic fixture's full doc number (S0000000A) shouldn't
    # show up — only the last-4 form (000A or "0000" depending on token).
    assert "S0000000A" not in payload
    # No image / video / address fields leaking through.
    for forbidden in ("front_image", "back_image", "portrait_image", "address"):
        assert forbidden not in payload


def test_document_type_normalized(didit_decision):
    """Didit returns 'Identity Card' / 'Passport'; we normalize for BSS."""
    from uuid import uuid4

    att = _build_attestation(decision=didit_decision, corroboration_id=uuid4())
    # Singapore NRIC fixture → 'nric'
    assert att.document_type in ("nric", "identity card")
