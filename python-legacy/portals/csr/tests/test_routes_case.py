"""Case detail page — read-only deep link (v0.13).

The case route was the one v0.5 surface kept into v0.13: useful for
copy-paste case-id deep links from chat sessions, slack, runbooks.
No login dependency anymore. Carries forward the v0.12 chat-transcript
panel for cases opened via ``case.open_for_me``.
"""

from __future__ import annotations

from bss_clients.errors import ClientError
from conftest import FakeBundle  # type: ignore[import-not-found]


def _seed_case(fake_clients: FakeBundle, **overrides) -> None:
    fake_clients.crm.case_raw = {
        "id": "CASE-042",
        "subject": "Data not working",
        "state": "open",
        "priority": "high",
        "category": "technical",
        "agentId": "csr-demo-001",
        "customerId": "CUST-test01",
        "createdAt": "2026-04-23T01:00:00Z",
        "tickets": [
            {
                "id": "TKT-101",
                "ticketType": "subscription",
                "subject": "Bundle exhausted",
                "state": "open",
                "agentId": "csr-demo-001",
            },
        ],
        "notes": [
            {
                "id": "NOTE-1",
                "body": "Customer called; investigating.",
                "createdBy": "csr-demo-001",
                "createdAt": "2026-04-23T01:05:00Z",
            },
        ],
        **overrides,
    }


def test_case_thread_renders_header_tickets_and_notes(
    case_client, fake_clients: FakeBundle
) -> None:
    _seed_case(fake_clients)
    r = case_client.get("/case/CASE-042")
    assert r.status_code == 200
    body = r.text
    assert "CASE-042" in body
    assert "Data not working" in body
    assert "TKT-101" in body
    assert "Customer called; investigating." in body


def test_case_thread_unknown_case_404s(
    case_client, fake_clients: FakeBundle
) -> None:
    fake_clients.crm.case_404 = True
    r = case_client.get("/case/CASE-NOPE")
    assert r.status_code == 404


def test_case_thread_renders_transcript_panel_when_hash_present(
    case_client, fake_clients: FakeBundle
) -> None:
    _seed_case(
        fake_clients,
        chatTranscriptHash="abcdef0123456789" * 4,
    )
    fake_clients.crm.transcripts[
        "abcdef0123456789" * 4
    ] = {
        "hash": "abcdef0123456789" * 4,
        "body": "user: my data is gone\nassistant: opened case",
        "recorded_at": "2026-04-23T01:00:00Z",
    }
    r = case_client.get("/case/CASE-042")
    assert r.status_code == 200
    assert "Chat transcript" in r.text
    assert "user: my data is gone" in r.text


def test_case_thread_no_transcript_section_when_hash_absent(
    case_client, fake_clients: FakeBundle
) -> None:
    _seed_case(fake_clients)
    body = case_client.get("/case/CASE-042").text
    assert "Chat transcript" not in body


def test_case_thread_transcript_fetch_error_renders_empty_state(
    case_client, fake_clients: FakeBundle
) -> None:
    _seed_case(
        fake_clients,
        chatTranscriptHash="abcdef0123456789" * 4,
    )
    fake_clients.crm.transcript_get_error = ClientError(503, "down")
    r = case_client.get("/case/CASE-042")
    assert r.status_code == 200
    assert "no longer retrievable" in r.text
