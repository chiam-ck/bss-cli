"""KYC attestation policies.

v0.15: ``check_attestation_signature`` is now provider-aware.

* ``provider="didit"`` → the attestation MUST carry a ``corroboration_id``
  pointing at an HMAC-verified webhook row in
  ``integrations.kyc_webhook_corroboration``. The row is the trust anchor
  (Didit's decision API has no JWS — see DECISIONS 2026-05-02 amendment 2).
  Validates the row exists, status is ``Approved``, and ``received_at`` is
  within the freshness window (30 minutes).
* anything else (``prebaked``, ``myinfo`` legacy alias) → preserves the
  v0.14 stub-signature check, but only when ``BSS_KYC_ALLOW_PREBAKED=true``
  is set explicitly. Defaults to ``true`` outside production and ``false``
  in production.
"""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import UUID

import structlog
from bss_clock import now as clock_now
from bss_models.integrations import KycWebhookCorroboration
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.policies.base import PolicyViolation, policy
from app.repositories.customer_repo import CustomerRepository
from app.repositories.kyc_repo import KycRepository

log = structlog.get_logger(__name__)

CORROBORATION_FRESHNESS_WINDOW = timedelta(minutes=30)
DIDIT_PROVIDER = "didit"


def _allow_prebaked() -> bool:
    """``BSS_KYC_ALLOW_PREBAKED`` defaults true outside production."""
    raw = os.environ.get("BSS_KYC_ALLOW_PREBAKED", "")
    if raw:
        return raw.lower() in ("1", "true", "yes")
    env = (os.environ.get("BSS_ENV", "") or "").lower()
    return env != "production"


def _allow_doc_reuse() -> bool:
    """``BSS_KYC_ALLOW_DOC_REUSE`` is a sandbox-testing affordance.

    Real Didit / Singpass / Onfido production return per-person
    document numbers that are globally unique by definition — so the
    ``customer.attest_kyc.document_hash_unique_per_tenant`` policy is
    a real invariant in production. But Didit sandbox returns a
    STABLE test document number for every verification: every test
    signup hashes to the same value, every second-and-later attempt
    trips the policy. Setting this flag to ``true`` in dev /
    sandbox lets the same document hash re-link to the latest
    customer (the prior row's ``customer_id`` is overwritten in
    place). Defaults false, even outside production — opting in is
    explicit, like ``BSS_KYC_ALLOW_PREBAKED``.
    """
    raw = os.environ.get("BSS_KYC_ALLOW_DOC_REUSE", "")
    return raw.lower() in ("1", "true", "yes") if raw else False


@policy("customer.attest_kyc.customer_exists")
async def check_customer_exists(
    customer_id: str, repo: CustomerRepository
) -> None:
    cust = await repo.get(customer_id)
    if not cust:
        raise PolicyViolation(
            rule="customer.attest_kyc.customer_exists",
            message=f"Customer {customer_id} does not exist",
            context={"customer_id": customer_id},
        )


@policy("customer.attest_kyc.attestation_signature_valid")
async def check_attestation_signature(
    *,
    provider: str,
    attestation_payload: dict,
    corroboration_id: str | None,
    session: AsyncSession,
) -> None:
    """v0.15 — provider-aware verification.

    Replaces the v0.14 stub that accepted any payload with a ``signature``
    field. The Didit path requires a corroborating HMAC-verified webhook
    row; the legacy path is gated on ``BSS_KYC_ALLOW_PREBAKED``.
    """
    if provider == DIDIT_PROVIDER:
        if not corroboration_id:
            raise PolicyViolation(
                rule="kyc.attestation.uncorroborated",
                message="Didit attestation requires corroboration_id",
                context={"provider": provider},
            )
        try:
            cid = (
                UUID(corroboration_id)
                if isinstance(corroboration_id, str)
                else corroboration_id
            )
        except (TypeError, ValueError) as exc:
            raise PolicyViolation(
                rule="kyc.attestation.uncorroborated",
                message=f"Malformed corroboration_id: {exc}",
                context={"provider": provider},
            ) from exc

        row = (
            await session.execute(
                select(KycWebhookCorroboration).where(
                    KycWebhookCorroboration.id == cid,
                    KycWebhookCorroboration.provider == DIDIT_PROVIDER,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise PolicyViolation(
                rule="kyc.attestation.uncorroborated",
                message=(
                    "No corroborating webhook delivery found for the "
                    "supplied corroboration_id"
                ),
                context={"corroboration_id": str(cid)},
            )
        if row.decision_status != "Approved":
            raise PolicyViolation(
                rule="kyc.attestation.uncorroborated",
                message=(
                    f"Corroborating webhook reports status "
                    f"{row.decision_status!r}; only 'Approved' attestations "
                    "are acceptable"
                ),
                context={"decision_status": row.decision_status},
            )
        age = clock_now() - row.received_at
        if age > CORROBORATION_FRESHNESS_WINDOW:
            raise PolicyViolation(
                rule="kyc.attestation.uncorroborated",
                message=(
                    f"Corroborating webhook is older than the freshness "
                    f"window ({CORROBORATION_FRESHNESS_WINDOW})"
                ),
                context={"age_seconds": age.total_seconds()},
            )
        return

    # Legacy / prebaked path
    if not _allow_prebaked():
        raise PolicyViolation(
            rule="kyc.attestation.uncorroborated",
            message=(
                f"Provider {provider!r} not accepted in this environment. "
                "Production requires BSS_KYC_ALLOW_PREBAKED=true to accept "
                "prebaked / legacy attestations."
            ),
            context={"provider": provider},
        )
    if "signature" not in (attestation_payload or {}):
        raise PolicyViolation(
            rule="kyc.attestation.uncorroborated",
            message="Attestation payload missing signature",
            context={},
        )


@policy("customer.attest_kyc.document_hash_unique_per_tenant")
async def check_document_hash_unique(
    document_type: str, document_number_hash: str, kyc_repo: KycRepository
) -> None:
    if _allow_doc_reuse():
        # Sandbox affordance — caller (service layer) re-links instead
        # of inserting on duplicate. See ``_allow_doc_reuse`` docstring.
        return
    ctx = auth_context.current()
    existing = await kyc_repo.find_by_document_hash(
        tenant_id=ctx.tenant,
        document_type=document_type,
        document_number_hash=document_number_hash,
    )
    if existing:
        raise PolicyViolation(
            rule="customer.attest_kyc.document_hash_unique_per_tenant",
            message="This identity document is already registered",
            context={"document_type": document_type},
        )
