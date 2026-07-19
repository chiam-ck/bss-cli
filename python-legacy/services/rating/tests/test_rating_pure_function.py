"""Unit tests for the pure `rate_usage` function.

Matrix: every offering × allowance type × usage quantity. No DB, no HTTP.
"""

from decimal import Decimal

import pytest
from app.domain.rating import (
    EVENT_TYPE_TO_ALLOWANCE,
    RatingError,
    UsageInput,
    rate_usage,
)

PLAN_M = {
    "id": "PLAN_M",
    "name": "Standard",
    "productOfferingPrice": [
        {"priceType": "recurring", "price": {"taxIncludedAmount": {"value": "25.00", "unit": "SGD"}}},
    ],
    "bundleAllowance": [
        {"allowanceType": "data", "quantity": 30720, "unit": "mb"},
        {"allowanceType": "voice", "quantity": -1, "unit": "minutes"},
        {"allowanceType": "sms", "quantity": -1, "unit": "count"},
    ],
}

PLAN_S = {
    "id": "PLAN_S",
    "name": "Saver",
    "productOfferingPrice": [
        {"priceType": "recurring", "price": {"taxIncludedAmount": {"value": "15.00", "unit": "SGD"}}},
    ],
    "bundleAllowance": [
        {"allowanceType": "data", "quantity": 10240, "unit": "mb"},
        {"allowanceType": "voice", "quantity": 300, "unit": "minutes"},
        {"allowanceType": "sms", "quantity": 100, "unit": "count"},
    ],
}

PLAN_DATA_ONLY = {
    "id": "PLAN_DATA_ONLY",
    "bundleAllowance": [
        {"allowanceType": "data", "quantity": 5120, "unit": "mb"},
    ],
}


def _usage(event_type: str, quantity: int, unit: str) -> UsageInput:
    return UsageInput(
        usage_event_id="UE-000001",
        subscription_id="SUB-0001",
        msisdn="90000042",
        event_type=event_type,
        quantity=quantity,
        unit=unit,
    )


@pytest.mark.parametrize(
    "event_type,quantity,unit,expected_allowance",
    [
        ("data", 100, "mb", "data"),
        ("data", 1, "mb", "data"),
        ("data", 1_000_000, "mb", "data"),
        ("voice", 5, "minutes", "voice"),
        ("voice_minutes", 10, "minutes", "voice"),
        ("sms", 1, "count", "sms"),
        ("sms", 50, "count", "sms"),
    ],
)
def test_rate_usage_plan_m_happy_path(event_type, quantity, unit, expected_allowance):
    result = rate_usage(_usage(event_type, quantity, unit), PLAN_M)
    assert result.allowance_type == expected_allowance
    assert result.consumed_quantity == quantity
    assert result.unit == unit
    assert result.charge_amount == Decimal("0")
    assert result.currency == "SGD"
    assert result.subscription_id == "SUB-0001"
    assert result.usage_event_id == "UE-000001"


def test_rate_usage_plan_s_same_shape():
    result = rate_usage(_usage("data", 200, "mb"), PLAN_S)
    assert result.allowance_type == "data"
    assert result.consumed_quantity == 200
    assert result.charge_amount == Decimal("0")


def test_rate_usage_no_matching_allowance_raises():
    """PLAN_DATA_ONLY has no voice/sms allowance → RatingError."""
    with pytest.raises(RatingError, match="no 'voice' allowance"):
        rate_usage(_usage("voice_minutes", 1, "minutes"), PLAN_DATA_ONLY)

    with pytest.raises(RatingError, match="no 'sms' allowance"):
        rate_usage(_usage("sms", 1, "count"), PLAN_DATA_ONLY)


def test_rate_usage_unknown_event_type_raises():
    with pytest.raises(RatingError, match="No allowance mapping"):
        rate_usage(_usage("video", 1, "mb"), PLAN_M)


def test_rate_usage_unit_mismatch_raises():
    """Data must come in mb, voice in minutes, sms in count."""
    with pytest.raises(RatingError, match="does not match allowance unit"):
        rate_usage(_usage("data", 1, "gb"), PLAN_M)

    with pytest.raises(RatingError, match="does not match allowance unit"):
        rate_usage(_usage("voice", 1, "seconds"), PLAN_M)


def test_rate_usage_empty_bundle_allowance_raises():
    tariff = {"id": "PLAN_X", "bundleAllowance": []}
    with pytest.raises(RatingError, match="no 'data' allowance"):
        rate_usage(_usage("data", 100, "mb"), tariff)


def test_rate_usage_missing_bundle_allowance_key_raises():
    tariff = {"id": "PLAN_X"}
    with pytest.raises(RatingError, match="no 'data' allowance"):
        rate_usage(_usage("data", 100, "mb"), tariff)


def test_rate_usage_pure_function_no_mutation():
    """Guarantee: rate_usage does not mutate its inputs."""
    import copy

    snapshot = copy.deepcopy(PLAN_M)
    usage = _usage("data", 100, "mb")
    usage_snapshot = UsageInput(**vars(usage))

    rate_usage(usage, PLAN_M)

    assert PLAN_M == snapshot
    assert usage == usage_snapshot


def test_event_type_alias_mapping_covers_mediation_vocabulary():
    """Every event_type that mediation accepts must map to an allowance."""
    mediation_event_types = {"data", "voice", "voice_minutes", "sms"}
    assert mediation_event_types <= set(EVENT_TYPE_TO_ALLOWANCE.keys())


def test_rate_usage_currency_defaults_to_sgd_when_absent():
    tariff = {"id": "PLAN_X", "bundleAllowance": [
        {"allowanceType": "data", "quantity": 1024, "unit": "mb"},
    ]}
    result = rate_usage(_usage("data", 100, "mb"), tariff)
    assert result.currency == "SGD"
