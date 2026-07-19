"""v0.17 — PortRequest policies.

Operator-only writes; customer-side surfaces never see these. Each
policy raises ``PolicyViolation`` with a stable rule id so the cockpit
can render structured errors and the audit log can group on rule.
"""

from app.domain import port_request_state
from app.policies.base import PolicyViolation, policy


@policy("port_request.create.donor_msisdn_unique_among_pending")
def check_donor_msisdn_unique(
    donor_msisdn: str, existing_open
) -> None:
    """One live port at a time per donor MSISDN. Mirrors the partial
    unique index — surface as a structured error before we hit the DB
    constraint so the operator sees the conflicting PORT-NNN id."""
    if existing_open is not None:
        raise PolicyViolation(
            rule="port_request.create.donor_msisdn_unique_among_pending",
            message=(
                f"Donor MSISDN {donor_msisdn} already has an open "
                f"port request {existing_open.id} "
                f"(state={existing_open.state})"
            ),
            context={
                "donor_msisdn": donor_msisdn,
                "existing_id": existing_open.id,
                "existing_state": existing_open.state,
            },
        )


@policy("port_request.create.direction_valid")
def check_direction_valid(direction: str) -> None:
    if direction not in ("port_in", "port_out"):
        raise PolicyViolation(
            rule="port_request.create.direction_valid",
            message=f"Direction must be port_in or port_out (got {direction!r})",
            context={"direction": direction},
        )


@policy("port_request.create.target_sub_required_for_port_out")
def check_target_sub_required(
    direction: str, target_subscription_id: str | None
) -> None:
    """Port-out releases an existing subscription's MSISDN to the
    recipient carrier — we need to know which subscription. Port-in is
    pre-activation by definition (the customer is signing up with their
    existing number), so target_subscription_id is optional there."""
    if direction == "port_out" and not target_subscription_id:
        raise PolicyViolation(
            rule="port_request.create.target_sub_required_for_port_out",
            message="port_out requires target_subscription_id",
            context={"direction": direction},
        )


@policy("port_request.transition.valid")
def check_transition_valid(from_state: str, trigger: str) -> None:
    if not port_request_state.is_valid_transition(from_state, trigger):
        raise PolicyViolation(
            rule="port_request.transition.valid",
            message=(
                f"Cannot {trigger!r} a port request in state {from_state!r}"
            ),
            context={"from_state": from_state, "trigger": trigger},
        )


@policy("port_request.reject.requires_reason")
def check_reject_reason(reason: str | None) -> None:
    if not reason or not reason.strip():
        raise PolicyViolation(
            rule="port_request.reject.requires_reason",
            message="Rejection reason is required",
            context={},
        )
