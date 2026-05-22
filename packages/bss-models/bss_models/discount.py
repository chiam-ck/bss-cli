"""Promo discount math — one pure function, shared by catalog, COM, subscription.

v1.1. The effective price is *always* computed from the full base snapshot at
charge time (the base is never overwritten). Keeping this in one place is the
guard against the create-time and renewal-time math drifting apart — the plan's
worked example ($25 base, 20% off, 3 periods → $20 ×3 then $25) must hold
identically wherever a charge is computed.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

PERCENT = "percent"
ABSOLUTE = "absolute"
_CENTS = Decimal("0.01")


def apply_discount(
    discount_type: str,
    discount_value: Decimal | int | str,
    base_amount: Decimal | int | str,
) -> Decimal:
    """Return the effective price after applying a discount to ``base_amount``.

    - ``percent``: ``base * (100 - value) / 100``
    - ``absolute``: ``base - value`` (floored at 0 — a discount never pays out)

    Rounds half-up to 2 decimal places. Raises ``ValueError`` on an unknown
    discount type (callers validate before persisting, so this is a backstop).
    """
    base = Decimal(base_amount)
    value = Decimal(discount_value)
    if discount_type == PERCENT:
        effective = base * (Decimal(100) - value) / Decimal(100)
    elif discount_type == ABSOLUTE:
        effective = base - value
    else:
        raise ValueError(f"unknown discount_type {discount_type!r}")
    if effective < 0:
        effective = Decimal(0)
    return effective.quantize(_CENTS, rounding=ROUND_HALF_UP)


def discount_label(
    discount_type: str,
    discount_value: Decimal | int | str,
    currency: str = "SGD",
) -> str:
    """Human-readable discount label for portal display, e.g. ``20% off`` or
    ``SGD 5.00 off``."""
    value = Decimal(discount_value)
    if discount_type == PERCENT:
        # Fixed-point formatting; normalize() drops trailing zeros ("20.00"->"20",
        # "12.50"->"12.5"). format(.,'f') avoids Decimal's scientific notation
        # (normalize() alone turns 20 into "2E+1").
        return f"{format(value.normalize(), 'f')}% off"
    if discount_type == ABSOLUTE:
        return f"{currency} {value.quantize(_CENTS)} off"
    raise ValueError(f"unknown discount_type {discount_type!r}")
