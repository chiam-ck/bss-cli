"""TMF676 Payment Method Management schemas (camelCase)."""

from __future__ import annotations

from datetime import datetime

from bss_models import PaymentMethod
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class TmfBase(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


# ── Request schemas ────────────────────────────────────────────────


class CardSummary(TmfBase):
    brand: str
    last4: str = Field(min_length=4, max_length=4)
    exp_month: int = Field(ge=1, le=12)
    exp_year: int = Field(ge=2000)
    country: str | None = None


class PaymentMethodCreateRequest(TmfBase):
    customer_id: str
    type: str = "card"
    tokenization_provider: str
    provider_token: str
    card_summary: CardSummary


# ── Response schemas ───────────────────────────────────────────────


class PaymentMethodResponse(TmfBase):
    id: str
    href: str
    customer_id: str
    type: str
    tokenization_provider: str | None = None
    provider_token: str
    card_summary: CardSummary
    is_default: bool
    status: str
    created_at: datetime | None = None
    at_type: str = Field(default="PaymentMethod", serialization_alias="@type")


# ── Mapping ────────────────────────────────────────────────────────

PAYMENT_METHOD_PATH = "/tmf-api/paymentMethodManagement/v4/paymentMethod"


def to_payment_method_response(
    pm: PaymentMethod,
    tokenization_provider: str | None = None,
) -> PaymentMethodResponse:
    return PaymentMethodResponse(
        id=pm.id,
        href=f"{PAYMENT_METHOD_PATH}/{pm.id}",
        customer_id=pm.customer_id,
        type=pm.type,
        tokenization_provider=tokenization_provider,
        provider_token=pm.token,
        card_summary=CardSummary(
            brand=pm.brand or "unknown",
            last4=pm.last4,
            exp_month=pm.exp_month,
            exp_year=pm.exp_year,
        ),
        is_default=pm.is_default,
        status=pm.status,
        created_at=pm.created_at,
    )
