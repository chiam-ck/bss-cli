"""v1.5 — unit tests for the autonomy-mode env reader.

The contract: ``read_autonomy_mode()`` returns one of ``granular`` /
``batched``, defaults to ``granular`` when unset, and raises
``AutonomyMisconfigured`` on anything else. Fail-closed validation is
the design — silent default-on-typo would make a misconfigured prod
deployment look identical to a correctly-defaulted dev one.
"""

from __future__ import annotations

import pytest

from bss_orchestrator.autonomy import (
    DEFAULT_AUTONOMY_MODE,
    VALID_AUTONOMY_MODES,
    AutonomyMisconfigured,
    read_autonomy_mode,
)


def test_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BSS_REPL_LLM_AUTONOMY", raising=False)
    assert read_autonomy_mode() == DEFAULT_AUTONOMY_MODE
    assert read_autonomy_mode() == "granular"


def test_default_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BSS_REPL_LLM_AUTONOMY", "")
    assert read_autonomy_mode() == DEFAULT_AUTONOMY_MODE


def test_default_when_whitespace_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BSS_REPL_LLM_AUTONOMY", "   ")
    assert read_autonomy_mode() == DEFAULT_AUTONOMY_MODE


@pytest.mark.parametrize("mode", VALID_AUTONOMY_MODES)
def test_valid_modes_load(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BSS_REPL_LLM_AUTONOMY", mode)
    assert read_autonomy_mode() == mode


@pytest.mark.parametrize(
    "raw,normalised",
    [
        ("GRANULAR", "granular"),
        ("Batched", "batched"),
        ("  granular  ", "granular"),
        ("\tbatched\n", "batched"),
    ],
)
def test_case_and_whitespace_normalisation(
    raw: str, normalised: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BSS_REPL_LLM_AUTONOMY", raw)
    assert read_autonomy_mode() == normalised


@pytest.mark.parametrize(
    "bad",
    [
        "off",
        "on",
        "auto",
        "yolo",
        "granuler",  # typo
        "batch",  # close-but-wrong
        "granular,batched",
        "1",
        "true",
        "none",
    ],
)
def test_unknown_value_raises(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BSS_REPL_LLM_AUTONOMY", bad)
    with pytest.raises(AutonomyMisconfigured) as exc_info:
        read_autonomy_mode()
    # The error message MUST name the env var, the bad value, and the
    # valid set — operators reading the boot crash should see all three
    # without having to grep the source.
    msg = str(exc_info.value)
    assert "BSS_REPL_LLM_AUTONOMY" in msg
    assert repr(bad.strip().lower()) in msg
    assert "granular" in msg
    assert "batched" in msg


def test_valid_modes_constant_stays_canonical() -> None:
    # If someone adds a third mode they MUST think about what it means
    # for the safety contract — this test fails loudly so the change
    # surfaces in code review.
    assert VALID_AUTONOMY_MODES == ("granular", "batched")
    assert DEFAULT_AUTONOMY_MODE == "granular"
