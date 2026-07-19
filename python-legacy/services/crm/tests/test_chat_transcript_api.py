"""Chat transcript API + chat_transcript_hash on case (v0.12 PR6)."""

from __future__ import annotations

import hashlib

from httpx import AsyncClient

CASE_PREFIX = "/crm-api/v1"
CUST_PREFIX = "/tmf-api/customerManagement/v4"


async def _create_customer(client: AsyncClient, suffix: str = "") -> str:
    r = await client.post(
        f"{CUST_PREFIX}/customer",
        json={
            "givenName": "Chat",
            "familyName": f"Transcript{suffix}",
            "contactMedium": [
                {
                    "medium_type": "email",
                    "value": f"chat{suffix}@example.com",
                }
            ],
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


class TestChatTranscriptApi:
    async def test_store_then_fetch_round_trip(self, client: AsyncClient) -> None:
        body = "User: hi\nAssistant: hi back\n"
        h = hashlib.sha256(body.encode("utf-8")).hexdigest()
        cust_id = await _create_customer(client, ".store")

        r = await client.post(
            f"{CASE_PREFIX}/chat-transcript",
            json={"hash": h, "customer_id": cust_id, "body": body},
        )
        assert r.status_code == 201
        assert r.json()["hash"] == h

        r = await client.get(f"{CASE_PREFIX}/chat-transcript/{h}")
        assert r.status_code == 200
        assert r.json()["body"] == body
        assert r.json()["customer_id"] == cust_id

    async def test_idempotent_store_does_not_error_on_second_post(
        self, client: AsyncClient
    ) -> None:
        body = "User: same conv\n"
        h = hashlib.sha256(body.encode("utf-8")).hexdigest()
        cust_id = await _create_customer(client, ".idem")

        for _ in range(2):
            r = await client.post(
                f"{CASE_PREFIX}/chat-transcript",
                json={"hash": h, "customer_id": cust_id, "body": body},
            )
            assert r.status_code == 201

        r = await client.get(f"{CASE_PREFIX}/chat-transcript/{h}")
        assert r.status_code == 200
        assert r.json()["body"] == body

    async def test_hash_mismatch_rejected(self, client: AsyncClient) -> None:
        cust_id = await _create_customer(client, ".mismatch")
        r = await client.post(
            f"{CASE_PREFIX}/chat-transcript",
            json={
                "hash": "deadbeef" * 8,
                "customer_id": cust_id,
                "body": "different body",
            },
        )
        assert r.status_code == 422
        assert r.json()["reason"] == "chat_transcript.hash_mismatch"

    async def test_get_unknown_hash_returns_404(self, client: AsyncClient) -> None:
        r = await client.get(f"{CASE_PREFIX}/chat-transcript/{'0' * 64}")
        assert r.status_code == 404


class TestCaseWithTranscriptHash:
    async def test_open_case_carries_transcript_hash_through_to_response(
        self, client: AsyncClient
    ) -> None:
        body = "User: I dispute this charge\n"
        h = hashlib.sha256(body.encode("utf-8")).hexdigest()
        cust_id = await _create_customer(client, ".linked")

        # First POST the transcript so the hash resolves.
        r = await client.post(
            f"{CASE_PREFIX}/chat-transcript",
            json={"hash": h, "customer_id": cust_id, "body": body},
        )
        assert r.status_code == 201

        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={
                "customer_id": cust_id,
                "subject": "Dispute charge",
                "category": "billing",
                "priority": "medium",
                "description": "[billing_dispute] customer disputes 2026-04-25 charge",
                "chat_transcript_hash": h,
            },
        )
        assert r.status_code == 201
        case = r.json()
        assert case["chat_transcript_hash"] == h

        # GET roundtrip preserves the hash.
        r = await client.get(f"{CASE_PREFIX}/case/{case['id']}")
        assert r.status_code == 200
        assert r.json()["chat_transcript_hash"] == h

    async def test_open_case_without_transcript_hash_unchanged(
        self, client: AsyncClient
    ) -> None:
        cust_id = await _create_customer(client, ".no-link")
        r = await client.post(
            f"{CASE_PREFIX}/case",
            json={"customer_id": cust_id, "subject": "Plain CSR-opened case"},
        )
        assert r.status_code == 201
        assert r.json().get("chat_transcript_hash") is None
