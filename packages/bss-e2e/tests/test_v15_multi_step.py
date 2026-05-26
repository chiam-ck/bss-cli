"""v1.5 — multi-step cockpit orchestration specs.

Three specs cover the v1.5 unlock:

1. ``test_v15_compound_action_granular`` — operator types a compound prompt,
   the cockpit stages the first destructive as ``pending_destructive``,
   ``/confirm`` resumes the loop, and the SECOND destructive in the same
   compound action gets re-gated (granular mode) so a NEW
   ``pending_destructive`` row appears. The "second pending row" is the
   v1.5 signal — pre-v1.5 the cockpit had no concept of re-gating after
   the first /confirm-resumed destructive fired.

2. ``test_v15_compound_action_batched`` — same operator prompt, same
   /confirm shape, but the e2e stack must run with
   ``BSS_REPL_LLM_AUTONOMY=batched``. After /confirm the loop executes
   BOTH destructives autonomously; NO second pending row appears. Skipped
   by default because the granular default is the v1.5 doctrine; flip the
   env (or use the ``e2e-batched`` make target) to run it.

3. ``test_v15_case_investigation`` — chained reads in one turn (``case.list``
   → ``customer.list`` → ``catalog.list_active_offerings`` → one-sentence
   summary). Tests that the v0.19 "Done." rule is softened — the agent
   actually chains the three reads in a single turn instead of stopping
   after the first. Autonomy-mode-agnostic (no destructives).
"""

from __future__ import annotations

import asyncio
import os
import threading

import asyncpg
import pytest
from playwright.sync_api import expect


def _run_async_in_thread(coro):
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
    page.goto(base_url + "/")
    page.locator("button.cockpit-link-btn").first.click()
    page.wait_for_url("**/cockpit/**", timeout=10_000)
    return page.url.rstrip("/").split("/")[-1]


def _send_message(page, message: str) -> None:
    page.locator("textarea[name=message]").fill(message)
    page.locator("form#thread-compose button[type=submit]").click()


def _wait_for_turn_done(page, timeout_ms: int = 15_000) -> None:
    page.wait_for_function(
        "() => {"
        "  const s = document.querySelector('.thread-stream-status');"
        "  if (!s) return false;"
        "  const t = s.innerText.toLowerCase();"
        "  return t.includes('done') || t.includes('error');"
        "}",
        timeout=timeout_ms,
    )


async def _count_pending_destructive(db_url: str, session_id: str) -> int:
    """Return number of pending_destructive rows for the given session.
    Should be 0 before any destructive proposal; 1 after a propose-then-
    pause; updated to a different tool_name when granular re-gates the
    second destructive in a /confirm-resumed turn."""
    conn = await asyncpg.connect(db_url)
    try:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM cockpit.pending_destructive "
            "WHERE session_id = $1",
            session_id,
        )
        return int(n or 0)
    finally:
        await conn.close()


async def _pending_destructive_tool(
    db_url: str, session_id: str
) -> str | None:
    """Return the tool_name of the pending_destructive row (or None)."""
    conn = await asyncpg.connect(db_url)
    try:
        row = await conn.fetchrow(
            "SELECT tool_name FROM cockpit.pending_destructive "
            "WHERE session_id = $1",
            session_id,
        )
        return row["tool_name"] if row else None
    finally:
        await conn.close()


# ── Spec 1: granular re-gates the second destructive in a compound action ──


@pytest.mark.cockpit
@pytest.mark.skipif(
    os.environ.get("BSS_REPL_LLM_AUTONOMY", "granular").strip().lower()
    == "batched",
    reason=(
        "Granular spec requires the cockpit running in granular mode "
        "(the v1.5 default). The e2e stack is currently configured for "
        "batched — run `make e2e` (without the batched override) to "
        "exercise this spec."
    ),
)
def test_v15_compound_action_granular(page, snap, base_urls):
    """Compound action under granular mode: two destructive proposals,
    each needing its own ``/confirm``.

    Flow:
      * Turn 1 (allow=False) — LLM proposes destructive A → BLOCKED →
        cockpit stages pending_destructive(A).
      * ``/confirm`` POST.
      * Turn 2 (allow=True, granular) — LLM executes A (loop_state=1)
        then proposes destructive B → granular wrapper RE-BLOCKS →
        cockpit stages pending_destructive(B). This is the v1.5 unlock:
        a single /confirm authorises ONLY ONE destructive step.
    """
    base = base_urls["cockpit"]
    db_url = os.environ["BSS_DB_URL"].replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    session_id = _open_new_conversation(page, base)

    # Sanity: brand-new session has no pending row.
    pre = _run_async_in_thread(
        _count_pending_destructive(db_url, session_id)
    )
    assert pre == 0, f"expected 0 pending rows on fresh session; got {pre}"
    snap("new-conversation-no-pending")

    # Turn 1 — operator's compound prompt. Matches the v15-compound-action-
    # turn1 fixture, which proposes a fault-injection destructive.
    _send_message(
        page,
        "plan compound: set fault injection on create_msisdn AND create_esim_profile",
    )
    _wait_for_turn_done(page)

    first_tool = _run_async_in_thread(
        _pending_destructive_tool(db_url, session_id)
    )
    assert first_tool == "provisioning.set_fault_injection", (
        "expected the first destructive (provisioning.set_fault_injection) "
        f"staged as pending after turn 1; got {first_tool!r}"
    )
    snap("turn1-first-pending-staged")

    # /confirm — POST returns 303 (same contract as the v1.4.1 spec).
    response = page.request.post(
        f"{base}/cockpit/{session_id}/confirm", max_redirects=0
    )
    assert response.status == 303, (
        f"expected 303 from /confirm; got {response.status}"
    )

    # The /confirm POST writes the synthetic "(operator typed /confirm — ...)"
    # user message + triggers a new SSE turn from the cockpit_events route.
    # Wait for that turn to finish.
    page.goto(f"{base}/cockpit/{session_id}")
    _wait_for_turn_done(page, timeout_ms=20_000)

    # Granular re-gate: after /confirm, the LLM tried two destructives;
    # the first executed, the second BLOCKED. The cockpit must stage the
    # SECOND as a fresh pending_destructive — different args (different
    # task_type) so we can tell it apart from turn 1's staging.
    second_tool = _run_async_in_thread(
        _pending_destructive_tool(db_url, session_id)
    )
    assert second_tool == "provisioning.set_fault_injection", (
        "expected a NEW pending_destructive row after /confirm-resumed "
        "granular turn (the second destructive in the compound action "
        f"should have been re-gated); got {second_tool!r}"
    )
    snap("turn2-second-pending-restaged")

    # Also verify exactly one row — the v1.5 staging path UPSERTs on
    # (session_id), so the old row should have been replaced not appended.
    post = _run_async_in_thread(
        _count_pending_destructive(db_url, session_id)
    )
    assert post == 1, (
        f"expected exactly 1 pending row after granular re-gate; got {post}"
    )


# ── Spec 2: batched authorises the whole loop after the first /confirm ─────


@pytest.mark.cockpit
@pytest.mark.skipif(
    os.environ.get("BSS_REPL_LLM_AUTONOMY", "granular").strip().lower()
    != "batched",
    reason=(
        "Batched-mode spec requires BSS_REPL_LLM_AUTONOMY=batched at "
        "portal-csr boot. Run the e2e stack with the override set "
        "(or use `make e2e-batched` once it ships in v1.5.1). "
        "Granular is the v1.5 default and is exercised by "
        "test_v15_compound_action_granular."
    ),
)
def test_v15_compound_action_batched(page, snap, base_urls):
    """Compound action under batched mode: one /confirm authorises the loop.

    Flow:
      * Turn 1 — same propose as granular; cockpit stages pending(A).
      * ``/confirm``.
      * Turn 2 (allow=True, batched) — LLM executes A then executes B
        autonomously; both succeed; the cockpit does NOT stage a new
        pending row. Final count of pending_destructive = 0.
    """
    base = base_urls["cockpit"]
    db_url = os.environ["BSS_DB_URL"].replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    session_id = _open_new_conversation(page, base)

    _send_message(
        page,
        "plan compound: set fault injection on create_msisdn AND create_esim_profile",
    )
    _wait_for_turn_done(page)
    snap("turn1-first-pending-staged")

    pre_confirm = _run_async_in_thread(
        _count_pending_destructive(db_url, session_id)
    )
    assert pre_confirm == 1, (
        f"expected 1 pending row after turn 1; got {pre_confirm}"
    )

    response = page.request.post(
        f"{base}/cockpit/{session_id}/confirm", max_redirects=0
    )
    assert response.status == 303

    page.goto(f"{base}/cockpit/{session_id}")
    _wait_for_turn_done(page, timeout_ms=20_000)

    # Batched: both destructives executed; no fresh pending row.
    post = _run_async_in_thread(
        _count_pending_destructive(db_url, session_id)
    )
    assert post == 0, (
        f"batched mode should consume the pending row and NOT stage a "
        f"second one (both destructives executed autonomously); got "
        f"{post} pending rows"
    )
    snap("turn2-both-executed-no-pending")


# ── Spec 3: case investigation (chained reads, softened "Done." rule) ──────


@pytest.mark.cockpit
def test_v15_case_investigation(page, snap, base_urls):
    """Three reads in ONE turn under the softened v0.19 rule.

    The cockpit prompt's pre-v1.5 wording forced "one short sentence
    and STOP" after the first renderer-backed read. v1.5 softens it to
    "one short sentence OR another tool call" — the agent chains
    case.list → customer.list → catalog.list_active_offerings in one
    astream_once turn, then terminates with a one-sentence summary.

    The spec asserts:
      * THREE tool pills rendered (one per chained read).
      * Final assistant bubble is a brief summary, NOT a markdown table
        re-rendering the data (anti-duplication contract intact).
    """
    base = base_urls["cockpit"]
    _open_new_conversation(page, base)
    snap("new-conversation")

    _send_message(page, "investigate the demo state")
    _wait_for_turn_done(page, timeout_ms=25_000)

    # Three tool pills in the order the fixture chains them.
    pills = page.locator(".chat-tool-name")
    expect(pills).to_have_count(3, timeout=10_000)
    expect(pills.nth(0)).to_contain_text("case.list")
    expect(pills.nth(1)).to_contain_text("customer.list")
    expect(pills.nth(2)).to_contain_text("catalog.list_active_offerings")
    snap("three-reads-chained")

    # Terminal assistant bubble — the fixture's summary. The anti-
    # duplication contract is the more important assertion: the bubble
    # must NOT contain a pipe-table re-render of the rows the cockpit
    # already showed as ASCII cards above.
    bubble = page.locator(".chat-bubble-assistant").last
    expect(bubble).to_be_visible()
    bubble_text = bubble.inner_text()
    assert "Investigation complete" in bubble_text, (
        f"expected the fixture's summary in the terminal bubble; got "
        f"{bubble_text!r}"
    )
    # Crude anti-duplication check — a markdown pipe table would carry
    # "| --- |" separators on the header line. The agent must NOT emit
    # those (v0.19 anti-duplication doctrine, preserved into v1.5).
    assert "| ---" not in bubble_text and "|---" not in bubble_text, (
        "terminal bubble re-rendered tool data as a markdown table — "
        "v0.19+ anti-duplication rule violated"
    )
    snap("terminal-bubble-one-sentence-summary")
