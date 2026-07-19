"""v0.17 — usage-rated policies.

Roaming-balance enforcement lives here so the doctrine
(``data_roaming`` is *additive*, never blocks the subscription itself)
is encoded in a single named rule. ``handle_usage_rated`` calls
``check_roaming_balance_required`` for any ``data_roaming``-typed
decrement before invoking ``consume``; failure raises ``PolicyViolation``
with rule ``subscription.usage_rated.roaming_balance_required``, which
the handler converts to a ``usage.rejected`` audit event without
touching subscription state.
"""

from app.policies.base import PolicyViolation, policy


@policy("subscription.usage_rated.roaming_balance_required")
def check_roaming_balance_required(
    *, subscription_id: str, balance, consumed_quantity: int
) -> None:
    """Reject roaming usage when the subscription has no roaming balance
    or the balance is exhausted.

    ``balance`` is the locked ``BundleBalance`` row (or None if missing).
    Subscription state is intentionally NOT changed by this rejection —
    home data is unaffected.
    """
    if balance is None:
        raise PolicyViolation(
            rule="subscription.usage_rated.roaming_balance_required",
            message=(
                f"Subscription {subscription_id} has no data_roaming "
                "allowance — roaming usage rejected"
            ),
            context={
                "subscription_id": subscription_id,
                "consumed_quantity": consumed_quantity,
            },
        )
    if balance.total != -1 and (balance.total - balance.consumed) <= 0:
        raise PolicyViolation(
            rule="subscription.usage_rated.roaming_balance_required",
            message=(
                f"Subscription {subscription_id} data_roaming balance "
                "exhausted — roaming usage rejected"
            ),
            context={
                "subscription_id": subscription_id,
                "remaining": balance.total - balance.consumed,
                "consumed_quantity": consumed_quantity,
            },
        )
