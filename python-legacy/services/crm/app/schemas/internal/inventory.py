"""Inventory DTOs."""

from datetime import datetime

from pydantic import BaseModel


class MsisdnResponse(BaseModel):
    msisdn: str
    status: str
    reserved_at: datetime | None = None
    assigned_to_subscription_id: str | None = None


class EsimResponse(BaseModel):
    iccid: str
    imsi: str
    profile_state: str
    smdp_server: str | None = None
    assigned_msisdn: str | None = None
    assigned_to_subscription_id: str | None = None
    reserved_at: datetime | None = None
    downloaded_at: datetime | None = None
    activated_at: datetime | None = None


class EsimActivationResponse(BaseModel):
    iccid: str
    activation_code: str | None = None
    smdp_server: str | None = None
    matching_id: str | None = None


class AssignMsisdnRequest(BaseModel):
    msisdn: str


class AddRangeRequest(BaseModel):
    prefix: str
    count: int


class AddRangeResponse(BaseModel):
    prefix: str
    count: int
    inserted: int
    skipped: int
    first: str
    last: str


class MsisdnPoolCountResponse(BaseModel):
    """Group-by-status count of the MSISDN pool.

    ``total`` is the sum across all states; the canonical state keys
    are always present (0 if the pool has no rows in that state).
    """

    available: int
    reserved: int
    assigned: int
    ported_out: int
    total: int
    prefix: str | None = None


def to_msisdn_response(m) -> MsisdnResponse:
    return MsisdnResponse(
        msisdn=m.msisdn,
        status=m.status,
        reserved_at=m.reserved_at,
        assigned_to_subscription_id=m.assigned_to_subscription_id,
    )


def to_esim_response(e) -> EsimResponse:
    return EsimResponse(
        iccid=e.iccid,
        imsi=e.imsi,
        profile_state=e.profile_state,
        smdp_server=e.smdp_server,
        assigned_msisdn=e.assigned_msisdn,
        assigned_to_subscription_id=e.assigned_to_subscription_id,
        reserved_at=e.reserved_at,
        downloaded_at=e.downloaded_at,
        activated_at=e.activated_at,
    )
