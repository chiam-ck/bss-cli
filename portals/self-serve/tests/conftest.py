"""Shared fixtures — mocked ``get_clients()`` so route tests don't need
a real catalog service. Each test can override the canned offering list
by mutating ``fake_clients.catalog.offerings`` before the request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

# v0.8 — portal lifespan calls ``validate_pepper_present()``. Set a
# fixed test pepper BEFORE importing the app modules so the lifespan
# doesn't fail when TestClient(app) runs the startup hooks.
os.environ.setdefault(
    "BSS_PORTAL_TOKEN_PEPPER",
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
)
# Tests use the in-memory NoopEmailAdapter — no file I/O on /tmp.
# v0.14: force-override (not setdefault) so a developer-sourced .env
# with BSS_PORTAL_EMAIL_PROVIDER=resend doesn't accidentally make the
# test suite hit the real Resend API. setdefault would leave the
# resend value in place.
os.environ["BSS_PORTAL_EMAIL_ADAPTER"] = "noop"
os.environ["BSS_PORTAL_EMAIL_PROVIDER"] = "noop"
# v0.14: force-clear public URL so magic-link tests get bare-token path.
os.environ["BSS_PORTAL_PUBLIC_URL"] = ""
# v0.15: force-override KYC provider to prebaked so the lifespan doesn't
# try to construct a DiditKycAdapter against a real Didit sandbox during
# tests. A developer-sourced .env with BSS_PORTAL_KYC_PROVIDER=didit
# would otherwise wire a real httpx client against verification.didit.me.
os.environ["BSS_PORTAL_KYC_PROVIDER"] = "prebaked"
os.environ["BSS_KYC_ALLOW_PREBAKED"] = "true"

import pytest
from bss_self_serve.config import Settings
from bss_self_serve.main import create_app
from fastapi.testclient import TestClient


# v0.16 — Force payment_provider=mock for the entire test session so
# legacy mock-mode tests don't break when the operator's .env says
# BSS_PAYMENT_PROVIDER=stripe (manual smoke testing). Pydantic-settings
# reads OS env AFTER .env, so a session-scoped override in os.environ
# wins over .env.
#
# Stripe-mode tests opt in by mutating app.state directly via
# `_make_stripe_app` in test_signup_stripe_mode.py — they don't need
# Settings() to return stripe-mode (they patch around it).
@pytest.fixture(autouse=True, scope="session")
def _force_mock_payment_provider_for_tests():
    import os
    prev_provider = os.environ.get("BSS_PAYMENT_PROVIDER")
    prev_key = os.environ.get("BSS_PAYMENT_STRIPE_API_KEY")
    os.environ["BSS_PAYMENT_PROVIDER"] = "mock"
    os.environ.pop("BSS_PAYMENT_STRIPE_API_KEY", None)
    yield
    if prev_provider is not None:
        os.environ["BSS_PAYMENT_PROVIDER"] = prev_provider
    else:
        os.environ.pop("BSS_PAYMENT_PROVIDER", None)
    if prev_key is not None:
        os.environ["BSS_PAYMENT_STRIPE_API_KEY"] = prev_key


@dataclass
class FakeCatalog:
    offerings: list[dict[str, Any]] = field(default_factory=list)
    vas_offerings: list[dict[str, Any]] = field(default_factory=list)
    # v1.1 — promo preview/entitlement reads. Tests seed these.
    promo_preview: dict[str, Any] = field(
        default_factory=lambda: {"valid": False, "reason": None}
    )
    customer_offers: dict[str, Any] = field(
        default_factory=lambda: {"offers": []}
    )
    assigned_offer: dict[str, Any] = field(
        default_factory=lambda: {"valid": False, "reason": "no_applicable_offer"}
    )

    async def list_offerings(self) -> list[dict[str, Any]]:
        return list(self.offerings)

    async def list_vas(self) -> list[dict[str, Any]]:
        return list(self.vas_offerings)

    async def list_active_offerings(self, *, at: Any = None) -> list[dict[str, Any]]:
        # v0.7 active-as-of query — for tests, return everything seeded.
        return list(self.offerings)

    async def preview_promo(
        self, *, code: str, offering: str, customer_id: str | None = None
    ) -> dict[str, Any]:
        return dict(self.promo_preview)

    async def list_customer_offers(
        self, *, customer_id: str, state: str | None = None
    ) -> dict[str, Any]:
        return dict(self.customer_offers)

    async def resolve_eligible_promo(
        self, *, customer_id: str, offering: str
    ) -> dict[str, Any]:
        return dict(self.assigned_offer)


@dataclass
class FakeSubscription:
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_customer: dict[str, list[str]] = field(default_factory=dict)
    balances: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    purchase_vas_calls: list[tuple[str, str]] = field(default_factory=list)
    terminate_calls: list[tuple[str, str | None]] = field(default_factory=list)
    schedule_plan_change_calls: list[tuple[str, str]] = field(default_factory=list)
    cancel_plan_change_calls: list[str] = field(default_factory=list)
    # v0.10 — tests pre-seed an exception here to simulate a server-side
    # PolicyViolation on the next call to ``purchase_vas`` etc.
    next_error: Exception | None = None

    async def get(self, subscription_id: str) -> dict[str, Any]:
        if subscription_id not in self.records:
            raise KeyError(subscription_id)
        return dict(self.records[subscription_id])

    async def list_for_customer(self, customer_id: str) -> list[dict[str, Any]]:
        ids = self.by_customer.get(customer_id, [])
        return [dict(self.records[i]) for i in ids if i in self.records]

    async def get_balance(self, subscription_id: str) -> list[dict[str, Any]]:
        return [dict(b) for b in self.balances.get(subscription_id, [])]

    async def purchase_vas(
        self, subscription_id: str, vas_offering_id: str
    ) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        self.purchase_vas_calls.append((subscription_id, vas_offering_id))
        if subscription_id in self.records:
            return dict(self.records[subscription_id])
        raise KeyError(subscription_id)

    async def terminate(
        self, subscription_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        if subscription_id not in self.records:
            raise KeyError(subscription_id)
        # Record the call for assertion + flip state to mirror server-side.
        self.terminate_calls.append((subscription_id, reason))
        rec = self.records[subscription_id]
        rec["state"] = "terminated"
        rec["terminatedAt"] = "2026-04-27T00:00:00+00:00"
        return dict(rec)

    async def schedule_plan_change(
        self, subscription_id: str, new_offering_id: str
    ) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        if subscription_id not in self.records:
            raise KeyError(subscription_id)
        self.schedule_plan_change_calls.append((subscription_id, new_offering_id))
        rec = self.records[subscription_id]
        rec["pendingOfferingId"] = new_offering_id
        rec["pendingEffectiveAt"] = rec.get("nextRenewalAt") or (
            "2026-05-27T00:00:00+00:00"
        )
        return dict(rec)

    async def cancel_plan_change(self, subscription_id: str) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        if subscription_id not in self.records:
            raise KeyError(subscription_id)
        self.cancel_plan_change_calls.append(subscription_id)
        rec = self.records[subscription_id]
        rec["pendingOfferingId"] = None
        rec["pendingEffectiveAt"] = None
        return dict(rec)


@dataclass
class FakeInventory:
    activations: dict[str, dict[str, Any]] = field(default_factory=dict)
    msisdns: list[dict[str, Any]] = field(default_factory=list)
    next_error: Exception | None = None

    async def get_activation_code(self, iccid: str) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        if iccid not in self.activations:
            raise KeyError(iccid)
        return dict(self.activations[iccid])

    async def list_msisdns(
        self,
        *,
        state: str | None = None,
        prefix: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        out = list(self.msisdns)
        if state:
            out = [n for n in out if n.get("status") == state]
        if prefix:
            out = [n for n in out if n["msisdn"].startswith(prefix)]
        return out[:limit]


@dataclass
class FakeCOM:
    """v0.10: order read fixture (used by the activation-poll route).

    v0.11 PR 2 extended this to cover the direct-write signup chain —
    create_order + submit_order. Tests pre-seed ``next_order_state`` to
    drive the get_order poll through ``acknowledged → in_progress →
    completed``; ``next_subscription_id`` lets the completed branch
    surface a SUB-* on the order envelope so the route can extract it.
    """

    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    submit_calls: list[str] = field(default_factory=list)
    next_error: Exception | None = None
    # Sequence of states get_order returns on each call (left-to-right).
    next_order_states: list[str] = field(default_factory=list)
    next_subscription_id: str | None = None
    next_activation_code: str | None = None

    async def get_order(self, order_id: str) -> dict[str, Any]:
        if order_id not in self.orders:
            raise KeyError(order_id)
        rec = dict(self.orders[order_id])
        if self.next_order_states:
            rec["state"] = self.next_order_states.pop(0)
            self.orders[order_id]["state"] = rec["state"]
        # On completed, surface SUB-* + activation code on the envelope so
        # the signup poll route extracts them. Mirrors COM's contract:
        # items[*].subscriptionId is set the moment SOM activation lands.
        if rec.get("state") == "completed":
            sub = self.next_subscription_id or self.orders[order_id].get(
                "subscriptionId"
            )
            if sub:
                items = rec.setdefault("items", [{}])
                items[0]["subscriptionId"] = sub
                rec["subscriptionId"] = sub
            ac = self.next_activation_code or self.orders[order_id].get(
                "activationCode"
            )
            if ac:
                rec["activationCode"] = ac
        return rec

    async def create_order(
        self,
        *,
        customer_id: str,
        offering_id: str,
        msisdn_preference: str | None = None,
        notes: str | None = None,
        discount_code: str | None = None,
        skip_assigned_offer: bool = False,
    ) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        record = {
            "customer_id": customer_id,
            "offering_id": offering_id,
            "msisdn_preference": msisdn_preference,
            "discount_code": discount_code,
            "skip_assigned_offer": skip_assigned_offer,
        }
        self.create_calls.append(record)
        new_id = f"ORD-{len(self.create_calls):04d}"
        self.orders[new_id] = {
            "id": new_id,
            "customerId": customer_id,
            "offeringId": offering_id,
            "state": "acknowledged",
            "items": [{"offeringId": offering_id, "msisdnPreference": msisdn_preference}],
        }
        return dict(self.orders[new_id])

    async def submit_order(self, order_id: str) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        self.submit_calls.append(order_id)
        if order_id in self.orders:
            self.orders[order_id]["state"] = "in_progress"
            return dict(self.orders[order_id])
        raise KeyError(order_id)


@dataclass
class FakeCRM:
    """v0.10 PR 8 — CRM client fake for contact-medium routes.

    Pre-seed by setting ``mediums_by_customer[customer_id] = [{...}, ...]``.
    Each medium dict mirrors the camelCase TMF629 ContactMedium shape:
    {id, mediumType, value, isPrimary, validFrom}. v0.10 patch — also
    seeds ``individual_by_customer[customer_id]`` for the name-update
    surface.
    """

    mediums_by_customer: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    individual_by_customer: dict[str, dict[str, str]] = field(default_factory=dict)
    update_calls: list[tuple[str, str, str]] = field(default_factory=list)
    individual_update_calls: list[tuple[str, str | None, str | None]] = field(
        default_factory=list
    )
    # v0.11 — direct-write signup chain calls create_customer + attest_kyc.
    # Pre-seed ``next_error`` to simulate a server-side PolicyViolation;
    # ``next_customer_id`` controls the CUST-* id returned by create_customer.
    create_customer_calls: list[dict[str, Any]] = field(default_factory=list)
    attest_kyc_calls: list[dict[str, Any]] = field(default_factory=list)
    next_customer_id: str | None = None
    next_error: Exception | None = None

    async def create_customer(
        self,
        *,
        name: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        record = {"name": name, "email": email, "phone": phone}
        self.create_customer_calls.append(record)
        cid = self.next_customer_id or f"CUST-{len(self.create_customer_calls):03d}"
        self.next_customer_id = None
        # Seed an empty mediums row so the v0.10 contact-medium fakes
        # find the customer if a follow-up read happens. Mirrors the
        # CRM service shape.
        self.mediums_by_customer.setdefault(cid, [])
        return {"id": cid, "givenName": name.split()[0], "familyName": name}

    async def attest_kyc(
        self,
        customer_id: str,
        *,
        provider: str,
        attestation_token: str,
        provider_reference: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        self.attest_kyc_calls.append(
            {
                "customer_id": customer_id,
                "provider": provider,
                "attestation_token": attestation_token,
                "provider_reference": provider_reference,
            }
        )
        return {
            "customerId": customer_id,
            "provider": provider,
            "status": "verified",
            "verifiedAt": "2026-04-27T00:00:00+00:00",
        }

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        ind = self.individual_by_customer.get(customer_id, {})
        return {
            "id": customer_id,
            "individual": (
                {
                    "givenName": ind.get("given_name", ""),
                    "familyName": ind.get("family_name", ""),
                }
                if ind
                else None
            ),
            "contactMedium": [
                dict(cm) for cm in self.mediums_by_customer.get(customer_id, [])
            ],
        }

    async def update_individual(
        self,
        customer_id: str,
        *,
        given_name: str | None = None,
        family_name: str | None = None,
    ) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        self.individual_update_calls.append(
            (customer_id, given_name, family_name)
        )
        ind = self.individual_by_customer.setdefault(customer_id, {})
        if given_name is not None:
            ind["given_name"] = given_name
        if family_name is not None:
            ind["family_name"] = family_name
        return await self.get_customer(customer_id)

    async def list_contact_mediums(
        self, customer_id: str
    ) -> list[dict[str, Any]]:
        return [dict(cm) for cm in self.mediums_by_customer.get(customer_id, [])]

    async def update_contact_medium(
        self, customer_id: str, medium_id: str, *, value: str
    ) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        self.update_calls.append((customer_id, medium_id, value))
        for cm in self.mediums_by_customer.get(customer_id, []):
            if cm["id"] == medium_id:
                cm["value"] = value
                return dict(cm)
        raise KeyError(medium_id)


@dataclass
class FakePayment:
    methods_by_customer: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    payments_by_customer: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    remove_calls: list[str] = field(default_factory=list)
    set_default_calls: list[str] = field(default_factory=list)
    next_error: Exception | None = None

    async def list_payments(
        self,
        *,
        customer_id: str | None = None,
        payment_method_id: str | None = None,
        limit: int = 20,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.payments_by_customer.get(customer_id or "", []))
        if payment_method_id:
            rows = [r for r in rows if r.get("paymentMethodId") == payment_method_id]
        start = offset or 0
        return [dict(r) for r in rows[start : start + (limit or len(rows))]]

    async def count_payments(self, *, customer_id: str) -> int:
        return len(self.payments_by_customer.get(customer_id, []))

    async def list_methods(self, customer_id: str) -> list[dict[str, Any]]:
        return [dict(m) for m in self.methods_by_customer.get(customer_id, [])]

    async def create_payment_method(
        self,
        *,
        customer_id: str,
        card_token: str,
        last4: str,
        brand: str,
        exp_month: int = 12,
        exp_year: int = 2030,
        tokenization_provider: str = "sandbox",
        country: str | None = "SG",
    ) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        record = {
            "customer_id": customer_id,
            "card_token": card_token,
            "last4": last4,
            "brand": brand,
            "exp_month": exp_month,
            "exp_year": exp_year,
        }
        self.create_calls.append(record)
        new_id = f"PM-{len(self.create_calls):04d}"
        existing = self.methods_by_customer.setdefault(customer_id, [])
        is_default = len(existing) == 0
        method = {
            "id": new_id,
            "customerId": customer_id,
            "brand": brand,
            "last4": last4,
            "expMonth": exp_month,
            "expYear": exp_year,
            "isDefault": is_default,
        }
        existing.append(method)
        return dict(method)

    async def remove_method(self, method_id: str) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        self.remove_calls.append(method_id)
        for cust_id, methods in self.methods_by_customer.items():
            for m in methods:
                if m["id"] == method_id:
                    methods.remove(m)
                    return {"id": method_id, "removed": True}
        raise KeyError(method_id)

    async def set_default_method(self, method_id: str) -> dict[str, Any]:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err
        self.set_default_calls.append(method_id)
        for cust_id, methods in self.methods_by_customer.items():
            target = next((m for m in methods if m["id"] == method_id), None)
            if target is not None:
                for m in methods:
                    m["isDefault"] = (m["id"] == method_id)
                return dict(target)
        raise KeyError(method_id)


@dataclass
class FakeClientsBundle:
    catalog: FakeCatalog = field(default_factory=FakeCatalog)
    subscription: FakeSubscription = field(default_factory=FakeSubscription)
    inventory: FakeInventory = field(default_factory=FakeInventory)
    com: FakeCOM = field(default_factory=FakeCOM)
    # v0.10 — added so post-login route tests can pre-seed responses.
    payment: FakePayment = field(default_factory=FakePayment)
    provisioning: Any = None
    crm: FakeCRM = field(default_factory=FakeCRM)


SAMPLE_VAS = [
    {
        "id": "VAS_DATA_1GB",
        "name": "Data Top-Up 1GB",
        "priceAmount": 3.00,
        "currency": "SGD",
        "allowanceType": "data",
        "allowanceQuantity": 1024,
        "allowanceUnit": "mb",
        "expiryHours": None,
    },
    {
        "id": "VAS_DATA_5GB",
        "name": "Data Top-Up 5GB",
        "priceAmount": 12.00,
        "currency": "SGD",
        "allowanceType": "data",
        "allowanceQuantity": 5120,
        "allowanceUnit": "mb",
        "expiryHours": None,
    },
    {
        "id": "VAS_UNLIMITED_DAY",
        "name": "Unlimited Data Day Pass",
        "priceAmount": 5.00,
        "currency": "SGD",
        "allowanceType": "data",
        "allowanceQuantity": -1,
        "allowanceUnit": "mb",
        "expiryHours": 24,
    },
]


SAMPLE_OFFERINGS = [
    {
        "id": "PLAN_S",
        "name": "Sidekick",
        "productOfferingPrice": [
            {"price": {"taxIncludedAmount": {"value": 15, "unit": "SGD"}}}
        ],
        "bundleAllowance": [
            {"type": "data", "total": 5120, "unit": "mb"},
            {"type": "voice", "total": 100, "unit": "min"},
            {"type": "sms", "total": 100, "unit": "sms"},
        ],
    },
    {
        "id": "PLAN_M",
        "name": "Mainline",
        "productOfferingPrice": [
            {"price": {"taxIncludedAmount": {"value": 25, "unit": "SGD"}}}
        ],
        "bundleAllowance": [
            {"type": "data", "total": 20480, "unit": "mb"},
            {"type": "voice", "total": 300, "unit": "min"},
            {"type": "sms", "total": -1, "unit": "sms"},
        ],
    },
    {
        "id": "PLAN_L",
        "name": "Long Haul",
        "productOfferingPrice": [
            {"price": {"taxIncludedAmount": {"value": 45, "unit": "SGD"}}}
        ],
        "bundleAllowance": [
            {"type": "data", "total": -1, "unit": "mb"},
            {"type": "voice", "total": -1, "unit": "min"},
            {"type": "sms", "total": -1, "unit": "sms"},
        ],
    },
]


@pytest.fixture
def fake_clients() -> FakeClientsBundle:
    bundle = FakeClientsBundle()
    bundle.catalog.offerings = list(SAMPLE_OFFERINGS)
    bundle.catalog.vas_offerings = list(SAMPLE_VAS)
    bundle.inventory.msisdns = [
        {"msisdn": f"9000000{i}", "status": "available", "reserved_at": None}
        for i in range(2, 8)
    ] + [
        {"msisdn": "90000010", "status": "available", "reserved_at": None},
        # an already-assigned one to prove the status filter works
        {"msisdn": "90000001", "status": "assigned", "reserved_at": "2026-04-23T00:00:00Z"},
    ]
    return bundle


@pytest.fixture
def client(fake_clients: FakeClientsBundle):
    # Patch get_clients at every import site the routes use. Using
    # ``create=False`` so we don't accidentally create missing attrs.
    # routes/landing.py is the dashboard now (no catalog reads); the
    # public plan-card browse moved to routes/welcome.py at /plans.
    with patch("bss_self_serve.routes.welcome.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.signup.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.activation.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.confirmation.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.msisdn_picker.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.landing.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.top_up.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.payment_methods.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.esim.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.cancel.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.profile.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.billing.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients):
        app = create_app(Settings())
        with TestClient(app) as c:
            yield c


@pytest.fixture
def authed_client(fake_clients: FakeClientsBundle):
    """TestClient with a pre-attached verified-email session cookie.

    v0.8 — the signup funnel routes (/signup/*, /agent/events/*) require
    an identity. This fixture seeds one via a separate setup engine, then
    attaches the session id as a cookie. Tests that hit gated routes
    use this fixture; tests for public surfaces use ``client``.

    Implementation note: we have to seed the session on a different
    engine than the app's lifespan engine because TestClient spins each
    test on a fresh asyncio loop and asyncpg connections are loop-bound.
    """
    import asyncio
    import os
    from pathlib import Path

    from bss_portal_auth.test_helpers import create_test_session
    from bss_self_serve.middleware import PORTAL_SESSION_COOKIE
    from pydantic_settings import BaseSettings, SettingsConfigDict
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    repo_root = Path(__file__).resolve().parents[3]

    class _DbSettings(BaseSettings):
        BSS_DB_URL: str = ""
        model_config = SettingsConfigDict(
            env_file=repo_root / ".env",
            env_file_encoding="utf-8",
            extra="ignore",
        )

    db_url = _DbSettings().BSS_DB_URL or os.environ.get("BSS_DB_URL", "")
    if not db_url:
        pytest.fail("BSS_DB_URL is not set. Export it or add to .env.")
    os.environ["BSS_DB_URL"] = db_url

    async def _seed():
        engine = create_async_engine(db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            await s.execute(text(
                "TRUNCATE portal_auth.login_attempt, portal_auth.session, "
                "portal_auth.login_token, portal_auth.identity RESTART IDENTITY CASCADE"
            ))
            await s.commit()
        async with factory() as s:
            sess, identity = await create_test_session(
                s, email="ada@example.sg", verified=True
            )
            await s.commit()
            sid = sess.id
            iid = identity.id
        await engine.dispose()
        return sid, iid

    async def _scrub():
        engine = create_async_engine(db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            await s.execute(text(
                "TRUNCATE portal_auth.login_attempt, portal_auth.session, "
                "portal_auth.login_token, portal_auth.identity RESTART IDENTITY CASCADE"
            ))
            await s.commit()
        await engine.dispose()

    session_id, identity_id = asyncio.run(_seed())

    # routes/landing.py is the dashboard now (no catalog reads); the
    # public plan-card browse moved to routes/welcome.py at /plans.
    with patch("bss_self_serve.routes.welcome.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.signup.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.activation.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.confirmation.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.msisdn_picker.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.landing.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.top_up.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.payment_methods.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.esim.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.cancel.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.profile.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.billing.get_clients", return_value=fake_clients), \
         patch("bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, session_id)
            # Stash for tests that want to assert against the seeded ids.
            c.app.state.test_identity_id = identity_id
            c.app.state.test_session_id = session_id
            yield c

    asyncio.run(_scrub())
