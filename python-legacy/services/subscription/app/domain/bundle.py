"""Bundle balance domain logic — pure functions.

No DB, no side effects, no wall-clock reads.
All functions take immutable inputs and return new values.
"""

from __future__ import annotations

from dataclasses import dataclass

UNLIMITED = -1


@dataclass(frozen=True)
class BalanceSnapshot:
    """Immutable view of a single allowance balance."""

    allowance_type: str
    total: int  # -1 = unlimited
    consumed: int
    unit: str

    @property
    def remaining(self) -> int:
        if self.total == UNLIMITED:
            return UNLIMITED
        return self.total - self.consumed


@dataclass(frozen=True)
class AllowanceSpec:
    """Plan-level allowance definition (from catalog)."""

    allowance_type: str
    quantity: int  # -1 = unlimited
    unit: str


def consume(balance: BalanceSnapshot, quantity: int) -> BalanceSnapshot:
    """Decrement consumed by quantity. Clamps so remaining never goes negative.

    Unlimited balances (-1 total) are never decremented.
    """
    if quantity < 0:
        raise ValueError(f"quantity must be non-negative, got {quantity}")
    if quantity == 0:
        return balance
    if balance.total == UNLIMITED:
        return balance
    new_consumed = min(balance.consumed + quantity, balance.total)
    return BalanceSnapshot(
        allowance_type=balance.allowance_type,
        total=balance.total,
        consumed=new_consumed,
        unit=balance.unit,
    )


def is_exhausted(
    balances: list[BalanceSnapshot], primary_type: str = "data"
) -> bool:
    """True if the primary allowance type has remaining <= 0.

    Unlimited (-1 total) never exhausts.
    If no balance matches primary_type, returns True (no data = exhausted).

    v0.17 doctrine: ``data_roaming`` is *additive*, never primary. Roaming
    exhaustion blocks roaming usage (enforced by
    ``subscription.usage_rated.roaming_balance_required`` policy) but
    must NOT exhaust the subscription itself, so callers must keep the
    default ``primary_type="data"``.
    """
    for b in balances:
        if b.allowance_type == primary_type:
            if b.total == UNLIMITED:
                return False
            return b.remaining <= 0
    return True


def add_allowance(balance: BalanceSnapshot, quantity: int) -> BalanceSnapshot:
    """Top-up: increase total by quantity. Unlimited balances are unchanged."""
    if quantity < 0:
        raise ValueError(f"quantity must be non-negative, got {quantity}")
    if quantity == 0:
        return balance
    if balance.total == UNLIMITED:
        return balance
    return BalanceSnapshot(
        allowance_type=balance.allowance_type,
        total=balance.total + quantity,
        consumed=balance.consumed,
        unit=balance.unit,
    )


def reset_for_new_period(allowance_specs: list[AllowanceSpec]) -> list[BalanceSnapshot]:
    """Renewal: create fresh balances from plan specs with consumed=0."""
    return [
        BalanceSnapshot(
            allowance_type=spec.allowance_type,
            total=spec.quantity,
            consumed=0,
            unit=spec.unit,
        )
        for spec in allowance_specs
    ]


def primary_allowance_type() -> str:
    """The primary allowance type that triggers block-on-exhaust.

    Always 'data' in v0.1. Voice/SMS exhaustion does not block.
    """
    return "data"
