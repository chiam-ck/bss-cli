"""Ticket state machine.

States: open, acknowledged, in_progress, pending, resolved, closed, cancelled.
Terminal: closed, cancelled.
"""

STATES = [
    "open", "acknowledged", "in_progress", "pending",
    "resolved", "closed", "cancelled",
]

TRANSITIONS = [
    {"trigger": "ack", "source": "open", "dest": "acknowledged"},
    {"trigger": "start", "source": "acknowledged", "dest": "in_progress"},
    {"trigger": "wait", "source": "in_progress", "dest": "pending"},
    {"trigger": "resume", "source": "pending", "dest": "in_progress"},
    {"trigger": "resolve", "source": "in_progress", "dest": "resolved"},
    {"trigger": "close", "source": "resolved", "dest": "closed"},
    {"trigger": "reopen", "source": "resolved", "dest": "in_progress"},
    {"trigger": "cancel", "source": "open", "dest": "cancelled"},
    {"trigger": "cancel", "source": "acknowledged", "dest": "cancelled"},
    {"trigger": "cancel", "source": "in_progress", "dest": "cancelled"},
    {"trigger": "cancel", "source": "pending", "dest": "cancelled"},
]

TERMINAL = frozenset({"closed", "cancelled"})
CANCELLABLE = frozenset({"open", "acknowledged", "in_progress", "pending"})


def is_valid_transition(from_state: str, trigger: str) -> bool:
    return any(
        t["trigger"] == trigger and t["source"] == from_state
        for t in TRANSITIONS
    )


def get_next_state(from_state: str, trigger: str) -> str | None:
    for t in TRANSITIONS:
        if t["trigger"] == trigger and t["source"] == from_state:
            return t["dest"]
    return None
