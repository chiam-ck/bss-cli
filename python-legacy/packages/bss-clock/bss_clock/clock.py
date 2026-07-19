"""Process-local scenario clock.

Two modes:

- ``"wall"`` — return wall-clock UTC plus an optional ``offset`` that
  ``advance`` can shift. In this mode ``now()`` keeps ticking.
- ``"frozen"`` — return a fixed instant forever until ``unfreeze`` or
  ``advance`` is called. ``advance`` on a frozen clock shifts the frozen
  instant forward (it does *not* resume wall-clock ticking).

State is module-global. Each service process has one clock. Tests use
``unfreeze`` in a fixture teardown to reset state between cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

_Mode = Literal["wall", "frozen"]


@dataclass
class _State:
    mode: _Mode = "wall"
    frozen_at: datetime | None = None
    # Positive offset added to wall clock in "wall" mode. Shifted by
    # ``advance`` calls while not frozen.
    offset: timedelta = field(default_factory=timedelta)


_state = _State()


def now() -> datetime:
    """Return the current time as a tz-aware UTC datetime.

    Use this everywhere instead of ``datetime.now(timezone.utc)``. In
    production it's equivalent to wall-clock UTC; during scenarios it
    reflects whatever freeze/advance commands the runner has issued.
    """
    if _state.mode == "frozen":
        assert _state.frozen_at is not None
        return _state.frozen_at
    return datetime.now(timezone.utc) + _state.offset


def freeze(at: datetime | None = None) -> datetime:
    """Freeze the clock at ``at`` (default: current ``now()``).

    Returns the instant the clock was frozen at. Re-calling ``freeze``
    while already frozen shifts the frozen instant to the new value.
    """
    if at is None:
        at = now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    _state.mode = "frozen"
    _state.frozen_at = at.astimezone(timezone.utc)
    return _state.frozen_at


def unfreeze() -> None:
    """Resume wall-clock ticking (keeps any accumulated offset).

    After ``unfreeze`` the clock returns wall-clock + current offset, so
    if a scenario froze at ``T`` and wants to drop back into live time it
    should call ``unfreeze`` *and* reset any offset if needed (callers
    that want a full reset should use ``reset_for_tests``).
    """
    _state.mode = "wall"
    _state.frozen_at = None


def advance(delta: timedelta | str) -> datetime:
    """Shift the clock forward by ``delta``.

    Accepts either a ``timedelta`` or a string like ``"30d"`` / ``"2h"``
    / ``"15m"`` / ``"45s"``. When frozen, advances the frozen instant;
    when unfrozen, accumulates into ``offset``. Returns the new ``now()``.
    """
    if isinstance(delta, str):
        delta = parse_duration(delta)
    if delta < timedelta(0):
        raise ValueError("advance requires a non-negative duration")

    if _state.mode == "frozen":
        assert _state.frozen_at is not None
        _state.frozen_at = _state.frozen_at + delta
    else:
        _state.offset = _state.offset + delta
    return now()


@dataclass(frozen=True)
class ClockState:
    """Snapshot of the clock for admin/diagnostic responses."""

    mode: _Mode
    now: datetime
    offset_seconds: float
    frozen_at: datetime | None


def state() -> ClockState:
    """Return a read-only snapshot of the current clock state."""
    return ClockState(
        mode=_state.mode,
        now=now(),
        offset_seconds=_state.offset.total_seconds(),
        frozen_at=_state.frozen_at,
    )


def reset_for_tests() -> None:
    """Restore a fresh wall-clock state — test-teardown helper.

    Not exported from ``__init__`` because production callers shouldn't
    ever need it, but tests that frobnicate the clock import this
    directly.
    """
    _state.mode = "wall"
    _state.frozen_at = None
    _state.offset = timedelta()


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")


def parse_duration(value: str) -> timedelta:
    """Parse ``"30d"`` / ``"2h"`` / ``"15m"`` / ``"45s"`` into a timedelta.

    Deliberately narrow — no weeks, no compound strings. Scenarios call
    this with scalar durations; anything more complex should build a
    ``timedelta`` in Python.
    """
    m = _DURATION_RE.match(value)
    if not m:
        raise ValueError(
            f"invalid duration {value!r} — expected '<N><s|m|h|d>' (e.g. '30d')"
        )
    qty = int(m.group(1))
    unit = m.group(2)
    if unit == "s":
        return timedelta(seconds=qty)
    if unit == "m":
        return timedelta(minutes=qty)
    if unit == "h":
        return timedelta(hours=qty)
    return timedelta(days=qty)
