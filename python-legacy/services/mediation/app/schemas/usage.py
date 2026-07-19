"""TMF635 usage schemas — camelCase on the wire."""

from datetime import datetime

from bss_models.mediation import UsageEvent
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

USAGE_PATH = "/tmf-api/usageManagement/v4/usage"


class UsageCreateRequest(BaseModel):
    """TMF635 usage creation.

    `eventType` is one of: data, voice, voice_minutes, sms
    `unit` is one of: mb, minutes, count
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    msisdn: str
    event_type: str
    event_time: datetime
    quantity: int
    unit: str
    source: str | None = None
    raw_cdr_ref: str | None = None
    # v0.17 — channel/network adapter sets True when the underlying CDR
    # was produced on a visited (roaming) network. Default False so
    # every existing scenario YAML and rating fixture posts unchanged.
    roaming_indicator: bool = False


class UsageResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    href: str
    msisdn: str
    subscription_id: str | None = None
    event_type: str
    event_time: datetime
    quantity: int
    unit: str
    source: str | None = None
    raw_cdr_ref: str | None = None
    processed: bool = False
    processing_error: str | None = None
    roaming_indicator: bool = False
    at_type: str = "Usage"


def to_usage_response(evt: UsageEvent) -> UsageResponse:
    return UsageResponse(
        id=evt.id,
        href=f"{USAGE_PATH}/{evt.id}",
        msisdn=evt.msisdn,
        subscription_id=evt.subscription_id,
        event_type=evt.event_type,
        event_time=evt.event_time,
        quantity=evt.quantity,
        unit=evt.unit,
        source=evt.source,
        raw_cdr_ref=evt.raw_cdr_ref,
        processed=evt.processed,
        processing_error=evt.processing_error,
        roaming_indicator=evt.roaming_indicator,
    )
