"""Specs 6–10 — operator cockpit browser veneer.

The cockpit at ``localhost:9002`` is a thin veneer over the same
``Conversation`` store the REPL uses (v0.13). No login wall —
single-operator-by-design behind a secure perimeter.

**v1.4.1 — mock-orchestrator seam.** Three of these specs are LLM-driven.
Real LLM output varies turn-to-turn and burns OpenRouter quota. v1.4.1
added an ``orchestrator.llm_mock.MockChatModel`` that returns scripted
responses from ``packages/bss-e2e/fixtures/cockpit_e2e.json`` when the
``BSS_LLM_FIXTURE_PATH`` env var is set (the e2e compose override does
exactly that on portal-csr). Tools still execute against the real
services, so the assertions test the cockpit's full rendering pipeline,
not just LLM output.
"""

from __future__ import annotations

import asyncio
import os
import threading

import asyncpg
import pytest
from playwright.sync_api import expect


def _run_async_in_thread(coro):
    """Same isolation trick as conftest.available_msisdn."""
    box: list = []
    err: list = []

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box.append(loop.run_until_complete(coro))
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)
        finally:
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if err:
        raise err[0]
    return box[0]


def _open_new_conversation(page, base_url: str) -> str:
    """Click "+ New conversation" on the sessions index, return the
    session_id from the URL."""
    page.goto(base_url + "/")
    page.locator("button.cockpit-link-btn").first.click()
    page.wait_for_url("**/cockpit/**", timeout=10_000)
    # URL is /cockpit/<session_id> — the suffix path is the session id.
    return page.url.rstrip("/").split("/")[-1]


def _send_message(page, message: str) -> None:
    """Type into the thread compose box and submit. After submit the page
    redirects to /cockpit/<id>?turn=N which auto-attaches the SSE stream."""
    page.locator("textarea[name=message]").fill(message)
    page.locator("form#thread-compose button[type=submit]").click()


def _wait_for_turn_done(page, timeout_ms: int = 15_000) -> None:
    """Wait for the SSE ``thread-stream-status`` element to flip to
    ``done`` (or ``error``). The cockpit emits the status frame as the
    last SSE event of every turn."""
    page.wait_for_function(
        "() => {"
        "  const s = document.querySelector('.thread-stream-status');"
        "  if (!s) return false;"
        "  const t = s.innerText.toLowerCase();"
        "  return t.includes('done') || t.includes('error');"
        "}",
        timeout=timeout_ms,
    )


# ── Spec 6: sessions index opens ────────────────────────────────────────────


@pytest.mark.cockpit
def test_cockpit_sessions_index_opens(page, snap, base_urls):
    """``/`` renders the sessions index + new-conversation CTA."""
    base = base_urls["cockpit"]
    page.goto(base + "/")
    expect(page.locator("button.cockpit-link-btn")).to_be_visible()
    expect(page.locator("button.cockpit-link-btn")).to_contain_text("New conversation")
    snap("sessions-index")


# ── Spec 7: tool roundtrip (LLM → customer.list → render) ──────────────────


@pytest.mark.cockpit
def test_cockpit_tool_roundtrip(page, snap, base_urls):
    """The mocked LLM calls ``customer.list(name_contains="Demo")``; the
    real CRM returns the demo customers; the cockpit renders a tool block
    + an assistant bubble. The spec asserts both surfaces appeared."""
    base = base_urls["cockpit"]
    _open_new_conversation(page, base)
    snap("new-conversation")

    # "list customers" matches the fixture's tool-roundtrip-customer-list
    # entry, which scripts customer.list(name_contains="Demo").
    _send_message(page, "list customers — show me the demo ones")
    _wait_for_turn_done(page)

    expect(page.locator(".chat-tool-name").first).to_contain_text("customer.list")
    expect(page.locator(".chat-bubble-assistant").last).to_be_visible()
    snap("tool-roundtrip-rendered")


# ── Spec 8: propose-then-confirm (destructive gate) ─────────────────────────


@pytest.mark.cockpit
def test_cockpit_propose_then_confirm(page, snap, base_urls):
    """Destructive tool call from the LLM is gated by
    ``safety.DESTRUCTIVE_TOOLS``. The cockpit stages a
    ``pending_destructive`` row in the DB instead of executing; POST
    ``/cockpit/<id>/confirm`` is the operator's acknowledgement marker.

    Spec asserts:
      * After first turn — ``cockpit.pending_destructive`` row exists
        for the session, naming ``provisioning.set_fault_injection``.
      * ``POST /cockpit/<id>/confirm`` returns 303 (parity with the
        REPL ``/confirm`` slash command).
    """
    base = base_urls["cockpit"]
    session_id = _open_new_conversation(page, base)

    # Match the fixture's propose-then-confirm-destructive entry.
    _send_message(
        page, "please set fault injection on the msisdn task"
    )
    _wait_for_turn_done(page)

    # Pending-destructive row should now exist for this session. Read
    # directly from cockpit schema — there's no UI banner for this state
    # (the /confirm button is always visible; the contract is on the
    # DB row).
    db_url = os.environ["BSS_DB_URL"].replace(
        "postgresql+asyncpg://", "postgresql://"
    )

    async def _check_pending() -> dict | None:
        conn = await asyncpg.connect(db_url)
        try:
            row = await conn.fetchrow(
                "SELECT tool_name, tool_args_json::text AS args "
                "FROM cockpit.pending_destructive WHERE session_id = $1",
                session_id,
            )
            return dict(row) if row else None
        finally:
            await conn.close()

    pending = _run_async_in_thread(_check_pending())
    assert pending is not None, (
        "expected a cockpit.pending_destructive row for the session; none found"
    )
    assert pending["tool_name"] == "provisioning.set_fault_injection", (
        f"expected pending tool to be provisioning.set_fault_injection; "
        f"got {pending['tool_name']!r}"
    )
    snap("destructive-proposal-pending")

    # POST /confirm must accept (303 to /cockpit/<id>). The contract is
    # "the operator has a button to press, parity with REPL /confirm";
    # the actual destructive execution would fire on the NEXT astream
    # turn (an operator-typed follow-up message), which is a different
    # spec to write cleanly without race-y multi-turn assertions.
    response = page.request.post(
        f"{base}/cockpit/{session_id}/confirm",
        max_redirects=0,
    )
    assert response.status == 303, (
        f"expected 303 from /cockpit/<id>/confirm; got {response.status}"
    )
    # Navigate to the redirect target so the visual record shows the
    # post-confirm state.
    page.goto(f"{base}/cockpit/{session_id}")
    page.wait_for_load_state("networkidle")
    snap("after-confirm-redirect")


# ── Spec 9: knowledge citation OR fallback (hallucination guard) ───────────


@pytest.mark.cockpit
def test_cockpit_knowledge_citation_or_fallback(page, snap, base_urls):
    """The cockpit's hallucination guard replaces uncited handbook claims
    with the canonical fallback string. Mock returns ``"Per the handbook,
    refunds..."`` with NO knowledge.* tool call; the guard fires; the
    fallback text replaces the bubble before render."""
    base = base_urls["cockpit"]
    _open_new_conversation(page, base)

    _send_message(page, "what's our refund policy?")
    _wait_for_turn_done(page)

    expect(page.locator(".chat-bubble-assistant").last).to_contain_text(
        "I don't have a citation for that"
    )
    snap("hallucination-fallback-rendered")


# ── Spec 10: slash-command parity (/focus) ──────────────────────────────────


@pytest.mark.cockpit
def test_cockpit_slash_command_parity(page, snap, base_urls):
    """Set the customer focus via the dedicated form, then clear it.
    ``/focus`` is server-side (no LLM hop), deterministic in both
    surfaces."""
    base = base_urls["cockpit"]
    page.goto(base + "/")
    page.locator("button.cockpit-link-btn").first.click()
    page.wait_for_url("**/cockpit/**", timeout=10_000)
    snap("new-conversation-no-focus")

    focus_form = page.locator("form.thread-focus-form")
    focus_form.locator("input[name=customer_id]").fill("CUST-E2EFOCUS")
    focus_form.locator("button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator(".thread-focus")).to_be_visible(timeout=10_000)
    expect(page.locator(".thread-focus")).to_contain_text("CUST-E2EFOCUS")
    snap("focus-pinned")

    focus_form_after = page.locator("form.thread-focus-form")
    focus_form_after.locator("input[name=customer_id]").fill("")
    focus_form_after.locator("button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator(".thread-focus")).to_have_count(0)
    snap("focus-cleared")
