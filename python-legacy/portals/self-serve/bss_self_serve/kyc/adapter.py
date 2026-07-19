"""KYC verification adapter Protocol + value types (v0.15).

The Protocol shape mirrors v0.14's email adapter doctrine: per-domain
Protocols, no unified ``Provider.execute()``. ``initiate()`` starts a
verification session and returns the redirect URL the customer is sent
to; ``fetch_attestation()`` reads back the verified result after the
hosted-UI flow completes.

``KycAttestation`` is the **only** shape that crosses the BSS boundary —
``last4`` + ``hash`` for the document number, no full names, no
addresses, no biometric URLs. See ``phases/V0_15_0.md`` §1.4 for the
privacy doctrine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True)
class KycSession:
    """Result of ``initiate()``. The portal redirects the customer to
    ``redirect_url``; later, the customer returns via the callback route
    and ``session_id`` is used to fetch the attestation."""

    session_id: str
    redirect_url: str


@dataclass(frozen=True)
class KycAttestation:
    """Verification receipt — what BSS sees.

    Names, addresses, biometric URLs, MRZ data, liveness scores, and the
    raw document number are all explicitly NOT carried. Customer
    identifying details on the BSS side stay in ``crm.customer`` from the
    customer's own signup form. The attestation is verification-only.
    """

    provider: str
    provider_reference: str
    document_type: str
    document_country: str
    document_number_last4: str
    document_number_hash: str
    date_of_birth: date
    corroboration_id: UUID | None = None


class KycCapExhausted(Exception):
    """Raised by ``initiate()`` when the provider's monthly free-tier
    counter is exhausted. Doctrine: hard-block, no silent fallback to
    prebaked. Customer sees a templated retry page; ops gets a
    high-priority ``kyc.cap_exhausted`` event."""


class KycCorroborationTimeout(Exception):
    """Raised by ``fetch_attestation()`` when the corroborating
    HMAC-signed webhook has not arrived within the polling window
    (default 10s). Customer sees a templated retry page; the webhook
    will likely arrive on the next click."""


@runtime_checkable
class KycVerificationAdapter(Protocol):
    """Channel-layer KYC verification surface.

    ``initiate`` is called when the customer is ready to begin
    verification; the returned ``KycSession`` carries the redirect URL
    the customer is sent to (Didit's hosted UI in production, an inline
    canned flow in prebaked mode).

    ``fetch_attestation`` is called from the portal callback handler
    after the customer returns from the verification provider. For Didit
    it blocks on the corroborating webhook; for prebaked it returns
    immediately.
    """

    async def initiate(
        self, *, email: str, return_url: str
    ) -> KycSession: ...

    async def fetch_attestation(
        self, *, session_id: str
    ) -> KycAttestation: ...
