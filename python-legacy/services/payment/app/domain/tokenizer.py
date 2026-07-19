"""TokenizerAdapter Protocol — payment provider seam (v0.16).

The Protocol shape mirrors v0.14's email adapter and v0.15's KYC adapter
doctrine: per-domain Protocols, no unified ``Provider.execute()``. Each
provider domain has genuinely different shapes; lowest-common-denominator
APIs erase information consumers need.

The four concrete operations the payment service performs against any
tokenization provider:

- ``charge`` — debit a saved token for an amount. Idempotency key on
  every call so BSS-crash-restart retries dedupe at the provider.
- ``tokenize`` — server-side tokenization of a raw PAN. Mock-only
  affordance; the production ``StripeTokenizerAdapter`` raises
  ``NotImplementedError`` because PAN never touches BSS in production.
  The portal uses Stripe.js + Elements client-side instead.
- ``attach_payment_method_to_customer`` — bind a payment method id to a
  provider-side customer record. Stripe requires this before the
  off-session ``confirm=True`` charge can succeed; mock is a no-op.
- ``ensure_customer`` — upsert a provider-side customer keyed on the
  BSS customer id. Returns the provider-side reference (``cus_*`` for
  Stripe). The result is cached in ``payment.customer.customer_external_ref``
  so subsequent charges skip the round-trip.

The Protocol intentionally avoids Stripe-specific terms in its API
surface (no ``PaymentIntent``, no ``SetupIntent``) so a future
``AdyenTokenizerAdapter`` or similar can implement it without a leaky
abstraction. v0.16 ships ``MockTokenizerAdapter`` + ``StripeTokenizerAdapter``;
Adyen is reserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TokenizeResult:
    """Result of a server-side tokenize call (mock dev affordance only).

    In production the portal uses Stripe.js + Elements client-side and
    BSS receives a ``pm_*`` id directly via the portal callback — no
    server-side tokenization happens, so this dataclass is only ever
    populated by ``MockTokenizerAdapter.tokenize``.
    """

    token: str
    last4: str
    brand: str


@dataclass(frozen=True)
class ChargeResult:
    """Result of a charge attempt against the tokenization provider.

    ``status``: ``"approved"`` (committed), ``"declined"`` (provider
    rejected), or ``"errored"`` (network/transport error; the BSS row
    is left in a recoverable state and reconciliation can retry).

    ``gateway_ref``: BSS-friendly opaque reference. For mock this is
    ``mock_<uuid4>``. For Stripe this is the ``pi_*`` PaymentIntent id
    (also recorded in ``provider_call_id`` for forensic queries).

    ``reason``: human-readable. For mock, ``"card_declined_by_issuer"``;
    for Stripe, the message-side description from the PaymentIntent's
    ``last_payment_error.message``.

    ``provider_call_id``: provider-side primary key for the call. ``pi_*``
    for Stripe, ``mock_<uuid4>`` for mock. Recorded in
    ``payment_attempt.provider_call_id`` for `bss external-calls` lookup.

    ``decline_code``: Stripe's machine-readable decline taxonomy
    (``insufficient_funds``, ``card_declined``, etc.). Passed through
    unchanged so downstream code can branch on a stable identifier
    rather than parsing the human ``reason``. ``None`` when the call
    didn't decline. Mock currently returns ``None`` for non-declines and
    a coarse placeholder when it does decline; v0.16 doesn't attempt to
    enumerate the full Stripe decline taxonomy in the mock.
    """

    status: str
    gateway_ref: str
    reason: str | None
    provider_call_id: str
    decline_code: str | None = None


@runtime_checkable
class TokenizerAdapter(Protocol):
    """Payment tokenization provider surface.

    All four operations are async (even ``tokenize``, despite the mock
    being CPU-only) so the Protocol shape doesn't bake in mock
    assumptions; a future ``AdyenTokenizerAdapter.tokenize`` may need
    network IO.

    The ``charge`` ``customer_external_ref`` argument is the provider-
    side customer id (Stripe's ``cus_*``). Pass ``None`` for mock; in
    production it must be set, and ``StripeTokenizerAdapter.charge``
    raises if it's missing because Stripe rejects off-session confirms
    without an attached customer.
    """

    async def charge(
        self,
        token: str,
        amount: Decimal,
        currency: str,
        *,
        idempotency_key: str,
        purpose: str,
        customer_external_ref: str | None,
    ) -> ChargeResult: ...

    async def tokenize(
        self,
        card_number: str,
        exp_month: int,
        exp_year: int,
        cvv: str,
    ) -> TokenizeResult: ...

    async def attach_payment_method_to_customer(
        self,
        *,
        payment_method_id: str,
        customer_id: str,
    ) -> None: ...

    async def ensure_customer(
        self,
        *,
        bss_customer_id: str,
        email: str,
    ) -> str: ...
