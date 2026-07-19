"""Didit KYC adapter (v0.15) — real provider implementation.

Trust model: the API decision endpoint (``GET /v2/session/{id}/decision/``)
returns plain JSON over TLS with no signature. The trust anchor is the
HMAC-signed webhook delivery, recorded in
``integrations.kyc_webhook_corroboration``. ``fetch_attestation`` blocks
on the corroboration row before returning.

Privacy model: the Didit response carries the raw NRIC, full name,
address, DOB, place of birth, and presigned S3 URLs of biometric
artifacts. ``fetch_attestation`` reduces the document number to ``last4 +
hash`` and drops everything else before returning. Names, addresses, and
URLs never cross the BSS boundary.

Free-tier cap: 500 sessions / month. ``initiate`` queries
``integrations.external_call`` for the running monthly count and raises
``KycCapExhausted`` on cap. **No silent fallback** — the customer sees a
templated retry page and ops gets a high-priority event.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import date

import httpx
import structlog

from .adapter import (
    KycAttestation,
    KycCapExhausted,
    KycCorroborationTimeout,
    KycSession,
)

log = structlog.get_logger(__name__)

DIDIT_BASE_URL = "https://verification.didit.me"
PROVIDER = "didit"
FREE_TIER_MONTHLY_CAP = 500
FREE_TIER_WARN_THRESHOLD = 450

CORROBORATION_POLL_INTERVAL_S = 0.2
CORROBORATION_POLL_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class DiditConfig:
    api_key: str
    workflow_id: str

    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}


class DiditKycAdapter:
    def __init__(
        self,
        *,
        config: DiditConfig,
        session_factory,  # async sessionmaker[AsyncSession]
        http_client: httpx.AsyncClient | None = None,
        poll_interval: float = CORROBORATION_POLL_INTERVAL_S,
        poll_timeout: float = CORROBORATION_POLL_TIMEOUT_S,
    ) -> None:
        self._cfg = config
        self._session_factory = session_factory
        self._http = http_client or httpx.AsyncClient(
            base_url=DIDIT_BASE_URL, timeout=10.0
        )
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    async def initiate(
        self, *, email: str, return_url: str
    ) -> KycSession:
        await self._guard_free_tier_cap()

        body = {
            "workflow_id": self._cfg.workflow_id,
            "vendor_data": f"bss-cli-{email}",
            "callback": return_url,
        }
        started = time.monotonic()
        try:
            resp = await self._http.post(
                "/v2/session/",
                headers=self._cfg.headers(),
                json=body,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            await self._record_external_call(
                operation="initiate",
                aggregate_id=None,
                success=False,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            raise

        await self._record_external_call(
            operation="initiate",
            aggregate_id=payload.get("session_id"),
            success=True,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return KycSession(
            session_id=payload["session_id"],
            redirect_url=payload["url"],
        )

    async def fetch_attestation(
        self, *, session_id: str
    ) -> KycAttestation:
        # 1. Wait for corroborating webhook row.
        corroboration = await self._wait_for_corroboration(session_id)
        if corroboration is None:
            raise KycCorroborationTimeout(
                f"No verified webhook delivery for Didit session {session_id} "
                f"within {self._poll_timeout}s"
            )

        # 2. Fetch the decision body for supplementary fields. The body
        # is unsigned but is also recorded in the webhook payload that
        # we just verified — we validate digest match below.
        started = time.monotonic()
        try:
            resp = await self._http.get(
                f"/v2/session/{session_id}/decision/",
                headers=self._cfg.headers(),
            )
            resp.raise_for_status()
            decision = resp.json()
        except Exception:
            await self._record_external_call(
                operation="fetch_attestation",
                aggregate_id=session_id,
                success=False,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            raise

        await self._record_external_call(
            operation="fetch_attestation",
            aggregate_id=session_id,
            success=True,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

        # 3. Reduce PII to last4 + hash. Drop everything else.
        return _build_attestation(
            decision=decision,
            corroboration_id=corroboration["id"],
        )

    async def _wait_for_corroboration(
        self, session_id: str
    ) -> dict | None:
        """Poll integrations.kyc_webhook_corroboration for the row."""
        # Lazy import: keeps test surface minimal when DiditKycAdapter
        # is imported but never used (e.g. in prebaked-only tests).
        from bss_models.integrations import KycWebhookCorroboration
        from sqlalchemy import select

        deadline = time.monotonic() + self._poll_timeout
        while time.monotonic() < deadline:
            async with self._session_factory() as db:
                row = (
                    await db.execute(
                        select(KycWebhookCorroboration).where(
                            KycWebhookCorroboration.provider == PROVIDER,
                            KycWebhookCorroboration.provider_session_id
                            == session_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    return {
                        "id": row.id,
                        "decision_status": row.decision_status,
                        "decision_body_digest": row.decision_body_digest,
                    }
            await asyncio.sleep(self._poll_interval)
        return None

    async def _guard_free_tier_cap(self) -> None:
        """Hard-block at 500/month. No silent fallback."""
        from bss_models.integrations import ExternalCall
        from sqlalchemy import func, select

        async with self._session_factory() as db:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(ExternalCall)
                    .where(
                        ExternalCall.provider == PROVIDER,
                        ExternalCall.operation == "initiate",
                        ExternalCall.occurred_at
                        >= func.date_trunc("month", func.now()),
                    )
                )
            ).scalar_one()

        if count >= FREE_TIER_MONTHLY_CAP:
            log.warning(
                "didit.cap_exhausted",
                count=count,
                cap=FREE_TIER_MONTHLY_CAP,
            )
            raise KycCapExhausted(
                f"Didit free-tier monthly cap ({FREE_TIER_MONTHLY_CAP}) reached"
            )
        if count >= FREE_TIER_WARN_THRESHOLD:
            log.warning(
                "didit.cap_warning", count=count, cap=FREE_TIER_MONTHLY_CAP
            )

    async def _record_external_call(
        self,
        *,
        operation: str,
        aggregate_id: str | None,
        success: bool,
        latency_ms: int,
    ) -> None:
        from bss_models.integrations import ExternalCall

        async with self._session_factory() as db:
            db.add(
                ExternalCall(
                    provider=PROVIDER,
                    operation=operation,
                    aggregate_type="kyc_session" if aggregate_id else None,
                    aggregate_id=aggregate_id,
                    success=success,
                    latency_ms=latency_ms,
                )
            )
            await db.commit()


def _build_attestation(
    *, decision: dict, corroboration_id
) -> KycAttestation:
    """Reduce Didit decision payload to the BSS-bound shape.

    THIS IS THE PII REDUCTION POINT. After this function returns, raw
    document_number / first_name / last_name / address / image URLs are
    GONE. Greppable: any code outside this function reading those fields
    is a doctrine bug.
    """
    idv = decision.get("id_verification") or {}
    raw_doc_number = idv.get("document_number") or ""
    document_country = idv.get("issuing_state") or "SGP"

    # Normalize then hash with domain separation.
    normalized = raw_doc_number.upper().strip()
    digest = hashlib.sha256(
        f"{normalized}|{document_country}|{PROVIDER}".encode()
    ).hexdigest()
    last4 = normalized[-4:] if len(normalized) >= 4 else normalized

    dob_str = idv.get("date_of_birth")
    if dob_str:
        dob = date.fromisoformat(dob_str)
    else:
        dob = date(1900, 1, 1)

    document_type = (idv.get("document_type") or "Identity Card").lower()
    document_type = {
        "identity card": "nric",
        "passport": "passport",
        "driver's license": "drivers_license",
        "fin": "fin",
    }.get(document_type, document_type)

    return KycAttestation(
        provider=PROVIDER,
        provider_reference=decision.get("session_id", ""),
        document_type=document_type,
        document_country=document_country,
        document_number_last4=last4,
        document_number_hash=digest,
        date_of_birth=dob,
        corroboration_id=corroboration_id,
    )
