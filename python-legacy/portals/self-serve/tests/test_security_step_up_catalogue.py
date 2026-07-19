"""v0.10 — SENSITIVE_ACTION_LABELS catalogue + requires_step_up gate.

Locks in:

* The catalogue is a frozenset and contains every label V0_10_0.md
  enumerates as sensitive.
* ``requires_step_up`` rejects unknown labels at dependency
  construction time — protects against typos that would otherwise
  silently disable the step-up gate (the previous failure mode: a
  route that requires_step_up("sub_terminate") would have *failed
  open* because the consume call would never find a matching grant
  and the user would just bounce back through the step-up flow).

The cross-check between the catalogue and route call sites lands in
PR 11 (final doctrine guards), once every PR 3–10 route exists.
"""

from __future__ import annotations

import pytest
from bss_self_serve.security import SENSITIVE_ACTION_LABELS, requires_step_up

EXPECTED_LABELS = frozenset({
    "vas_purchase",
    "payment_method_add",
    "payment_method_remove",
    "payment_method_set_default",
    "subscription_terminate",
    "email_change",
    "phone_update",
    "address_update",
    "name_update",
    "plan_change_schedule",
    "plan_change_cancel",
})


def test_catalogue_is_frozenset():
    assert isinstance(SENSITIVE_ACTION_LABELS, frozenset)


def test_catalogue_matches_v0_10_spec():
    """The phase doc enumerates these ten labels; PR 2 freezes them."""
    assert SENSITIVE_ACTION_LABELS == EXPECTED_LABELS


def test_requires_step_up_accepts_known_label():
    # No exception means the factory accepted the label.
    dep = requires_step_up("vas_purchase")
    assert callable(dep)


def test_requires_step_up_rejects_unknown_label():
    """A typo or unregistered label fails at factory time, not at request time."""
    with pytest.raises(ValueError, match="not in SENSITIVE_ACTION_LABELS"):
        requires_step_up("vas_purchasee")  # deliberate typo
    with pytest.raises(ValueError, match="not in SENSITIVE_ACTION_LABELS"):
        requires_step_up("subscription_pause")  # not in v0.10 surface


def test_every_label_is_lowercase_snake_case():
    """No accidental camelCase or hyphenation in the audit trail."""
    for label in SENSITIVE_ACTION_LABELS:
        assert label.islower()
        assert " " not in label
        assert "-" not in label
