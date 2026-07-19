"""KYC attestation intake endpoints."""

import structlog
from fastapi import APIRouter, Depends

from app.dependencies import get_kyc_service
from app.schemas.internal.kyc import KycAttestationRequest, KycStatusResponse
from app.services.kyc_service import KycService

log = structlog.get_logger()

router = APIRouter(tags=["KYC"])


@router.post("/customer/{customer_id}/kyc-attestation", status_code=200)
async def attest_kyc(
    customer_id: str,
    body: KycAttestationRequest,
    svc: KycService = Depends(get_kyc_service),
) -> dict:
    # v0.15 — never log document_number (raw or even stub-derived). The
    # service derives last4 + hash if needed; only the reduced forms are
    # safe to log.
    log.info(
        "kyc.attestation.received",
        customer_id=customer_id,
        provider=body.provider,
        document_type=body.document_type,
        has_corroboration=body.corroboration_id is not None,
    )
    result = await svc.attest(
        customer_id,
        provider=body.provider,
        provider_reference=body.provider_reference,
        document_type=body.document_type,
        document_number=body.document_number,
        document_number_last4=body.document_number_last4,
        document_number_hash=body.document_number_hash,
        document_country=body.document_country,
        date_of_birth=body.date_of_birth,
        nationality=body.nationality,
        verified_at=body.verified_at,
        attestation_payload=body.attestation_payload,
        corroboration_id=body.corroboration_id,
    )
    return result


@router.get("/customer/{customer_id}/kyc-status", response_model=KycStatusResponse)
async def get_kyc_status(
    customer_id: str,
    svc: KycService = Depends(get_kyc_service),
) -> KycStatusResponse:
    result = await svc.get_status(customer_id)
    return KycStatusResponse(**result)
