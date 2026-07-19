"""Idempotency-key construction for outbound provider calls.

Stripe (v0.16), Resend (v0.14), Didit (v0.15) all accept an
``Idempotency-Key`` header that lets the provider dedupe at-least-once
retries. The key has subtle semantics: the *same* key on a
BSS-crash-restart retry must return the original outcome (we want
dedup); a *new* key on a user-initiated retry must charge fresh (we
want a new attempt).

The discriminator is "is this the same logical attempt as before?"
rather than "is this the same network request?" Both are encoded by
the (aggregate_id, retry_count) pair:

* Same aggregate row in ``charging`` state + recorded
  ``idempotency_key`` → restart retry → reuse the recorded key.
* New aggregate row → user retry → new ``retry_count=0`` key.

This module is the single source of key construction so callers can't
drift the format.
"""

from __future__ import annotations


def idempotency_key(
    *, aggregate_id: str, retry_count: int = 0
) -> str:
    """Build a deterministic idempotency key for a provider call.

    Format: ``"<AGGREGATE_ID>-r<retry_count>"`` (e.g. ``"ATT-0042-r0"``).
    Stable across processes — given the same inputs, the same key is
    produced.

    :param aggregate_id: BSS-side aggregate id (``ATT-*``, ``IDT-*``,
        ``SUB-*``, etc.). Caller is responsible for not feeding
        externally-controlled values.
    :param retry_count: 0 for the first attempt; bumped only when the
        caller's intent is "I want a fresh attempt." Restart retries
        keep the same retry_count.
    """
    if not aggregate_id:
        raise ValueError("aggregate_id is required for idempotency_key")
    if retry_count < 0:
        raise ValueError(
            f"retry_count must be >= 0, got {retry_count}"
        )
    return f"{aggregate_id}-r{retry_count}"
