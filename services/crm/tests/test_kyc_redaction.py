"""End-to-end KYC redaction test.

One POST to /kyc-attestation with plaintext NRIC "S9999999Z", then three
leak-path assertions:
  a) structlog output — plaintext NOT in captured stdout
  b) audit.domain_event payload — plaintext NOT in event JSON
  c) crm.customer_identity — hash column matches sha256("S9999999Z"),
     plaintext NOT in any column value
"""

import hashlib
import json

from bss_models.audit import DomainEvent
from bss_models.crm import CustomerIdentity
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

CUST_PREFIX = "/tmf-api/customerManagement/v4"
KYC_PREFIX = "/crm-api/v1"
PLAINTEXT_NRIC = "S9999999Z"
# v0.15: hash is domain-separated (number | country | provider) to prevent
# cross-provider/country collision. The test posts with country="SG" and
# provider="myinfo".
EXPECTED_HASH = hashlib.sha256(f"{PLAINTEXT_NRIC}|SG|myinfo".encode()).hexdigest()


async def _create_customer(client: AsyncClient) -> str:
    r = await client.post(
        f"{CUST_PREFIX}/customer",
        json={
            "givenName": "Redaction",
            "familyName": "Test",
            "contactMedium": [
                {"medium_type": "email", "value": "redaction.test@example.com"}
            ],
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


class TestKycRedaction:
    async def test_plaintext_nric_never_leaks(
        self, client: AsyncClient, db_session: AsyncSession, capsys
    ):
        """One POST, three leak-path checks."""
        # Setup: create customer
        cust_id = await _create_customer(client)

        # POST KYC attestation with plaintext NRIC
        r = await client.post(
            f"{KYC_PREFIX}/customer/{cust_id}/kyc-attestation",
            json={
                "provider": "myinfo",
                "provider_reference": "myinfo-tx-redaction-test",
                "document_type": "nric",
                "document_number": PLAINTEXT_NRIC,
                "document_country": "SG",
                "date_of_birth": "1985-03-15",
                "nationality": "SG",
                "verified_at": "2026-04-11T09:15:00+08:00",
                "attestation_payload": {
                    "issuer": "singpass.gov.sg",
                    "subject_id": "test",
                    "claims": {"name": "Redaction Test"},
                    "signature": "eyJhbGc...",
                },
            },
        )
        assert r.status_code == 200, f"KYC attestation failed: {r.text}"
        assert r.json()["kyc_status"] == "verified"

        # ── Check (a): structlog output ─────────────────────────────
        # v0.15 doctrine: the API endpoint never logs document_number
        # at all (raw OR redacted form). Earlier versions relied on the
        # structlog redaction processor as a defense-in-depth marker;
        # v0.15 amendment 2 removed the log call outright since the
        # safer posture is "never write the value to the log pipeline
        # in the first place." Redaction stays in place as backstop for
        # any incidental leak through other paths.
        captured = capsys.readouterr()
        all_output = captured.out + captured.err
        assert PLAINTEXT_NRIC not in all_output, (
            f"LEAK: plaintext NRIC '{PLAINTEXT_NRIC}' found in log output"
        )

        # ── Check (b): audit.domain_event payload ───────────────────
        result = await db_session.execute(
            select(DomainEvent).where(
                DomainEvent.aggregate_type == "customer",
                DomainEvent.event_type == "customer.kyc_attested",
                DomainEvent.aggregate_id == cust_id,
            )
        )
        events = list(result.scalars().all())
        assert len(events) >= 1, "No kyc_attested event found in audit.domain_event"
        for event in events:
            payload_str = json.dumps(event.payload)
            assert PLAINTEXT_NRIC not in payload_str, (
                f"LEAK: plaintext NRIC found in audit.domain_event payload: {payload_str}"
            )

        # ── Check (c): crm.customer_identity columns ────────────────
        result = await db_session.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.customer_id == cust_id
            )
        )
        identity = result.scalar_one_or_none()
        assert identity is not None, "customer_identity row not created"

        # Hash column must match sha256(NRIC)
        assert identity.document_number_hash == EXPECTED_HASH, (
            f"Hash mismatch: expected {EXPECTED_HASH}, got {identity.document_number_hash}"
        )

        # Scan ALL column values for plaintext leak
        for col in [
            identity.customer_id,
            identity.document_type,
            identity.document_number_hash,
            identity.document_country,
            str(identity.date_of_birth),
            identity.nationality,
            identity.verified_by,
            json.dumps(identity.attestation_payload) if identity.attestation_payload else "",
            str(identity.verified_at),
        ]:
            assert PLAINTEXT_NRIC not in str(col), (
                f"LEAK: plaintext NRIC found in customer_identity column value: {col}"
            )
