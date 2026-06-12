"""Idempotency-key construction tests."""

from __future__ import annotations

import pytest
from bss_webhooks.idempotency import idempotency_key


def test_idempotency_key_default_retry_zero() -> None:
    assert idempotency_key(aggregate_id="ATT-0042") == "ATT-0042-r0"


def test_idempotency_key_with_retry_count() -> None:
    assert idempotency_key(aggregate_id="ATT-0042", retry_count=3) == "ATT-0042-r3"


def test_idempotency_key_deterministic() -> None:
    """Same inputs → same output across calls (no internal randomness)."""
    a = idempotency_key(aggregate_id="IDT-0001", retry_count=1)
    b = idempotency_key(aggregate_id="IDT-0001", retry_count=1)
    assert a == b


def test_idempotency_key_different_for_different_retry() -> None:
    a = idempotency_key(aggregate_id="ATT-0001", retry_count=0)
    b = idempotency_key(aggregate_id="ATT-0001", retry_count=1)
    assert a != b


def test_idempotency_key_empty_aggregate_id_raises() -> None:
    with pytest.raises(ValueError, match="aggregate_id is required"):
        idempotency_key(aggregate_id="")


def test_idempotency_key_negative_retry_raises() -> None:
    with pytest.raises(ValueError, match="retry_count must be >= 0"):
        idempotency_key(aggregate_id="ATT-0001", retry_count=-1)
