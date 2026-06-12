"""v0.17 — PortRequest FSM + policies (pure unit tests, no DB)."""


import pytest
from app.domain import port_request_state
from app.policies import inventory as inv_policies
from app.policies import port_request as pr_policies
from app.policies.base import PolicyViolation


class TestPortRequestFSM:
    def test_requested_to_validated(self):
        assert (
            port_request_state.get_next_state("requested", "validate")
            == "validated"
        )

    def test_requested_to_completed(self):
        assert (
            port_request_state.get_next_state("requested", "complete")
            == "completed"
        )

    def test_validated_to_completed(self):
        assert (
            port_request_state.get_next_state("validated", "complete")
            == "completed"
        )

    def test_requested_to_rejected(self):
        assert (
            port_request_state.get_next_state("requested", "reject")
            == "rejected"
        )

    def test_validated_to_rejected(self):
        assert (
            port_request_state.get_next_state("validated", "reject")
            == "rejected"
        )

    def test_completed_is_terminal(self):
        assert port_request_state.get_next_state("completed", "complete") is None
        assert port_request_state.get_next_state("completed", "reject") is None

    def test_rejected_is_terminal(self):
        assert port_request_state.get_next_state("rejected", "complete") is None


class TestPortRequestPolicies:
    def test_donor_uniqueness_blocks_when_open_exists(self):
        class _FakeOpen:
            id = "PORT-EXISTS"
            state = "requested"

        with pytest.raises(PolicyViolation) as exc:
            pr_policies.check_donor_msisdn_unique("90000001", _FakeOpen())
        assert (
            exc.value.rule
            == "port_request.create.donor_msisdn_unique_among_pending"
        )

    def test_donor_uniqueness_passes_when_none(self):
        pr_policies.check_donor_msisdn_unique("90000001", None)

    def test_direction_must_be_known(self):
        with pytest.raises(PolicyViolation):
            pr_policies.check_direction_valid("port_sideways")
        pr_policies.check_direction_valid("port_in")
        pr_policies.check_direction_valid("port_out")

    def test_port_out_requires_target_subscription(self):
        with pytest.raises(PolicyViolation) as exc:
            pr_policies.check_target_sub_required("port_out", None)
        assert (
            exc.value.rule
            == "port_request.create.target_sub_required_for_port_out"
        )
        # port-in does not require it
        pr_policies.check_target_sub_required("port_in", None)
        pr_policies.check_target_sub_required("port_in", "SUB-001")
        pr_policies.check_target_sub_required("port_out", "SUB-001")

    def test_reject_reason_required(self):
        with pytest.raises(PolicyViolation):
            pr_policies.check_reject_reason("")
        with pytest.raises(PolicyViolation):
            pr_policies.check_reject_reason("   ")
        pr_policies.check_reject_reason("donor carrier denied")

    def test_transition_validity(self):
        with pytest.raises(PolicyViolation):
            pr_policies.check_transition_valid("completed", "complete")
        pr_policies.check_transition_valid("requested", "complete")


class TestMsisdnAddRangePolicies:
    def test_prefix_must_be_digits(self):
        with pytest.raises(PolicyViolation):
            inv_policies.check_sane_prefix("9X00", 10)

    def test_prefix_length_bounded(self):
        with pytest.raises(PolicyViolation):
            inv_policies.check_sane_prefix("123", 10)
        with pytest.raises(PolicyViolation):
            inv_policies.check_sane_prefix("12345678", 10)
        # 4..7 is fine
        inv_policies.check_sane_prefix("9100", 10)
        inv_policies.check_sane_prefix("9100123", 10)

    def test_count_bounds(self):
        with pytest.raises(PolicyViolation):
            inv_policies.check_sane_prefix("9100", 0)
        with pytest.raises(PolicyViolation):
            inv_policies.check_sane_prefix("9100", 10001)
        inv_policies.check_sane_prefix("9100", 1)
        inv_policies.check_sane_prefix("9100", 10000)


class TestMsisdnReleaseTerminal:
    def test_ported_out_release_blocked(self):
        with pytest.raises(PolicyViolation) as exc:
            inv_policies.check_msisdn_releasable("ported_out", "90000001")
        assert exc.value.rule == "msisdn.release.terminal_status"

    def test_reserved_release_allowed(self):
        inv_policies.check_msisdn_releasable("reserved", "90000001")
        inv_policies.check_msisdn_releasable("assigned", "90000001")
