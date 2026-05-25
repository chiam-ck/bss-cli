"""Specs 6–10 — operator cockpit browser veneer.

The cockpit at ``localhost:9002`` is a thin veneer over the same
``Conversation`` store the REPL uses (v0.13). No login wall —
single-operator-by-design behind a secure perimeter.

* **Sessions index opens** — ``/`` lists recent conversations, new-CTA
  navigates to ``/cockpit/<id>``.
* **Tool roundtrip** — operator asks for a customer, cockpit returns a
  rendered ``customer.get`` row.
* **Propose-then-confirm** — destructive proposal pends; ``/confirm``
  executes.
* **Knowledge citation or fallback** — "what's our refund policy?" cites
  a doctrine source OR returns the canonical fallback (guard 16).
* **Slash-command parity** — ``/focus`` and ``/clear`` work in the
  browser, mirroring the REPL.

All five are placeholders during v1.4.0 phase 1.
"""

from __future__ import annotations

import pytest


@pytest.mark.cockpit
def test_cockpit_sessions_index_opens(page, base_urls):
    """``/`` renders the sessions index + new-conversation CTA."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")


@pytest.mark.cockpit
def test_cockpit_tool_roundtrip(page, base_urls):
    """Ask for a customer, see a rendered ``customer.get`` row."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")


@pytest.mark.cockpit
def test_cockpit_propose_then_confirm(page, base_urls):
    """Destructive proposal pends; ``/confirm`` clears + executes."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")


@pytest.mark.cockpit
def test_cockpit_knowledge_citation_or_fallback(page, base_urls):
    """Doctrine question — citation OR the canonical fallback."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")


@pytest.mark.cockpit
def test_cockpit_slash_command_parity(page, base_urls):
    """``/focus`` and ``/clear`` work in the browser."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")
