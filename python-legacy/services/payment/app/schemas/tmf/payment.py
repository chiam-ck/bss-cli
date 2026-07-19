"""TMF676 Payment Management schemas (camelCase)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from bss_models import PaymentAttempt
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class TmfBase(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


# ── Request schemas ────────────────────────────────────────────────


class PaymentChargeRequest(TmfBase):
    customer_id: str
    payment_method_id: str
    amount: Decimal
    currency: str = "SGD"
    purpose: str


# ── Response schemas ───────────────────────────────────────────────


class PaymentAttemptResponse(TmfBase):
    id: str
    href: str
    customer_id: str
    payment_method_id: str
    amount: Decimal
    currency: str
    purpose: str
    status: str
    gateway_ref: str | None = None
    decline_reason: str | None = None
    attempted_at: datetime | None = None
    at_type: str = Field(default="Payment", serialization_alias="@type")


# ── Mapping ────────────────────────────────────────────────────────

PAYMENT_PATH = "/tmf-api/paymentManagement/v4/payment"


def to_payment_attempt_response(attempt: PaymentAttempt) -> PaymentAttemptResponse:
    return PaymentAttemptResponse(
        id=attempt.id,
        href=f"{PAYMENT_PATH}/{attempt.id}",
        customer_id=attempt.customer_id,
        payment_method_id=attempt.payment_method_id,
        amount=attempt.amount,
        currency=attempt.currency,
        purpose=attempt.purpose,
        status=attempt.status,
        gateway_ref=attempt.gateway_ref,
        decline_reason=attempt.decline_reason,
        attempted_at=attempt.attempted_at,
    )
