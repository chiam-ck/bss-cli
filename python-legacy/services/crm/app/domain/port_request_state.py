"""v0.17 — PortRequest finite state machine.

States: ``requested → validated → completed | rejected``.

``validated`` is a hook for an automated donor-carrier check — v0.17
ships only the operator-driven path so ``requested → completed |
rejected`` is the common transition. Reject is allowed from either
``requested`` or ``validated``; complete is allowed from either too.

Mirrors the shape of ``case_state.py`` so a future hot-path optimization
can lift this into a shared FSM helper.
"""

STATES = ["requested", "validated", "completed", "rejected"]

TRANSITIONS = [
    {"trigger": "validate", "source": "requested", "dest": "validated"},
    {"trigger": "complete", "source": "requested", "dest": "completed"},
    {"trigger": "complete", "source": "validated", "dest": "completed"},
    {"trigger": "reject", "source": "requested", "dest": "rejected"},
    {"trigger": "reject", "source": "validated", "dest": "rejected"},
]

TERMINAL = frozenset({"completed", "rejected"})


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
