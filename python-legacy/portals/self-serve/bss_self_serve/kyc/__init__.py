"""Portal-side KYC adapter (v0.15).

The KYC verification flow lives in the portal because eKYC is a
channel-layer concern (per ``CLAUDE.md`` "Scope boundaries"). The portal
runs the customer through the verification provider's hosted UI, receives
the verified attestation, reduces document PII to ``last4 + hash``, and
submits the receipt to BSS via ``customer.attest_kyc``. BSS-side trust is
anchored by the HMAC-signed webhook recorded in
``integrations.kyc_webhook_corroboration`` — see
``services/crm/app/policies/kyc.py``.
"""

from .adapter import (
    KycAttestation,
    KycCapExhausted,
    KycCorroborationTimeout,
    KycSession,
    KycVerificationAdapter,
)
from .didit import DiditKycAdapter
from .prebaked import PrebakedKycAdapter
from .select import select_kyc_adapter

__all__ = [
    "KycAttestation",
    "KycCapExhausted",
    "KycCorroborationTimeout",
    "KycSession",
    "KycVerificationAdapter",
    "DiditKycAdapter",
    "PrebakedKycAdapter",
    "select_kyc_adapter",
]
