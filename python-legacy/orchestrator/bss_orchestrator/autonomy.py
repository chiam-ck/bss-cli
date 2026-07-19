"""v1.5 — operator autonomy mode for the cockpit's multi-step LLM loop.

Mirrors the loyalty-cli pattern at ``cli/loyalty-cli/src/loyalty_cli/repl/llm.py``
(``read_autonomy_mode`` / ``AutonomyMisconfigured``). One env var, two valid
values, fail-closed at process boot.

``BSS_REPL_LLM_AUTONOMY`` (default ``granular``):

- ``granular`` — every destructive step in a compound action gates on its own
  ``/confirm``. Maximum operator control. Behaviour-preserving for every
  call site that existed pre-v1.5.
- ``batched`` — the FIRST destructive step in a ``/confirm``-resumed loop
  gates; subsequent destructive steps in the same loop execute autonomously.
  Operator authorises the plan once and the loop runs to completion.

Unknown values raise ``AutonomyMisconfigured`` at startup — same fail-closed
shape as ``BSS_API_TOKEN=changeme``. Silent default-on-typo is the kind of
quiet contract drift the v0.9 named-token work explicitly refused; this
module honours the same line.

The destructive-tool list (``DESTRUCTIVE_TOOLS`` in ``safety.py``) is
unchanged. The autonomy mode controls *how many* ``/confirm``s a compound
action needs, NOT *which* tools require one. A future
``subscription.terminate`` is still gated by the wrapper either way.

The mode is process-scoped (read once at orchestrator startup, cached on
``app.state``). Per-session ``/autonomy {granular,batched}`` slash command
override is deferred to v1.5.1.
"""

from __future__ import annotations

import os
from typing import Final

VALID_AUTONOMY_MODES: Final[tuple[str, ...]] = ("granular", "batched")
DEFAULT_AUTONOMY_MODE: Final[str] = "granular"

_ENV_VAR: Final[str] = "BSS_REPL_LLM_AUTONOMY"


class AutonomyMisconfigured(RuntimeError):
    """Raised at orchestrator boot if ``BSS_REPL_LLM_AUTONOMY`` is set to an
    unrecognised value. Fail-closed: the process refuses to start rather
    than silently defaulting to ``granular``."""


def read_autonomy_mode() -> str:
    """Read ``BSS_REPL_LLM_AUTONOMY`` from the environment.

    Returns the mode string (one of :data:`VALID_AUTONOMY_MODES`). Unset
    or empty returns :data:`DEFAULT_AUTONOMY_MODE`. Any other value
    raises :class:`AutonomyMisconfigured`.

    Whitespace is stripped and case is normalised before validation, so
    ``BSS_REPL_LLM_AUTONOMY=Batched`` and ``BSS_REPL_LLM_AUTONOMY="  granular  "``
    both load cleanly.
    """
    raw = os.environ.get(_ENV_VAR, "").strip().lower()
    if not raw:
        return DEFAULT_AUTONOMY_MODE
    if raw not in VALID_AUTONOMY_MODES:
        raise AutonomyMisconfigured(
            f"{_ENV_VAR}={raw!r} is not valid. "
            f"Set to one of {sorted(VALID_AUTONOMY_MODES)} or leave unset "
            f"to default to {DEFAULT_AUTONOMY_MODE!r}."
        )
    return raw
