"""State machine tests — parametrized over DECISIONS.md transitions table."""

import pytest
from app.domain.state_machine import (
    ALL_TRIGGERS,
    STATES,
    TERMINAL,
    TRANSITIONS,
    get_next_state,
    is_valid_transition,
)

# ── Valid transitions (from DECISIONS.md Phase 6 table) ─────────────

VALID_CASES = [
    ("pending", "activate", "active"),
    ("pending", "fail_activate", "terminated"),
    ("active", "exhaust", "blocked"),
    ("blocked", "top_up", "active"),
    ("active", "top_up", "active"),
    ("active", "renew", "active"),
    ("active", "renew_fail", "blocked"),
    ("active", "terminate", "terminated"),
    ("blocked", "terminate", "terminated"),
]


@pytest.mark.parametrize(
    "from_state,trigger,expected_dest",
    VALID_CASES,
    ids=[f"{s}-{t}->{d}" for s, t, d in VALID_CASES],
)
def test_valid_transition(from_state: str, trigger: str, expected_dest: str):
    assert is_valid_transition(from_state, trigger) is True
    assert get_next_state(from_state, trigger) == expected_dest


# ── Forbidden transitions ──────────────────────────────────────────

def _forbidden_pairs():
    """All (state, trigger) pairs that are NOT in the transitions table."""
    valid = {(t["source"], t["trigger"]) for t in TRANSITIONS}
    forbidden = []
    for state in STATES:
        for trigger in ALL_TRIGGERS:
            if (state, trigger) not in valid:
                forbidden.append((state, trigger))
    return forbidden


FORBIDDEN_CASES = _forbidden_pairs()


@pytest.mark.parametrize(
    "from_state,trigger",
    FORBIDDEN_CASES,
    ids=[f"{s}-{t}->FORBIDDEN" for s, t in FORBIDDEN_CASES],
)
def test_forbidden_transition(from_state: str, trigger: str):
    assert is_valid_transition(from_state, trigger) is False
    assert get_next_state(from_state, trigger) is None


# ── Terminal state tests ───────────────────────────────────────────

def test_terminated_is_terminal():
    assert "terminated" in TERMINAL


def test_no_transitions_from_terminal():
    for trigger in ALL_TRIGGERS:
        assert is_valid_transition("terminated", trigger) is False


# ── Consistency checks ─────────────────────────────────────────────

def test_all_transition_sources_are_valid_states():
    for t in TRANSITIONS:
        assert t["source"] in STATES, f"Unknown source state: {t['source']}"


def test_all_transition_dests_are_valid_states():
    for t in TRANSITIONS:
        assert t["dest"] in STATES, f"Unknown dest state: {t['dest']}"


def test_transitions_table_matches_decisions_md():
    """Ensure our code has exactly the 9 transitions from the plan."""
    assert len(TRANSITIONS) == 9
    assert len(VALID_CASES) == 9
