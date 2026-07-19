"""Case state machine.

States: open, in_progress, pending_customer, resolved, closed.
Cancel only from {open, in_progress, pending_customer}.
"""

STATES = ["open", "in_progress", "pending_customer", "resolved", "closed"]

TRANSITIONS = [
    {"trigger": "take", "source": "open", "dest": "in_progress"},
    {"trigger": "await_customer", "source": "in_progress", "dest": "pending_customer"},
    {"trigger": "resume", "source": "pending_customer", "dest": "in_progress"},
    {"trigger": "resolve", "source": "in_progress", "dest": "resolved"},
    {"trigger": "resolve", "source": "open", "dest": "resolved"},
    {"trigger": "close", "source": "resolved", "dest": "closed"},
    {"trigger": "cancel", "source": "open", "dest": "closed"},
    {"trigger": "cancel", "source": "in_progress", "dest": "closed"},
    {"trigger": "cancel", "source": "pending_customer", "dest": "closed"},
]

TERMINAL = frozenset({"closed"})
CANCELLABLE = frozenset({"open", "in_progress", "pending_customer"})


def is_valid_transition(from_state: str, trigger: str) -> bool:
    """Check if a trigger is valid from the given state."""
    return any(
        t["trigger"] == trigger and (t["source"] == from_state or from_state in t.get("source", []))
        for t in TRANSITIONS
    )


def get_next_state(from_state: str, trigger: str) -> str | None:
    """Return the destination state for a trigger from a given state, or None if invalid."""
    for t in TRANSITIONS:
        src = t["source"]
        if t["trigger"] == trigger and src == from_state:
            return t["dest"]
    return None
