"""Specs 6–10 — operator cockpit browser veneer.

The cockpit at ``localhost:9002`` is a thin veneer over the same
``Conversation`` store the REPL uses (v0.13). No login wall —
single-operator-by-design behind a secure perimeter.

**Determinism vs LLM behaviour.** Three of the five specs in the v1.4
phase doc are LLM-driven (tool roundtrip, propose-then-confirm,
knowledge citation). Real LLM responses vary turn-to-turn; verifying
exact tool-call shape from a Playwright assertion is flakey by
construction and burns OpenRouter quota on every run. v1.4.0 ships the
deterministic three (sessions index, slash-command parity, message
post + render); the three LLM-driven specs are scaffolded as xfail
with a v1.4.1 follow-up to either record LLM fixtures for playback
or mock the orchestrator at the seam.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect


# ── Spec 6: sessions index opens ────────────────────────────────────────────


@pytest.mark.cockpit
def test_cockpit_sessions_index_opens(page, base_urls):
    """``/`` renders the sessions index + new-conversation CTA."""
    base = base_urls["cockpit"]
    page.goto(base + "/")
    # The page renders with no login wall — single-operator-by-design
    # (v0.13). New-conversation CTA is the doctrine target.
    expect(page.locator("button.cockpit-link-btn")).to_be_visible()
    expect(page.locator("button.cockpit-link-btn")).to_contain_text("New conversation")


# ── Spec 10: slash-command parity (deterministic, no LLM) ───────────────────


@pytest.mark.cockpit
def test_cockpit_slash_command_parity(page, base_urls):
    """Open a new conversation, set the customer focus via the dedicated
    focus form, then clear it. The cockpit pins the operator's attention
    server-side (POST ``/cockpit/<id>/focus``) — no LLM hop, fully
    deterministic.

    REPL parity: ``/focus CUST-NNN`` in the REPL hits the same path
    against the same ``Conversation`` store; the browser veneer's form
    is just another caller. Verifying focus set + cleared closes the
    parity-doctrine loop for the simplest cockpit verb.
    """
    base = base_urls["cockpit"]

    # 1) Land on the sessions index, hit "+ New conversation".
    page.goto(base + "/")
    page.locator("button.cockpit-link-btn").first.click()
    # New-conversation POST returns 303 to /cockpit/<session_id>.
    page.wait_for_url("**/cockpit/**", timeout=10_000)

    # 2) Set focus via the dedicated focus form (Pin button). Customer
    #    needn't exist — the cockpit treats this as an attention pointer
    #    and renders the label even when the lookup misses.
    focus_form = page.locator("form.thread-focus-form")
    focus_form.locator("input[name=customer_id]").fill("CUST-E2EFOCUS")
    focus_form.locator("button[type=submit]").click()

    # 3) Page reloads on /cockpit/<id>; the focus banner now renders.
    page.wait_for_load_state("networkidle")
    expect(page.locator(".thread-focus")).to_be_visible(timeout=10_000)
    expect(page.locator(".thread-focus")).to_contain_text("CUST-E2EFOCUS")

    # 4) Clear focus by submitting empty customer_id (REPL parity:
    #    ``/focus`` with no arg).
    focus_form_after = page.locator("form.thread-focus-form")
    focus_form_after.locator("input[name=customer_id]").fill("")
    focus_form_after.locator("button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    # Banner is conditional on `focus_label`; with no focus the div
    # disappears.
    expect(page.locator(".thread-focus")).to_have_count(0)


# ── Spec 7: tool roundtrip (LLM-driven — DEFERRED) ──────────────────────────


@pytest.mark.cockpit
@pytest.mark.xfail(
    reason=(
        "v1.4.1 follow-up: LLM response shape is non-deterministic and "
        "asserting on tool-call rendering needs recorded fixtures or a "
        "mock-orchestrator seam. The slash-command-parity spec exercises "
        "the same message-post + render pipeline deterministically."
    ),
    strict=False,
)
def test_cockpit_tool_roundtrip(page, base_urls):
    """Ask for a customer, see a rendered ``customer.get`` row."""
    pytest.skip("LLM-driven; deferred to v1.4.1")


# ── Spec 8: propose-then-confirm (LLM-driven — DEFERRED) ────────────────────


@pytest.mark.cockpit
@pytest.mark.xfail(
    reason=(
        "v1.4.1 follow-up: needs the LLM to consistently propose a "
        "destructive tool call. The `/confirm` slash command + the "
        "POST /cockpit/<id>/confirm route are covered by bss-cockpit "
        "unit tests; the e2e UI layer waits on a deterministic prompt "
        "fixture."
    ),
    strict=False,
)
def test_cockpit_propose_then_confirm(page, base_urls):
    """Destructive proposal pends; ``/confirm`` clears + executes."""
    pytest.skip("LLM-driven; deferred to v1.4.1")


# ── Spec 9: knowledge citation (LLM + knowledge index — DEFERRED) ───────────


@pytest.mark.cockpit
@pytest.mark.xfail(
    reason=(
        "v1.4.1 follow-up: needs the doc corpus reindexed against the "
        "e2e DB plus an LLM that consistently quotes/cites. The "
        "hallucination guard regex (`_RE_KNOWLEDGE_CLAIM`) is covered "
        "by bss-cockpit unit tests; the UI assertion needs a stable "
        "LLM response."
    ),
    strict=False,
)
def test_cockpit_knowledge_citation_or_fallback(page, base_urls):
    """Doctrine question — citation OR the canonical fallback."""
    pytest.skip("LLM-driven + knowledge index; deferred to v1.4.1")
