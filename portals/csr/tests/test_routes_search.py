"""Search route — query-shape routing (v1.6.2).

The search box resolves three query shapes: MSISDN → find_by_msisdn,
email → find_by_email (new), everything else → list(name_contains=q).
The email lane exists because name_contains is a LIKE on the display
name only — pasting an email used to return zero results silently
(same gap that looped the cockpit agent on 2026-06-12).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from bss_clients.errors import ClientError
from bss_csr.config import Settings
from bss_csr.main import create_app
from fastapi.testclient import TestClient


def _tmf_customer(customer_id: str = "CUST-em01", email: str = "jane+1@example.com") -> dict[str, Any]:
    return {
        "id": customer_id,
        "status": "active",
        "kycStatus": "verified",
        "individual": {"givenName": "Jane", "familyName": "Doe"},
        "contactMedium": [
            {"mediumType": "email", "value": email},
            {"mediumType": "mobile", "value": "90001234"},
        ],
    }


@dataclass
class _FakeSearchCRM:
    by_email: dict[str, dict[str, Any]] = field(default_factory=dict)
    email_queries: list[str] = field(default_factory=list)
    name_queries: list[str] = field(default_factory=list)

    async def find_customer_by_email(self, email: str) -> dict[str, Any]:
        self.email_queries.append(email)
        if email not in self.by_email:
            raise ClientError(404, f"No customer has email {email}")
        return dict(self.by_email[email])

    async def find_customer_by_msisdn(self, msisdn: str) -> dict[str, Any]:
        raise ClientError(404, f"No customer owns MSISDN {msisdn}")

    async def list_customers(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.name_queries.append(kwargs.get("name_contains", ""))
        return []


@dataclass
class _FakeBundle:
    crm: _FakeSearchCRM = field(default_factory=_FakeSearchCRM)


@pytest.fixture
def fake_search_clients() -> _FakeBundle:
    return _FakeBundle()


@pytest.fixture
def search_client(fake_search_clients: _FakeBundle, monkeypatch):
    # Same lifespan dodge as case_client: the store engine is built
    # lazily, so a syntactically valid URL is enough — search never
    # touches the DB.
    monkeypatch.setenv(
        "BSS_DB_URL",
        "postgresql+asyncpg://bss:bss_password@localhost:5432/bss",
    )
    with patch(
        "bss_csr.routes.search.get_clients", return_value=fake_search_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            yield c


def test_email_query_routes_to_find_by_email(
    search_client, fake_search_clients: _FakeBundle
) -> None:
    email = "jane+1@example.com"
    fake_search_clients.crm.by_email[email] = _tmf_customer(email=email)

    r = search_client.get("/search", params={"q": email})
    assert r.status_code == 200
    assert "CUST-em01" in r.text
    # The plus-addressed email reached the client verbatim and the
    # name lane was never consulted.
    assert fake_search_clients.crm.email_queries == [email]
    assert fake_search_clients.crm.name_queries == []


def test_unknown_email_renders_empty_results_not_500(
    search_client, fake_search_clients: _FakeBundle
) -> None:
    r = search_client.get("/search", params={"q": "nobody@nowhere.example"})
    assert r.status_code == 200
    assert "CUST-" not in r.text


def test_name_query_still_routes_to_list(
    search_client, fake_search_clients: _FakeBundle
) -> None:
    r = search_client.get("/search", params={"q": "Jane"})
    assert r.status_code == 200
    assert fake_search_clients.crm.name_queries == ["Jane"]
    assert fake_search_clients.crm.email_queries == []
