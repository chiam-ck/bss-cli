"""Plan-change policies — guard the schedule path.

Doctrine (CLAUDE.md / DECISIONS.md): plan changes apply at the next renewal
boundary. No proration, no immediate effect, no shortcut. The customer
commits to the new plan at the price that's active at scheduling time;
that price is snapshotted onto the subscription's pending fields and
charged the next time renewal fires.
"""

from __future__ import annotations

from bss_clients.errors import PolicyViolationFromServer

from app.policies.base import PolicyViolation, policy


@policy("subscription.plan_change.not_eligible_state")
def check_subscription_active_or_pending_renewal(state: str) -> None:
    """Only active subscriptions can schedule a plan change.

    `pending` (never activated), `blocked` (out of allowance), and
    `terminated` are all rejected. v0.6 has no `pending_renewal` state —
    if/when one is added, allow it here too.
    """
    if state != "active":
        raise PolicyViolation(
            rule="subscription.plan_change.not_eligible_state",
            message=f"Cannot schedule plan change in state '{state}'",
            context={"state": state},
        )


@policy("subscription.plan_change.target_not_sellable_now")
async def check_offering_sellable_now(catalog_client, new_offering_id: str) -> dict:
    """Target offering must be in the active catalog at scheduling time."""
    active = await catalog_client.list_active_offerings()
    match = next((o for o in active if o.get("id") == new_offering_id), None)
    if match is None:
        raise PolicyViolation(
            rule="subscription.plan_change.target_not_sellable_now",
            message=(
                f"Offering {new_offering_id} is not currently sellable "
                f"and cannot be a plan-change target"
            ),
            context={"new_offering_id": new_offering_id},
        )
    return match


@policy("subscription.plan_change.same_offering")
def check_not_same_offering(current_offering_id: str, new_offering_id: str) -> None:
    if current_offering_id == new_offering_id:
        raise PolicyViolation(
            rule="subscription.plan_change.same_offering",
            message="Cannot schedule a plan change to the current plan",
            context={
                "current_offering_id": current_offering_id,
                "new_offering_id": new_offering_id,
            },
        )


@policy("subscription.plan_change.already_pending")
def check_no_pending_change(pending_offering_id: str | None) -> None:
    if pending_offering_id is not None:
        raise PolicyViolation(
            rule="subscription.plan_change.already_pending",
            message=(
                "A plan change is already pending. Cancel it first if you "
                "want to schedule a different one."
            ),
            context={"pending_offering_id": pending_offering_id},
        )


@policy("subscription.admin_only")
def check_admin_role() -> None:
    """v0.7 — admin-only gate, sourced from `auth_context.current()`.

    The default v0.3 context grants ``roles=["admin"]`` to every actor, so
    in practice this guard is permissive until Phase 12 wires real RBAC.
    The check still exists as a structural seam — admin-flagged operations
    go through this policy and Phase 12 only has to tighten the role list.
    """
    from app import auth_context

    ctx = auth_context.current()
    if "admin" not in ctx.roles:
        raise PolicyViolation(
            rule="subscription.admin_only",
            message="This operation requires the admin role",
            context={"actor": ctx.actor, "roles": ctx.roles},
        )


async def fetch_active_price_for_target(catalog_client, new_offering_id: str) -> dict:
    """Resolve and return the active price row for the target offering.

    Wraps PolicyViolationFromServer (the catalog-side error) into a local
    ``subscription.plan_change.target_no_active_price`` so callers see a
    consistent rule namespace. Should be called *after* the target has
    passed ``check_offering_sellable_now``.
    """
    try:
        return await catalog_client.get_active_price(new_offering_id)
    except PolicyViolationFromServer as exc:
        raise PolicyViolation(
            rule="subscription.plan_change.target_no_active_price",
            message=f"No active price for {new_offering_id} at this moment",
            context={"new_offering_id": new_offering_id, "underlying": exc.rule},
        )
