"""TMF622 ProductOrder Pydantic schemas — camelCase for API."""

from datetime import datetime
from decimal import Decimal

from bss_models.order_mgmt import OrderItem, ProductOrder
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

ORDER_PATH = "/tmf-api/productOrderingManagement/v4/productOrder"


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    customer_id: str
    offering_id: str
    msisdn_preference: str | None = None
    notes: str | None = None
    # v1.1 — optional typed promo code. Absent → assigned-offer discovery still runs.
    discount_code: str | None = None
    # v1.1 — opt out of the auto-applied assigned offer (customer unticked it).
    skip_assigned_offer: bool = False


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    action: str
    offering_id: str
    state: str | None = None
    target_subscription_id: str | None = None
    # v0.7 — price snapshot stamped at create time, copied to subscription.
    price_amount: Decimal | None = None
    price_currency: str | None = None
    price_offering_price_id: str | None = None
    # v1.1 — promo discount intent stamped at create (NULL when no promo applies).
    discount_code: str | None = None
    promo_offer_definition_id: str | None = None
    discount_type: str | None = None
    discount_value: Decimal | None = None
    discount_periods_total: int | None = None
    promo_offer_id: str | None = None


class ProductOrderResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    href: str
    customer_id: str
    state: str
    order_date: datetime | None = None
    requested_completion_date: datetime | None = None
    completed_date: datetime | None = None
    msisdn_preference: str | None = None
    notes: str | None = None
    items: list[OrderItemResponse] = []
    at_type: str = Field(default="ProductOrder", serialization_alias="@type")


def to_order_item_response(item: OrderItem) -> OrderItemResponse:
    return OrderItemResponse(
        id=item.id,
        action=item.action,
        offering_id=item.offering_id,
        state=item.state,
        target_subscription_id=item.target_subscription_id,
        price_amount=item.price_amount,
        price_currency=item.price_currency,
        price_offering_price_id=item.price_offering_price_id,
        discount_code=item.discount_code,
        promo_offer_definition_id=item.promo_offer_definition_id,
        discount_type=item.discount_type,
        discount_value=item.discount_value,
        discount_periods_total=item.discount_periods_total,
        promo_offer_id=item.promo_offer_id,
    )


def to_product_order_response(order: ProductOrder) -> ProductOrderResponse:
    return ProductOrderResponse(
        id=order.id,
        href=f"{ORDER_PATH}/{order.id}",
        customer_id=order.customer_id,
        state=order.state,
        order_date=order.order_date,
        requested_completion_date=order.requested_completion_date,
        completed_date=order.completed_date,
        msisdn_preference=order.msisdn_preference,
        notes=order.notes,
        items=[to_order_item_response(i) for i in order.items] if order.items else [],
    )
