"""PaymentClient — service-to-service client for Payment (port 8003)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .auth import AuthProvider
from .base import BSSClient


class PaymentClient(BSSClient):
    """Client for the Payment service (port 8003)."""

    def __init__(
        self,
        base_url: str = "http://payment:8000",
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
    ):
        super().__init__(base_url, auth_provider, timeout)

    # ── Payment attempts ────────────────────────────────────────────────

    async def charge(
        self,
        *,
        customer_id: str,
        payment_method_id: str,
        amount: Decimal,
        currency: str = "SGD",
        purpose: str,
    ) -> dict[str, Any]:
        """POST /tmf-api/paymentManagement/v4/payment."""
        resp = await self._request(
            "POST",
            "/tmf-api/paymentManagement/v4/payment",
            json={
                "customerId": customer_id,
                "paymentMethodId": payment_method_id,
                "amount": str(amount),
                "currency": currency,
                "purpose": purpose,
            },
        )
        return resp.json()

    async def get_payment(self, attempt_id: str) -> dict[str, Any]:
        """GET /tmf-api/paymentManagement/v4/payment/{attempt_id}."""
        resp = await self._request(
            "GET", f"/tmf-api/paymentManagement/v4/payment/{attempt_id}"
        )
        return resp.json()

    async def list_payments(
        self,
        *,
        customer_id: str | None = None,
        payment_method_id: str | None = None,
        limit: int = 20,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /tmf-api/paymentManagement/v4/payment.

        v0.10 — ``offset`` added for the portal charge-history page's
        per-page stable pagination. ``limit + offset`` matches the
        SQL semantics; clients passing only ``limit`` see no behaviour
        change.
        """
        params: dict[str, Any] = {"limit": limit}
        if customer_id:
            params["customerId"] = customer_id
        if payment_method_id:
            params["paymentMethodId"] = payment_method_id
        if offset is not None:
            params["offset"] = offset
        resp = await self._request(
            "GET", "/tmf-api/paymentManagement/v4/payment", params=params
        )
        return resp.json()

    async def count_payments(self, *, customer_id: str) -> int:
        """GET /tmf-api/paymentManagement/v4/payment/count.

        v0.10 — total-count companion to ``list_payments`` so a paginated
        UI can render "Page N of M" without scanning the full list.
        """
        resp = await self._request(
            "GET",
            "/tmf-api/paymentManagement/v4/payment/count",
            params={"customerId": customer_id},
        )
        body = resp.json()
        return int(body.get("count", 0))

    # ── Payment methods ─────────────────────────────────────────────────

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
        """POST /tmf-api/paymentMethodManagement/v4/paymentMethod.

        Server is pre-tokenized (no PAN on the wire). ``card_token`` is the
        opaque provider token we pass through as ``providerToken``; real
        channels would pass a Stripe/Adyen token with the matching provider.

        v0.16: when ``tokenization_provider='stripe'``, the BSS-side card
        metadata (last4/brand/exp) is canonical on Stripe's side, not BSS.
        Portal callers send empty strings / 0s; we substitute schema-
        satisfying placeholders here so the TMF schema validation passes.
        The payment service replaces them with real data fetched from
        Stripe before persisting (PaymentMethodService.register_method).
        """
        if tokenization_provider == "stripe":
            if not last4:
                last4 = "0000"
            if not brand:
                brand = "card"
            if not exp_month:
                exp_month = 12
            if not exp_year:
                exp_year = 2099
        card_summary: dict[str, Any] = {
            "brand": brand,
            "last4": last4,
            "expMonth": exp_month,
            "expYear": exp_year,
        }
        if country is not None:
            card_summary["country"] = country
        body: dict[str, Any] = {
            "customerId": customer_id,
            "type": "card",
            "tokenizationProvider": tokenization_provider,
            "providerToken": card_token,
            "cardSummary": card_summary,
        }
        resp = await self._request(
            "POST",
            "/tmf-api/paymentMethodManagement/v4/paymentMethod",
            json=body,
        )
        return resp.json()

    async def get_method(self, method_id: str) -> dict[str, Any]:
        """GET /tmf-api/paymentMethodManagement/v4/paymentMethod/{id}."""
        resp = await self._request(
            "GET",
            f"/tmf-api/paymentMethodManagement/v4/paymentMethod/{method_id}",
        )
        return resp.json()

    async def list_methods(self, customer_id: str) -> list[dict[str, Any]]:
        """GET /tmf-api/paymentMethodManagement/v4/paymentMethod."""
        resp = await self._request(
            "GET",
            "/tmf-api/paymentMethodManagement/v4/paymentMethod",
            params={"customerId": customer_id},
        )
        return resp.json()

    async def remove_method(self, method_id: str) -> dict[str, Any]:
        """DELETE /tmf-api/paymentMethodManagement/v4/paymentMethod/{id}."""
        resp = await self._request(
            "DELETE",
            f"/tmf-api/paymentMethodManagement/v4/paymentMethod/{method_id}",
        )
        return resp.json() if resp.content else {"id": method_id, "removed": True}

    async def set_default_method(self, method_id: str) -> dict[str, Any]:
        """POST /tmf-api/paymentMethodManagement/v4/paymentMethod/{id}/setDefault.

        v0.10 — used by the self-serve portal's "Set default" CTA. The
        server owns the "exactly one default per customer" invariant
        and clears any prior default in the same transaction.
        """
        resp = await self._request(
            "POST",
            f"/tmf-api/paymentMethodManagement/v4/paymentMethod/{method_id}/setDefault",
        )
        return resp.json()

    # ── v0.16 ensure_customer ────────────────────────────────────────────

    async def ensure_customer(
        self, *, customer_id: str, email: str
    ) -> dict[str, Any]:
        """POST /admin-api/v1/payment-customer/ensure.

        Mint (or return cached) provider-side customer ref for a BSS
        customer. Stripe path returns ``{customer_external_ref: cus_*,
        provider: "stripe"}``; mock returns ``cus_mock_<id>``.

        Used by the portal's Stripe Checkout init route — must run
        BEFORE creating the CheckoutSession so the resulting pm_* gets
        attached to a customer that PaymentService.charge can later
        debit off-session.
        """
        resp = await self._request(
            "POST",
            "/admin-api/v1/payment-customer/ensure",
            json={"customer_id": customer_id, "email": email},
        )
        return resp.json()

    # ── v0.16 cutover ────────────────────────────────────────────────────

    async def cutover_invalidate_mock_tokens(
        self, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """POST /admin-api/v1/cutover/invalidate-mock-tokens.

        Marks every active mock-token payment_method as expired. Use
        BEFORE flipping ``BSS_PAYMENT_PROVIDER=mock → stripe``.
        ``dry_run=True`` returns the candidate count without writing.
        """
        resp = await self._request(
            "POST",
            "/admin-api/v1/cutover/invalidate-mock-tokens",
            params={"dryRun": "true" if dry_run else "false"},
        )
        return resp.json()
