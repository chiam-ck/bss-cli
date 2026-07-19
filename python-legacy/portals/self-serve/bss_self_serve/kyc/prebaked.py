"""Prebaked KYC adapter — preserves v0.14 behavior for dev / scenarios.

Returns a deterministic per-customer attestation without calling any
external service. Used by the v0.12 14-day soak corpus, hero scenarios,
and any dev environment where ``BSS_PORTAL_KYC_PROVIDER=prebaked``. The
BSS-side policy (``check_attestation_signature``) accepts prebaked only
when ``BSS_KYC_ALLOW_PREBAKED=true`` is set explicitly — defaults to
true in non-production envs and false in production.
"""

from __future__ import annotations

import hashlib
from datetime import date

from .adapter import KycAttestation, KycSession

_PREBAKED_PROVIDER = "prebaked"
_PREBAKED_ATTESTATION_ID = "KYC-PREBAKED-001"


class PrebakedKycAdapter:
    """v0.14-equivalent canned-attestation flow.

    ``initiate`` returns a ``return_url`` that loops straight back to
    the portal callback (no real hosted UI). ``fetch_attestation``
    returns a stable per-email attestation; the document number is a
    deterministic stub derived from the email, hashed in the same shape
    a real adapter would produce, so downstream uniqueness checks work.
    """

    async def initiate(
        self, *, email: str, return_url: str
    ) -> KycSession:
        return KycSession(
            session_id=f"prebaked-{_email_session_token(email)}",
            redirect_url=return_url,
        )

    async def fetch_attestation(
        self, *, session_id: str
    ) -> KycAttestation:
        # session_id is "prebaked-<email_token>" — re-derive the
        # per-customer document_number for hash stability.
        email_token = session_id.removeprefix("prebaked-") or "unknown"
        document_number = _stub_document_number(email_token)
        digest = hashlib.sha256(
            f"{document_number}|SGP|{_PREBAKED_PROVIDER}".encode()
        ).hexdigest()
        return KycAttestation(
            provider=_PREBAKED_PROVIDER,
            provider_reference=_PREBAKED_ATTESTATION_ID,
            document_type="nric",
            document_country="SGP",
            document_number_last4=document_number[-4:],
            document_number_hash=digest,
            date_of_birth=date(1990, 1, 1),
            corroboration_id=None,  # prebaked has no webhook to corroborate
        )


def _email_session_token(email: str) -> str:
    """Stable per-email token; same email → same session id → same hash."""
    return hashlib.sha256(email.lower().encode()).hexdigest()[:16]


def _stub_document_number(token: str) -> str:
    """Synthesize a Singapore-NRIC-shaped document number from a token.

    Format: ``S<7-digits><checksum-letter>`` — derived from the token so
    two different emails produce two different hashes. The checksum is
    the first letter of the token (uppercased), giving us a stable
    9-character string.
    """
    digits = "".join(ch for ch in token if ch.isdigit())[:7].rjust(7, "0")
    if not any(ch.isdigit() for ch in token):
        # Token is all hex letters; fall back to ord-summing the token
        # to give a digit string.
        digits = f"{abs(hash(token)) % 10_000_000:07d}"
    checksum = (token[0].upper() if token else "Z")
    if not checksum.isalpha():
        checksum = "Z"
    return f"S{digits}{checksum}"
