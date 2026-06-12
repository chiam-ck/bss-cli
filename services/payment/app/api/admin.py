"""Payment admin router — wipes the ``payment`` schema + v0.16 cutover + ensure_customer."""

from __future__ import annotations

from bss_admin import ResetPlan, TableReset, admin_router
from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.dependencies import get_payment_method_service
from app.services.payment_method_service import PaymentMethodService

_OPERATIONAL = (
    TableReset("payment_attempt"),
    TableReset("payment_method"),
)

router = admin_router(
    service_name="payment",
    plans=[ResetPlan(schema="payment", tables=_OPERATIONAL)],
)


# v0.16 — cutover endpoint. Operator-only, gated by BSS_API_TOKEN at the
# perimeter. The CLI (`bss payment cutover --invalidate-mock-tokens`)
# calls it; no portal route surfaces this. Kept on the existing admin
# router so the same `/admin-api/v1` prefix + same auth applies.
@router.post("/cutover/invalidate-mock-tokens")
async def cutover_invalidate_mock_tokens(
    dry_run: bool = Query(False, alias="dryRun"),
    svc: PaymentMethodService = Depends(get_payment_method_service),
):
    """Mark every active mock-token payment method as expired.

    Use BEFORE flipping ``BSS_PAYMENT_PROVIDER=mock → stripe`` in
    production. Without this (or the lazy-fail recovery path), the
    first charge against any pre-cutover saved card silently fails
    weeks after the switch.
    """
    return await svc.cutover_invalidate_mock_tokens(dry_run=dry_run)


# v0.16 — ensure_customer endpoint. Used by the portal's Stripe Checkout
# init route to mint (or look up cached) the cus_* before creating a
# CheckoutSession. Pre-attaching the saved card to a customer is what
# makes the resulting pm_* charge-able off-session via PaymentService.charge.
class _EnsureCustomerRequest(BaseModel):
    customer_id: str
    email: str


class _EnsureCustomerResponse(BaseModel):
    customer_external_ref: str
    provider: str


@router.post(
    "/payment-customer/ensure",
    response_model=_EnsureCustomerResponse,
)
async def ensure_payment_customer(
    request: Request,
    body: _EnsureCustomerRequest,
):
    """Mint or look up the provider-side customer ref for a BSS customer.

    Stripe-mode: returns ``cus_*`` (cached in ``payment.customer`` after
    the first call; subsequent calls hit the cache).

    Mock-mode: returns the deterministic ``cus_mock_<bss_id>``.
    """
    tokenizer = request.app.state.tokenizer
    if tokenizer is None:
        raise HTTPException(
            status_code=503,
            detail="payment service has no TokenizerAdapter configured",
        )
    cus = await tokenizer.ensure_customer(
        bss_customer_id=body.customer_id,
        email=body.email,
    )
    provider = (
        "stripe"
        if type(tokenizer).__name__ == "StripeTokenizerAdapter"
        else "mock"
    )
    return _EnsureCustomerResponse(
        customer_external_ref=cus,
        provider=provider,
    )
