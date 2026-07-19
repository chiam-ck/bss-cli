"""v0.10 PR 11 — every SENSITIVE_ACTION_LABELS entry is used by ≥1 route.

V0_10_0.md Track 10.2 + 10.3:
* Every label in the catalogue must appear in at least one
  ``requires_step_up(...)`` call site under
  ``portals/self-serve/bss_self_serve/routes/``.
* Every ``requires_step_up(...)`` call site uses a label from the
  set (this half is enforced at construction time by
  ``security.requires_step_up`` itself, but we double-check here).

Adding a sensitive route requires extending the catalogue + this
test stays green automatically.
"""

from __future__ import annotations

import re
from pathlib import Path

from bss_self_serve.security import SENSITIVE_ACTION_LABELS

_ROUTES_DIR = Path(__file__).resolve().parents[1] / "bss_self_serve" / "routes"
_CALL_PATTERN = re.compile(r'requires_step_up\(\s*"([^"]+)"\s*\)')


def _all_labels_in_routes() -> set[str]:
    """Walk every routes/*.py + extract requires_step_up('label') strings."""
    labels: set[str] = set()
    for py in _ROUTES_DIR.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        labels.update(_CALL_PATTERN.findall(text))
    return labels


def test_every_sensitive_label_is_used_by_at_least_one_route():
    """Catalogue entries with no call site are dead code or a missed wiring."""
    used = _all_labels_in_routes()
    unused = SENSITIVE_ACTION_LABELS - used
    assert not unused, (
        f"SENSITIVE_ACTION_LABELS contains entries with no requires_step_up "
        f"call site: {sorted(unused)}. Either wire them onto a route or "
        f"remove them from the catalogue."
    )


def test_every_call_site_uses_a_catalogued_label():
    """Defense-in-depth — security.requires_step_up itself rejects unknown
    labels at construction time, but the test catches it at code-review
    time too."""
    used = _all_labels_in_routes()
    unknown = used - SENSITIVE_ACTION_LABELS
    assert not unknown, (
        f"requires_step_up call site uses unknown label(s): {sorted(unknown)}. "
        f"Add them to SENSITIVE_ACTION_LABELS or fix the typo."
    )


def test_no_route_imports_astream_once():
    """v0.11 doctrine — only the chat surface goes through the orchestrator.

    v0.10 carved out post-login self-serve as direct-API; v0.11 extended
    the carve-out to signup. As of v0.11 there is no orchestrator-mediated
    route in the self-serve portal — the chat route lives in v0.12+.
    Greppable: ``rg 'astream_once' portals/self-serve/bss_self_serve/routes/``
    must match the chat route only (empty until chat lands).

    The Makefile doctrine-check enforces this at the project level; this
    test catches it at the test layer too so a contributor sees the
    failure during normal pytest runs without needing make.
    """
    chat_only_whitelist = {
        # v0.12 — the chat surface is the one orchestrator-mediated
        # route. Every other route writes directly via bss-clients.
        "chat.py",
    }
    offenders = []
    for py in _ROUTES_DIR.glob("*.py"):
        if py.name in chat_only_whitelist:
            continue
        text = py.read_text(encoding="utf-8")
        if "astream_once" in text:
            offenders.append(py.name)
    assert not offenders, (
        f"astream_once appears in non-chat route(s): {offenders}. "
        f"Per v0.11 doctrine, only the chat surface goes through the "
        f"orchestrator (CLAUDE.md (v0.11+ / chat only) + DECISIONS "
        f"2026-04-27)."
    )


def test_no_post_login_route_takes_customer_id_from_input():
    """customer_id from request.state only — never form/body/query/path."""
    pattern = re.compile(
        r'customer_id\s*[:=]\s*(?:Form|Body|Query|Path)\('
    )
    offenders = []
    for py in _ROUTES_DIR.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(py.name)
    assert not offenders, (
        f"Route(s) accept customer_id as user input: {offenders}. "
        f"customer_id must come from request.state (bound by the verified "
        f"session) — see CLAUDE.md '(v0.10+)' anti-pattern."
    )
