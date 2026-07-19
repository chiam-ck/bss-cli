"""KYC attestation DTOs.

v0.15 made ``document_number`` optional and added the pre-reduced
``document_number_last4`` + ``document_number_hash`` fields. Doctrine:
the Didit path (``provider="didit"``) MUST submit the reduced form so
raw NRIC never crosses the BSS boundary; the legacy/prebaked path
(stub callers in scenarios + the CSR cockpit) may still submit raw
``document_number``, which the service hashes and reduces internally.
The CRM service rejects requests that supply neither.
"""

from pydantic import BaseModel


class KycAttestationRequest(BaseModel):
    provider: str
    provider_reference: str
    document_type: str
    # Either ``document_number`` (legacy/prebaked path) or both
    # ``document_number_last4`` + ``document_number_hash`` (Didit path)
    # MUST be supplied. Service-side validation enforces.
    document_number: str | None = None
    document_number_last4: str | None = None
    document_number_hash: str | None = None
    document_country: str
    date_of_birth: str
    nationality: str | None = None
    verified_at: str
    attestation_payload: dict
    # v0.15 — required when ``provider="didit"``. References
    # ``integrations.kyc_webhook_corroboration.id``; the policy layer
    # validates the corroboration row exists and is fresh.
    corroboration_id: str | None = None


class KycStatusResponse(BaseModel):
    customer_id: str
    kyc_status: str
    kyc_verified_at: str | None = None
    kyc_verification_method: str | None = None
    kyc_reference: str | None = None
