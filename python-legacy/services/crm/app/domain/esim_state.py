"""eSIM profile lifecycle state machine.

States: available, reserved, downloaded, activated, suspended, recycled.
"""

STATES = [
    "available", "reserved", "downloaded",
    "activated", "suspended", "recycled",
]

TRANSITIONS = [
    {"trigger": "reserve", "source": "available", "dest": "reserved"},
    {"trigger": "assign_msisdn", "source": "reserved", "dest": "reserved"},
    {"trigger": "download", "source": "reserved", "dest": "downloaded"},
    {"trigger": "activate", "source": "downloaded", "dest": "activated"},
    {"trigger": "suspend", "source": "activated", "dest": "suspended"},
    {"trigger": "activate", "source": "suspended", "dest": "activated"},
    {"trigger": "recycle", "source": "activated", "dest": "recycled"},
    {"trigger": "release", "source": "reserved", "dest": "available"},
]

TERMINAL = frozenset({"recycled"})


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
