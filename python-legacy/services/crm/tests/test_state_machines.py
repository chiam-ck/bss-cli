"""Unit tests for domain state machines — no DB needed."""

from app.domain import case_state, esim_state, ticket_state


class TestCaseStateMachine:
    def test_open_to_in_progress(self):
        assert case_state.get_next_state("open", "take") == "in_progress"

    def test_resolve_from_open(self):
        assert case_state.get_next_state("open", "resolve") == "resolved"

    def test_resolve_from_in_progress(self):
        assert case_state.get_next_state("in_progress", "resolve") == "resolved"

    def test_close_from_resolved(self):
        assert case_state.get_next_state("resolved", "close") == "closed"

    def test_cancel_from_open(self):
        assert case_state.get_next_state("open", "cancel") == "closed"

    def test_cancel_from_in_progress(self):
        assert case_state.get_next_state("in_progress", "cancel") == "closed"

    def test_cancel_from_pending_customer(self):
        assert case_state.get_next_state("pending_customer", "cancel") == "closed"

    def test_cancel_not_from_resolved(self):
        assert case_state.get_next_state("resolved", "cancel") is None

    def test_cancel_not_from_closed(self):
        assert case_state.get_next_state("closed", "cancel") is None

    def test_invalid_transition(self):
        assert case_state.get_next_state("open", "close") is None
        assert not case_state.is_valid_transition("open", "close")


class TestTicketStateMachine:
    def test_full_happy_path(self):
        assert ticket_state.get_next_state("open", "ack") == "acknowledged"
        assert ticket_state.get_next_state("acknowledged", "start") == "in_progress"
        assert ticket_state.get_next_state("in_progress", "resolve") == "resolved"
        assert ticket_state.get_next_state("resolved", "close") == "closed"

    def test_reopen(self):
        assert ticket_state.get_next_state("resolved", "reopen") == "in_progress"

    def test_cancel_from_open(self):
        assert ticket_state.get_next_state("open", "cancel") == "cancelled"

    def test_cancel_not_from_resolved(self):
        assert ticket_state.get_next_state("resolved", "cancel") is None

    def test_cancel_not_from_closed(self):
        assert ticket_state.get_next_state("closed", "cancel") is None

    def test_terminal_states(self):
        assert "closed" in ticket_state.TERMINAL
        assert "cancelled" in ticket_state.TERMINAL


class TestEsimStateMachine:
    def test_full_lifecycle(self):
        assert esim_state.get_next_state("available", "reserve") == "reserved"
        assert esim_state.get_next_state("reserved", "download") == "downloaded"
        assert esim_state.get_next_state("downloaded", "activate") == "activated"
        assert esim_state.get_next_state("activated", "recycle") == "recycled"

    def test_release_from_reserved(self):
        assert esim_state.get_next_state("reserved", "release") == "available"

    def test_assign_msisdn_stays_reserved(self):
        assert esim_state.get_next_state("reserved", "assign_msisdn") == "reserved"

    def test_suspend_activate(self):
        assert esim_state.get_next_state("activated", "suspend") == "suspended"
        assert esim_state.get_next_state("suspended", "activate") == "activated"

    def test_invalid_transitions(self):
        assert esim_state.get_next_state("available", "activate") is None
        assert esim_state.get_next_state("downloaded", "release") is None
