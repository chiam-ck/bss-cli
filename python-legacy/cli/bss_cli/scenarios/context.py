"""Variable interpolation + capture for scenarios.

Scenarios can reference earlier values via ``{{ var }}`` anywhere in step
args or ``ask:`` prompt strings. The runner builds a ``ScenarioContext``
seeded with ``setup.variables`` + ``variables`` + a synthetic ``run_id``
(short hex), and every successful step's ``capture`` entries are merged
back in via ``apply_captures``.

Interpolation is intentionally plain — no filters, no Jinja expressions.
If a scenario needs a transform it can capture a fresh value in a later
step via an explicit ``action:``.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from jsonpath_ng.ext import parse as jsonpath_parse

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass
class ScenarioContext:
    """Runtime variable bag threaded through every step."""

    variables: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls, *, run_id: str | None = None, seed: dict[str, Any] | None = None
    ) -> "ScenarioContext":
        ctx = cls(variables={"run_id": run_id or secrets.token_hex(4)})
        # Resolve seed values against already-defined vars so scenarios can
        # write e.g. ``customer_email: "ck-{{ run_id }}@bss-cli.local"``.
        for k, v in (seed or {}).items():
            ctx.variables[k] = ctx.interpolate(v)
        return ctx

    # ── Interpolation ───────────────────────────────────────────────────

    def interpolate(self, value: Any) -> Any:
        """Recursively substitute ``{{ name }}`` placeholders inside strings,
        lists, and dicts. Non-string leaves (ints, bools, None) pass through."""
        if isinstance(value, str):
            return self._interpolate_str(value)
        if isinstance(value, list):
            return [self.interpolate(v) for v in value]
        if isinstance(value, dict):
            return {k: self.interpolate(v) for k, v in value.items()}
        return value

    def _interpolate_str(self, s: str) -> Any:
        # Special case: the whole string IS one placeholder → preserve the
        # captured value's original type (useful when capturing ints, lists).
        m = _VAR_RE.fullmatch(s.strip())
        if m:
            return self._resolve(m.group(1))

        def _sub(m: re.Match[str]) -> str:
            return str(self._resolve(m.group(1)))

        return _VAR_RE.sub(_sub, s)

    def _resolve(self, name: str) -> Any:
        if name not in self.variables:
            raise KeyError(f"undefined scenario variable: {name!r}")
        return self.variables[name]

    # ── Capture ─────────────────────────────────────────────────────────

    def apply_captures(
        self, result: Any, captures: dict[str, str]
    ) -> dict[str, Any]:
        """Evaluate each jsonpath against ``result`` and merge into the bag.

        Returns the newly-captured keys for reporting. Missing / empty path
        matches raise ``KeyError`` so scenarios fail loud on selector drift.
        """
        newly: dict[str, Any] = {}
        for var_name, path_expr in captures.items():
            matches = jsonpath_parse(path_expr).find(result)
            if not matches:
                raise KeyError(
                    f"capture {var_name!r}: jsonpath {path_expr!r} "
                    f"matched nothing in tool result"
                )
            value = matches[0].value
            self.variables[var_name] = value
            newly[var_name] = value
        return newly

    def snapshot(self) -> dict[str, Any]:
        """Return a shallow copy for reporting/debug dumps."""
        return dict(self.variables)
