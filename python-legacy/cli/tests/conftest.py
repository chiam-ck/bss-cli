"""Shared CLI test fixtures.

The hero of v0.6 is :func:`assert_snapshot` — every renderer test
that wants the diff itself to be the review artifact pins its
output against a golden file under ``cli/tests/snapshots/``.

Workflow:

* Add a renderer test that calls :func:`assert_snapshot('name', output)`
* Run with ``UPDATE_SNAPSHOTS=1`` to write the initial golden file
* Commit both the renderer change and the golden file in the same PR
* Reviewer reads the diff to confirm the visual change is the intent

See ``docs/runbooks/snapshot-regeneration.md`` for the full workflow.
"""

from __future__ import annotations

import os
from pathlib import Path

_SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def assert_snapshot(name: str, actual: str) -> None:
    """Compare ``actual`` against ``snapshots/<name>.txt`` byte-for-byte.

    Under ``UPDATE_SNAPSHOTS=1`` (env var), writes ``actual`` to the
    golden file instead of comparing — used to (re)generate snapshots
    after an intentional renderer change. Run this from your shell,
    inspect the diff, then commit the new golden alongside the code
    change. Never set the env var in CI.
    """
    path = _SNAPSHOT_DIR / f"{name}.txt"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    if not path.exists():
        raise AssertionError(
            f"snapshot {name!r} missing at {path} — "
            f"run with UPDATE_SNAPSHOTS=1 to create it"
        )
    expected = path.read_text()
    if actual != expected:
        raise AssertionError(
            f"snapshot {name!r} mismatch\n"
            f"--- expected ({path}) ---\n{expected}\n"
            f"--- actual ---\n{actual}\n"
            f"--- end ---\n"
            f"If the change is intentional, run:\n"
            f"  UPDATE_SNAPSHOTS=1 uv run pytest cli/tests/test_renderer_*.py\n"
        )
