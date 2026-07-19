"""TMF676 Payment Management router — no business logic."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_payment_service
from app.schemas.tmf.payment import (
    PaymentAttemptResponse,
    PaymentChargeRequest,
    to_payment_attempt_response,
)
from app.services.payment_service import PaymentService

router = APIRouter(tags=["payment"])


@router.post(
    "/payment",
    response_model=PaymentAttemptResponse,
    response_model_by_alias=True,
    status_code=201,
)
async def charge(
    body: PaymentChargeRequest,
    svc: PaymentService = Depends(get_payment_service),
) -> PaymentAttemptResponse:
    attempt = await svc.charge(
        customer_id=body.customer_id,
        payment_method_id=body.payment_method_id,
        amount=body.amount,
        currency=body.currency,
        purpose=body.purpose,
    )
    return to_payment_attempt_response(attempt)


# NOTE: /payment/count MUST be declared before /payment/{attempt_id}
# in this file. FastAPI matches routes top-to-bottom; if the
# parameterised /payment/{attempt_id} route comes first, a GET to
# /payment/count is interpreted as attempt_id="count" and 404s with
# "Payment attempt count not found" — which is the v0.10 charge-
# history regression I shipped before catching this. Order matters.
@router.get("/payment/count", include_in_schema=False)
async def count_payments(
    customerId: str,
    svc: PaymentService = Depends(get_payment_service),
) -> dict[str, int]:
    """v0.10 — total count for paginated listings.

    Internal endpoint (not part of TMF676): the portal needs the
    total to render "Page N of M" / "Next disabled". Lives under
    /payment/count rather than as a header on /payment so existing
    callers' response shape is unchanged.
    """
    return {"count": await svc.count_attempts(customerId)}


@router.get(
    "/payment/{attempt_id}",
    response_model=PaymentAttemptResponse,
    response_model_by_alias=True,
)
async def get_payment(
    attempt_id: str,
    svc: PaymentService = Depends(get_payment_service),
) -> PaymentAttemptResponse:
    attempt = await svc.get_attempt(attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail=f"Payment attempt {attempt_id} not found")
    return to_payment_attempt_response(attempt)


@router.get(
    "/payment",
    response_model=list[PaymentAttemptResponse],
    response_model_by_alias=True,
)
async def list_payments(
    customerId: str,
    limit: int | None = None,
    offset: int | None = None,
    svc: PaymentService = Depends(get_payment_service),
) -> list[PaymentAttemptResponse]:
    """v0.10 — limit + offset added for charge-history pagination.

    Existing v0.x callers pass neither and get the full ordered list
    (back-compat). The portal billing/history page uses limit=20 and
    offset=N*20 for stable per-page pagination.
    """
    attempts = await svc.list_attempts(customerId, limit=limit, offset=offset)
    return [to_payment_attempt_response(a) for a in attempts]
