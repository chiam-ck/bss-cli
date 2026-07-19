"""Assertion evaluator for ``assert:`` steps and LLM ``expect_final_state``.

Matching rules:

* Keys are dot-paths. ``foo.bar`` walks dict keys. For a list, a segment
  that isn't numeric tries to match ``{type|allowanceType|id|name}`` on
  each list item — so ``balances.data.remaining`` works against
  ``{"balances": [{"allowanceType": "data", "remaining": 0}]}``.
* Numeric segments (``balances.0.remaining``) index the list directly.
* Values are either scalars (equality) or operator dicts:
    ``{eq: 5}`` | ``{ne: ...}`` | ``{gt: 0}`` | ``{gte: 1}`` |
    ``{lt: 100}`` | ``{lte: 10}`` | ``{in: [...]}`` | ``{not_in: [...]}`` |
    ``{starts_with: "llm-"}`` | ``{contains: "sub"}`` | ``{not_null: true}``
* The special key ``any_match`` takes a sub-mapping of expectations that
  must ALL hold on AT LEAST ONE element of a list-typed value (used for
  interaction-log spot checks).

Polling: ``Poll(interval_ms, timeout_seconds)`` retries the full check
until the timeout; the last failure is surfaced if it never passes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .schema import Poll

_SENTINEL = object()


@dataclass
class AssertionFailure:
    """A single path-level mismatch — pretty-printable."""

    path: str
    expected: Any
    actual: Any
    reason: str

    def format(self) -> str:
        return (
            f"  ✗ {self.path}: expected {self.expected!r} "
            f"got {self.actual!r} ({self.reason})"
        )


@dataclass
class AssertionResult:
    ok: bool
    failures: list[AssertionFailure] = field(default_factory=list)
    last_value: Any = None

    def format(self) -> str:
        if self.ok:
            return "✓"
        return "\n".join(f.format() for f in self.failures)


# ─────────────────────────────────────────────────────────────────────────────
# Path traversal
# ─────────────────────────────────────────────────────────────────────────────

_LIST_KEY_CANDIDATES = ("allowanceType", "type", "id", "name", "channel")


def resolve_path(obj: Any, path: str) -> Any:
    """Walk a dot-path, returning ``_SENTINEL`` (not None) on miss.

    Returning a sentinel lets callers distinguish ``None`` (legitimately
    missing field on the payload) from "path did not resolve".
    """
    cur: Any = obj
    for seg in path.split("."):
        if cur is None:
            return _SENTINEL
        if isinstance(cur, list):
            # Numeric index?
            if seg.isdigit():
                idx = int(seg)
                if idx >= len(cur):
                    return _SENTINEL
                cur = cur[idx]
                continue
            # Try matching one of the candidate keys on list items.
            match = None
            for item in cur:
                if not isinstance(item, dict):
                    continue
                if any(item.get(k) == seg for k in _LIST_KEY_CANDIDATES):
                    match = item
                    break
            if match is None:
                return _SENTINEL
            cur = match
            continue
        if isinstance(cur, dict):
            if seg not in cur:
                return _SENTINEL
            cur = cur[seg]
            continue
        return _SENTINEL
    return cur


# ─────────────────────────────────────────────────────────────────────────────
# Matching
# ─────────────────────────────────────────────────────────────────────────────


def _match_value(expected: Any, actual: Any) -> tuple[bool, str]:
    """Return (ok, reason). ``reason`` describes *why* when ok=False."""
    if isinstance(expected, dict):
        return _match_operator(expected, actual)
    if isinstance(expected, list) and isinstance(actual, list):
        if expected != actual:
            return False, "list inequality"
        return True, ""
    if expected == actual:
        return True, ""
    return False, "inequality"


def _match_operator(ops: dict[str, Any], actual: Any) -> tuple[bool, str]:
    for op, expected in ops.items():
        try:
            ok, why = _apply_op(op, expected, actual)
        except TypeError as e:
            return False, f"operator {op!r} type error: {e}"
        if not ok:
            return False, why
    return True, ""


def _apply_op(op: str, expected: Any, actual: Any) -> tuple[bool, str]:
    if op == "eq":
        return (actual == expected, "eq")
    if op == "ne":
        return (actual != expected, "ne")
    if op == "gt":
        return (actual is not None and actual > expected, "gt")
    if op == "gte":
        return (actual is not None and actual >= expected, "gte")
    if op == "lt":
        return (actual is not None and actual < expected, "lt")
    if op == "lte":
        return (actual is not None and actual <= expected, "lte")
    if op == "in":
        return (actual in expected, "in")
    if op == "not_in":
        return (actual not in expected, "not_in")
    if op == "starts_with":
        return (
            isinstance(actual, str) and actual.startswith(expected),
            "starts_with",
        )
    if op == "contains":
        if isinstance(actual, (str, list, tuple, dict)):
            return (expected in actual, "contains")
        return (False, "contains on non-container")
    if op == "not_null":
        want_not_null = bool(expected)
        return ((actual is not None) == want_not_null, "not_null")
    raise ValueError(f"unknown operator: {op!r}")


def evaluate_expect(expect: dict[str, Any], actual: Any) -> AssertionResult:
    """Evaluate every key/value in ``expect`` against ``actual``."""
    failures: list[AssertionFailure] = []
    for path, expected in expect.items():
        if path == "any_match":
            ok, failure = _eval_any_match(expected, actual)
            if not ok:
                failures.append(failure)
            continue
        resolved = resolve_path(actual, path)
        if resolved is _SENTINEL:
            failures.append(
                AssertionFailure(path, expected, None, "path did not resolve")
            )
            continue
        ok, why = _match_value(expected, resolved)
        if not ok:
            failures.append(AssertionFailure(path, expected, resolved, why))
    return AssertionResult(ok=not failures, failures=failures, last_value=actual)


def _eval_any_match(
    expected: dict[str, Any], actual: Any
) -> tuple[bool, AssertionFailure]:
    if not isinstance(actual, list):
        return False, AssertionFailure(
            "any_match", expected, actual, "target is not a list"
        )
    for item in actual:
        inner = evaluate_expect(expected, item)
        if inner.ok:
            return True, AssertionFailure("", "", "", "")
    return False, AssertionFailure(
        "any_match",
        expected,
        f"<{len(actual)} items, none matched>",
        "no element satisfied all sub-expectations",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Polling loop
# ─────────────────────────────────────────────────────────────────────────────


async def poll_until(
    fetch: Callable[[], Awaitable[Any]],
    expect: dict[str, Any],
    poll: Poll | None,
) -> AssertionResult:
    """Run ``fetch()`` + ``evaluate_expect`` until green or timeout."""
    if poll is None:
        value = await fetch()
        return evaluate_expect(expect, value)

    deadline = time.monotonic() + poll.timeout_seconds
    interval = max(poll.interval_ms / 1000.0, 0.01)
    last: AssertionResult | None = None
    while True:
        value = await fetch()
        last = evaluate_expect(expect, value)
        if last.ok:
            return last
        if time.monotonic() >= deadline:
            return last
        await asyncio.sleep(interval)
