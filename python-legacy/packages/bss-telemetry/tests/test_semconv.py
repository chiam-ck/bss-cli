"""Unit tests for semantic-convention attribute keys.

The PII guard here is the in-package complement to the CI grep
guard in v0.2: keys must be ID strings or status enums, never raw
PII fields.
"""

from __future__ import annotations


def test_keys_are_bss_namespaced():
    from bss_telemetry import semconv

    public = [k for k in dir(semconv) if not k.startswith("_") and k.isupper()]
    assert len(public) >= 10
    for key in public:
        value = getattr(semconv, key)
        assert isinstance(value, str)
        assert value.startswith("bss."), f"{key}={value!r} must be bss.* namespaced"


def test_no_pii_keys():
    """Keys must not declare raw PII fields."""
    from bss_telemetry import semconv

    forbidden = ["email", "card", "nric", "iccid_full", "ki_full", "pan", "cvv"]
    public = [k for k in dir(semconv) if not k.startswith("_") and k.isupper()]
    for key in public:
        value = getattr(semconv, key).lower()
        for token in forbidden:
            assert token not in value, (
                f"{key}={value!r} contains forbidden PII substring {token!r}"
            )


def test_core_id_keys_present():
    """Core identity keys are exported and usable."""
    from bss_telemetry import semconv

    assert semconv.BSS_CUSTOMER_ID == "bss.customer_id"
    assert semconv.BSS_TENANT_ID == "bss.tenant_id"
    assert semconv.BSS_ORDER_ID == "bss.order_id"
    assert semconv.BSS_SUBSCRIPTION_ID == "bss.subscription_id"
    assert semconv.BSS_OFFERING_ID == "bss.offering_id"
    # v0.9 — perimeter-resolved identity for span filtering.
    assert semconv.BSS_SERVICE_IDENTITY == "bss.service.identity"
