"""File-read step (v0.8) — read a local file, capture substrings.

Used by the auth-flow hero scenarios to fetch the OTP / magic-link
that ``LoggingEmailAdapter`` writes to ``BSS_PORTAL_DEV_MAILBOX_PATH``.
The compose stack volume-mounts that file from the host so the
scenario runner (which runs on the host) can read it directly.

Doctrine fit: this step has no security concern in the scope it
serves — local-file reads on a path the runner explicitly names. It
is NOT a generic shell-out: there's no subprocess, no path traversal
defence beyond ``Path``'s normal semantics, and it's a hard error if
the file is missing past the poll deadline.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .context import ScenarioContext
from .schema import FileReadStep, HTTPRegexCapture


def _resolve_source(result: dict[str, Any], path: str) -> Any:
    node: Any = result
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _capture_regex(
    result: dict[str, Any], captures: dict[str, HTTPRegexCapture]
) -> dict[str, Any]:
    newly: dict[str, Any] = {}
    for var_name, cfg in captures.items():
        source = _resolve_source(result, cfg.source)
        if not isinstance(source, str):
            raise KeyError(
                f"capture_regex {var_name!r}: source {cfg.source!r} "
                f"did not resolve to a string"
            )
        # ``re.findall`` returns the LAST match group via [-1]; for the
        # mailbox file that's the most recent OTP, which is what every
        # caller wants.
        matches = re.findall(cfg.pattern, source, flags=re.MULTILINE)
        if not matches:
            raise KeyError(
                f"capture_regex {var_name!r}: pattern {cfg.pattern!r} "
                f"did not match source"
            )
        last = matches[-1]
        # `re.findall` returns tuples when the pattern has multiple
        # groups; pick the requested group index (1-based, matching
        # capture_regex convention).
        if isinstance(last, tuple):
            newly[var_name] = last[max(cfg.group - 1, 0)]
        else:
            newly[var_name] = last
    return newly


async def run_file_step(
    step: FileReadStep,
    ctx: ScenarioContext,
    *,
    format_error: Callable[[BaseException], str],
):
    """Execute a FileReadStep; returns a StepResult compatible with runner.py."""
    from .runner import StepResult

    t0 = time.monotonic()
    poll = step.poll
    deadline = time.monotonic() + (poll.timeout_seconds if poll else 0)
    interval = max((poll.interval_ms if poll else 0) / 1000.0, 0.05)

    path_str = ctx.interpolate(step.file)
    path = Path(path_str)

    body_text: str | None = None
    captured: dict[str, Any] = {}
    last_err: str = ""

    while True:
        try:
            if path.exists():
                body_text = path.read_text(encoding=step.encoding)
                result = {
                    "path": str(path),
                    "body_text": body_text,
                    "size": path.stat().st_size,
                }
                if step.capture_regex:
                    captured = _capture_regex(result, step.capture_regex)
                break
            last_err = f"file not found: {path}"
        except KeyError as e:
            last_err = str(e)
        except Exception as e:  # noqa: BLE001
            last_err = format_error(e)
        if poll is None or time.monotonic() >= deadline:
            break
        await asyncio.sleep(interval)

    if body_text is None or (step.capture_regex and not captured):
        return StepResult(
            name=step.name,
            kind="file",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            error=last_err or "file read produced no captures",
        )

    for name, value in captured.items():
        ctx.variables[name] = value

    return StepResult(
        name=step.name,
        kind="file",
        ok=True,
        duration_ms=(time.monotonic() - t0) * 1000,
        captured=captured,
        result={"path": str(path), "size": path.stat().st_size},
    )
