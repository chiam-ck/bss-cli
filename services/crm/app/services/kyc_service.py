"""KYC attestation service.

Receives signed attestations from channel layer.
Hashes document_number immediately — NEVER stores or logs plaintext.
"""

import hashlib
from datetime import datetime

import structlog
from bss_clock import now as clock_now
from bss_models.crm import CustomerIdentity, Interaction
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context
from app.events import publisher
from app.policies import kyc as kyc_policies
from app.repositories.customer_repo import CustomerRepository
from app.repositories.interaction_repo import InteractionRepository
from app.repositories.kyc_repo import KycRepository

log = structlog.get_logger()

from uuid import uuid4


def _next_int_id() -> str:
    return f"INT-{uuid4().hex[:8]}"


class KycService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        customer_repo: CustomerRepository,
        kyc_repo: KycRepository,
        interaction_repo: InteractionRepository,
    ) -> None:
        self._session = session
        self._customer_repo = customer_repo
        self._kyc_repo = kyc_repo
        self._interaction_repo = interaction_repo

    async def attest(
        self,
        customer_id: str,
        *,
        provider: str,
        provider_reference: str,
        document_type: str,
        document_number: str | None = None,
        document_number_last4: str | None = None,
        document_number_hash: str | None = None,
        document_country: str,
        date_of_birth: str,
        nationality: str | None = None,
        verified_at: str,
        attestation_payload: dict,
        corroboration_id: str | None = None,
    ) -> dict:
        ctx = auth_context.current()

        # --- Reduce raw → last4 + hash if needed ---
        # The Didit path supplies pre-reduced fields (raw never crosses
        # the BSS boundary). The legacy/prebaked path supplies raw
        # ``document_number`` and the service reduces it here. This is
        # the LAST place a raw document number is allowed to exist.
        if document_number_last4 is None or document_number_hash is None:
            if document_number is None:
                from app.policies.base import PolicyViolation

                raise PolicyViolation(
                    rule="customer.attest_kyc.document_required",
                    message=(
                        "Either document_number (legacy) or "
                        "(document_number_last4 + document_number_hash) "
                        "must be supplied"
                    ),
                    context={"provider": provider},
                )
            normalized = document_number.upper().strip()
            document_number_last4 = normalized[-4:] if len(normalized) >= 4 else normalized
            document_number_hash = hashlib.sha256(
                f"{normalized}|{document_country}|{provider}".encode()
            ).hexdigest()
        # From this point, raw document_number is dead. We do NOT pass it anywhere else.
        document_number = None

        # --- Policies ---
        await kyc_policies.check_customer_exists(customer_id, self._customer_repo)
        await kyc_policies.check_attestation_signature(
            provider=provider,
            attestation_payload=attestation_payload,
            corroboration_id=corroboration_id,
            session=self._session,
        )
        await kyc_policies.check_document_hash_unique(
            document_type, document_number_hash, self._kyc_repo
        )

        # --- Write ---
        from datetime import date as date_type
        from uuid import UUID

        now = clock_now()
        # v0.15 — when ``BSS_KYC_ALLOW_DOC_REUSE=true`` (sandbox affordance),
        # the policy doesn't reject duplicate document hashes; the service
        # re-links the existing identity row to the new customer instead
        # of inserting (the DB unique index would reject the insert
        # otherwise). Real production keeps the flag false, the policy
        # enforces uniqueness, and this branch never runs.
        existing = await self._kyc_repo.find_by_document_hash(
            tenant_id=ctx.tenant,
            document_type=document_type,
            document_number_hash=document_number_hash,
        )
        if existing is not None:
            log.info(
                "kyc.document_hash_relink",
                old_customer_id=existing.customer_id,
                new_customer_id=customer_id,
            )
            # Drop the old row and create a new one for the new
            # customer. CustomerIdentity has customer_id as PK so an
            # in-place customer_id update would violate FK ordering.
            await self._kyc_repo.delete(existing)
            await self._session.flush()
        identity = CustomerIdentity(
            customer_id=customer_id,
            document_type=document_type,
            document_number_hash=document_number_hash,
            document_number_last4=document_number_last4,
            document_country=document_country,
            date_of_birth=date_type.fromisoformat(date_of_birth),
            nationality=nationality,
            verified_by=provider,
            attestation_payload=attestation_payload,
            verified_at=datetime.fromisoformat(verified_at),
            corroboration_id=UUID(corroboration_id) if corroboration_id else None,
            tenant_id=ctx.tenant,
        )
        await self._kyc_repo.create(identity)

        # Update customer KYC status
        cust = await self._customer_repo.get(customer_id)
        cust.kyc_status = "verified"
        cust.kyc_verified_at = now
        cust.kyc_verification_method = provider
        cust.kyc_reference = provider_reference
        await self._customer_repo.update(cust)

        # --- Event (NEVER include plaintext document_number) ---
        await publisher.publish(
            self._session,
            event_type="customer.kyc_attested",
            aggregate_type="customer",
            aggregate_id=customer_id,
            payload={
                "provider": provider,
                "document_type": document_type,
                "document_country": document_country,
                "kyc_status": "verified",
            },
        )

        # --- Interaction auto-log ---
        await self._interaction_repo.create(
            Interaction(
                id=_next_int_id(),
                customer_id=customer_id,
                channel=ctx.channel,
                direction="inbound",
                summary=f"KYC attested via {provider}",
                occurred_at=now,
                tenant_id=ctx.tenant,
            )
        )

        await self._session.commit()

        return {
            "customer_id": customer_id,
            "kyc_status": "verified",
            "provider": provider,
            "verified_at": now.isoformat(),
        }

    async def get_status(self, customer_id: str) -> dict:
        cust = await self._customer_repo.get(customer_id)
        if not cust:
            from app.policies.base import PolicyViolation
            raise PolicyViolation(
                rule="customer.kyc.not_found",
                message=f"Customer {customer_id} not found",
                context={"customer_id": customer_id},
            )
        return {
            "customer_id": customer_id,
            "kyc_status": cust.kyc_status,
            "kyc_verified_at": cust.kyc_verified_at.isoformat() if cust.kyc_verified_at else None,
            "kyc_verification_method": cust.kyc_verification_method,
            "kyc_reference": cust.kyc_reference,
        }
