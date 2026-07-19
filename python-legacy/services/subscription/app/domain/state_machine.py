"""Subscription state machine.

States: pending, active, blocked, terminated.
Terminal: terminated.

Pure module — no DB, no side effects.
"""

STATES = ["pending", "active", "blocked", "terminated"]

TRANSITIONS = [
    {"trigger": "activate", "source": "pending", "dest": "active"},
    {"trigger": "fail_activate", "source": "pending", "dest": "terminated"},
    {"trigger": "exhaust", "source": "active", "dest": "blocked"},
    {"trigger": "top_up", "source": "blocked", "dest": "active"},
    {"trigger": "top_up", "source": "active", "dest": "active"},
    {"trigger": "renew", "source": "active", "dest": "active"},
    {"trigger": "renew_fail", "source": "active", "dest": "blocked"},
    {"trigger": "terminate", "source": "active", "dest": "terminated"},
    {"trigger": "terminate", "source": "blocked", "dest": "terminated"},
]

TERMINAL = frozenset({"terminated"})

ALL_TRIGGERS = frozenset(t["trigger"] for t in TRANSITIONS)


def is_valid_transition(from_state: str, trigger: str) -> bool:
    """Check if a trigger is valid from the given state."""
    return any(
        t["trigger"] == trigger and t["source"] == from_state
        for t in TRANSITIONS
    )


def get_next_state(from_state: str, trigger: str) -> str | None:
    """Return the destination state for a trigger from a given state, or None."""
    for t in TRANSITIONS:
        if t["trigger"] == trigger and t["source"] == from_state:
            return t["dest"]
    return None
