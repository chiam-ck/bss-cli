"""Unit tests for the v0.13 operator_cockpit tool profile.

The profile is "full registry coverage MINUS the customer-side
``*.mine`` / ``*_for_me`` wrappers". Doctrine: a coverage assertion,
not a restriction set. Tests assert:

* The profile registers and every entry resolves in TOOL_REGISTRY.
* No mine wrapper appears in the cockpit profile.
* Coverage drift catches: every non-mine tool in TOOL_REGISTRY is
  listed in the profile.
* validate_profiles() runs cleanly with the cockpit profile in place.
"""

from __future__ import annotations

import pytest
from bss_orchestrator.tools._profiles import (
    TOOL_PROFILES,
    is_mine_tool,
    validate_profiles,
)
from bss_orchestrator.tools._registry import TOOL_REGISTRY


def test_operator_cockpit_profile_registered() -> None:
    assert "operator_cockpit" in TOOL_PROFILES
    assert len(TOOL_PROFILES["operator_cockpit"]) > 0


def test_every_entry_resolves_in_registry() -> None:
    profile = TOOL_PROFILES["operator_cockpit"]
    missing = sorted(t for t in profile if t not in TOOL_REGISTRY)
    assert not missing, (
        f"operator_cockpit lists tools not in TOOL_REGISTRY: {missing}"
    )


def test_no_mine_wrapper_in_operator_cockpit() -> None:
    profile = TOOL_PROFILES["operator_cockpit"]
    mines = sorted(t for t in profile if is_mine_tool(t))
    assert not mines, (
        f"operator_cockpit must not contain *.mine / *_for_me wrappers; "
        f"found: {mines}"
    )


def test_coverage_assertion_no_drift() -> None:
    """If a new tool ships and isn't added to the cockpit profile,
    this test fails. Forces the conscious-inclusion discipline the
    spec calls out."""
    non_mine = {n for n in TOOL_REGISTRY if not is_mine_tool(n)}
    profile = TOOL_PROFILES["operator_cockpit"]
    not_in_profile = sorted(non_mine - profile)
    assert not not_in_profile, (
        f"new non-mine tool(s) registered but not added to "
        f"operator_cockpit profile: {not_in_profile}. Add them to "
        f"orchestrator/bss_orchestrator/tools/_profiles.py."
    )


def test_validate_profiles_passes() -> None:
    # Should not raise.
    validate_profiles()


def test_validate_profiles_rejects_mine_in_cockpit_profile() -> None:
    """Drift guard: if someone slips a mine wrapper into the cockpit
    profile, validate_profiles must catch it at startup."""
    original = set(TOOL_PROFILES["operator_cockpit"])
    try:
        TOOL_PROFILES["operator_cockpit"] = original | {"customer.get_mine"}
        with pytest.raises(RuntimeError, match=r"\*\.mine"):
            validate_profiles()
    finally:
        TOOL_PROFILES["operator_cockpit"] = original
