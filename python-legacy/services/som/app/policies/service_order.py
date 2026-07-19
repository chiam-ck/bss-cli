"""SOM state-machine policies — enforce legal transitions."""

from app.policies.base import PolicyViolation, policy

# ── ServiceOrder transitions ───────────────────────────────────────────
_SO_TRANSITIONS: dict[str, set[str]] = {
    "acknowledged": {"in_progress"},
    "in_progress": {"completed", "failed"},
}

# ── Service (CFS/RFS) transitions ─────────────────────────────────────
_SVC_TRANSITIONS: dict[str, set[str]] = {
    "designed": {"reserved", "failed"},
    "reserved": {"activated", "failed"},
    "activated": {"terminated"},
}


@policy("service_order.transition.invalid")
def check_service_order_transition(current_state: str, target_state: str) -> None:
    """Raise PolicyViolation if the ServiceOrder transition is illegal."""
    allowed = _SO_TRANSITIONS.get(current_state, set())
    if target_state not in allowed:
        raise PolicyViolation(
            rule="service_order.transition.invalid",
            message=(
                f"ServiceOrder cannot transition from '{current_state}' to '{target_state}'. "
                f"Allowed: {sorted(allowed) if allowed else 'none'}."
            ),
            context={
                "current_state": current_state,
                "target_state": target_state,
                "allowed": sorted(allowed) if allowed else [],
            },
        )


@policy("service.transition.invalid")
def check_service_transition(current_state: str, target_state: str) -> None:
    """Raise PolicyViolation if the Service transition is illegal."""
    allowed = _SVC_TRANSITIONS.get(current_state, set())
    if target_state not in allowed:
        raise PolicyViolation(
            rule="service.transition.invalid",
            message=(
                f"Service cannot transition from '{current_state}' to '{target_state}'. "
                f"Allowed: {sorted(allowed) if allowed else 'none'}."
            ),
            context={
                "current_state": current_state,
                "target_state": target_state,
                "allowed": sorted(allowed) if allowed else [],
            },
        )
