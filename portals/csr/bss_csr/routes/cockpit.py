"""Operator cockpit — browser veneer over the v0.13 Conversation store.

Mirrors the CLI REPL. Both surfaces read and write the same Postgres-
backed cockpit.session/message/pending_destructive tables; exit ``bss``,
open ``/cockpit/<id>``, see the same turns. Per phases/V0_13_0.md
§3.2:

    GET  /                              → sessions index
    GET  /cockpit/{session_id}          → chat thread page
    POST /cockpit/{session_id}/turn     → enqueue a user message
    GET  /cockpit/{session_id}/events   → SSE stream
    POST /cockpit/{session_id}/reset    → conversation.reset()
    POST /cockpit/{session_id}/confirm  → flip next turn destructive
    POST /cockpit/{session_id}/focus    → set/clear customer focus
    POST /cockpit/new                   → 303 → /cockpit/<new id>
    GET  /case/{id}                     → kept (read-only deep link)

No login route. No middleware-level auth. The cockpit runs single-
operator-by-design behind a secure perimeter; ``actor`` comes from
``.bss-cli/settings.toml`` via ``bss_cockpit.config.current()``.

Doctrine: this is the only orchestrator-mediated route in the CSR
portal. Doctrine guard ``rg 'astream_once' portals/csr/bss_csr/routes/``
must match cockpit.py only.
"""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from bss_clients.errors import ClientError
from bss_clock import now as clock_now
from bss_cockpit import (
    OPERATOR_ACTOR,
    Conversation,
    ConversationSummary,
    build_cockpit_prompt,
    current as cockpit_config_current,
    knowledge_called,
)
from bss_cockpit.chrome_filter import strip_fake_propose
from bss_cockpit.renderers import render_tool_result
from bss_orchestrator.clients import get_clients
from bss_orchestrator.session import (
    AgentEventError,
    AgentEventFinalMessage,
    AgentEventToolCallCompleted,
    AgentEventToolCallStarted,
    astream_once,
)
from bss_portal_ui import (
    render_assistant_bubble,
    render_chat_markdown,
    render_tool_pill,
    strip_reasoning_leakage,
)
from bss_portal_ui.sse import format_frame as _sse_frame
from bss_portal_ui.sse import status_html as _status_html
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


# ── Destructive-tool prefixes (mirrors cli/bss_cli/repl.py) ──────────


_DESTRUCTIVE_PREFIXES = (
    "subscription.terminate",
    "subscription.migrate_to_new_price",
    "subscription.purchase_vas",
    "subscription.schedule_plan_change",
    "subscription.cancel_pending_plan_change",
    "payment.add_card",
    "payment.remove_method",
    "payment.charge",
    "customer.create",
    "customer.update_contact",
    "customer.attest_kyc",
    "customer.close",
    "customer.add_contact_medium",
    "customer.remove_contact_medium",
    "case.open",
    "case.close",
    "case.add_note",
    "case.transition",
    "case.update_priority",
    "ticket.open",
    "ticket.assign",
    "ticket.transition",
    "ticket.resolve",
    "ticket.close",
    "ticket.cancel",
    "order.create",
    "order.cancel",
    "catalog.add_offering",
    "catalog.add_price",
    "catalog.window_offering",
    "provisioning.resolve_stuck",
    "provisioning.retry_failed",
    "provisioning.set_fault_injection",
)


def _is_destructive(tool_name: str) -> bool:
    return any(
        tool_name == p or tool_name.startswith(p) for p in _DESTRUCTIVE_PREFIXES
    )


# Tools whose return value is rendered as a deterministic ASCII card
# inside the cockpit. When ANY of these fire on a turn, the LLM's
# subsequent assistant bubble is commentary — never the answer. If
# the LLM produces a structural recap (matching the heuristics in
# ``_looks_like_tool_recap`` below), the bubble is replaced with a
# short acknowledgement so the operator never sees a duplicate.
#
# Source of truth is the renderer dispatch — keep this in sync by
# importing the dispatch table.
from bss_cockpit.renderers import RENDERER_DISPATCH as _RENDERED_TOOLS

import re as _re

# Heuristics that flag an assistant bubble as a tool-result mimic.
# Each pattern is conservative — only fires when the bubble is clearly
# trying to reproduce structured tool output, not when the LLM is
# legitimately commenting (e.g., "I've topped up your line — your
# balance now shows X").
_RE_RECAP_PRE_TAG = _re.compile(r"^\s*<pre[^>]*>", _re.IGNORECASE)
_RE_RECAP_HEADERED = _re.compile(
    # A keyword from the canonical Customer 360 / Subscription
    # vocabulary, followed by ``:`` or ``|`` — possibly wrapped in
    # ``**bold**``. Two or more occurrences anywhere in the text is
    # the threshold; below that, a single mention is legitimate
    # commentary, not a recap.
    r"\b(?:Customer|Status|KYC|Since|Contact|"
    r"Subscriptions?|Open\s+Cases?|Recent\s+Interactions?|MSISDN|Plan|"
    r"State|Activated|Renews|Bundle|Balance)\s*[:\|]",
    _re.IGNORECASE,
)

# v0.20+ — citation guard. Mirror of REPL's _RE_KNOWLEDGE_CLAIM. If
# the assistant claims handbook/runbook/doctrine and no knowledge.*
# tool fired this turn, replace with the safe fallback. Browser
# surface fallback is markdown, not Rich, but the message is identical.
_RE_KNOWLEDGE_CLAIM = _re.compile(
    r"\b(?:"
    r"(?:per|according\s+to|as\s+per)\s+(?:the\s+)?"
    r"(?:handbook|runbook|doctrine|"
    r"CLAUDE\.md|ARCHITECTURE\.md|DECISIONS\.md|TOOL_SURFACE\.md|HANDBOOK\.md)"
    r"|(?:the|in\s+the)\s+(?:handbook|runbook|doctrine)\s+"
    r"(?:says|states|specifies|mentions|requires|forbids|allows)"
    r"|the\s+docs?\s+(?:say|state|specify|mention|require|forbid)"
    r")\b",
    _re.IGNORECASE,
)
_KNOWLEDGE_HALLUCINATION_FALLBACK = (
    "I don't have a citation for that. Run `bss admin knowledge search "
    '"<your query>"` or open `docs/HANDBOOK.md` for the authoritative '
    "answer."
)


def _claims_handbook(text: str) -> bool:
    return bool(_RE_KNOWLEDGE_CLAIM.search(text or ""))


def _looks_like_tool_recap(text: str) -> bool:
    """True when the assistant bubble is mimicking a tool result.

    The doctrine in ``_COCKPIT_INVARIANTS`` forbids re-rendering tool
    output, but small models (gemma 4 26b in particular) ignore the
    instruction. This heuristic catches the resulting bubble before
    it reaches the operator's eye:

    * Bubble starts with a literal ``<pre>`` tag — the LLM is trying
      to format ASCII inside HTML, which is a clear mimic signal.
    * Bubble contains multiple structured "Header:" / "**Header:**"
      lines drawn from the canonical Customer 360 / Subscription
      vocabulary. Two-or-more is the threshold so a single
      "Status: active" mention in commentary doesn't false-positive.
    """
    if not text:
        return False
    if _RE_RECAP_PRE_TAG.search(text):
        return True
    matches = _RE_RECAP_HEADERED.findall(text)
    return len(matches) >= 2


def _suppress_tool_recap(
    text: str, captured_tool_calls: list[dict[str, Any]]
) -> str:
    """Replace a tool-recap bubble with a short acknowledgement.

    Only fires when (a) at least one tool with a registered renderer
    fired this turn — so the operator already saw the canonical
    output above the bubble — AND (b) the bubble matches the recap
    heuristics. Otherwise returns ``text`` unchanged.
    """
    if not captured_tool_calls:
        return text
    rendered_tool_fired = any(
        call.get("name") in _RENDERED_TOOLS for call in captured_tool_calls
    )
    if not rendered_tool_fired:
        return text
    if not _looks_like_tool_recap(text):
        return text
    return "(see above)"


def _render_tool_row_as_pre(
    tool_name: str, body: str, *, include_pill: bool = True
) -> str:
    """Render a tool-row body as a unified ``<details open>`` block.

    Single source of truth for tool-result HTML on the browser
    surface — used by both the page-load transcript path and the SSE
    stream. Same shape on both wires; same look-and-feel for the
    operator. Doctrine: tool results never render as markdown / table
    / paraphrase; they render as monospace ASCII inside <pre>.

    Block shape::

        <details class="tool-row" open>
          <summary class="tool-row-summary">
            <span class="tool-row-icon">≈</span>
            <span class="tool-row-name">tool.name</span>
          </summary>
          <pre class="tool-row-body">…rendered ASCII…</pre>
        </details>

    The block opens by default — the operator's most recent question
    just landed; the answer should be immediately visible. Older
    blocks in scrollback can be manually collapsed by the operator.
    The summary is clickable; the chevron is implicit via the
    <details> default UA toggle, restyled in CSS.

    ``body`` is whatever was persisted in ``cockpit.message`` —
    pre-rendered ASCII (REPL stored the rendered card), raw JSON
    (tool with no registered renderer), or — preferred — ASCII
    produced by ``render_tool_result`` at stream time. We re-attempt
    rendering when the body looks like JSON so old conversations
    rendered before the unified renderer landed retroactively get
    nice cards on page reload.

    Newlines in the rendered ASCII are encoded as ``&#10;`` numeric
    character references so the resulting HTML is a single physical
    line. SSE's wire format requires the ``data:`` field be one line,
    and a raw ``\\n`` would split the frame at the wrong boundary and
    drop every line of the card after the first. Inside ``<pre>``,
    the browser parses ``&#10;`` back to a real LF and renders it as
    a line break, so the visible output is identical to the REPL's.

    ``include_pill`` is kept for source compatibility but no longer
    affects output — both paths emit the same block now (the
    surrounding stream logic decides whether to suppress separate
    pill events).
    """
    import html as _html

    rendered: str
    looks_like_json = body.lstrip().startswith(("{", "["))
    if looks_like_json and tool_name:
        rendered = render_tool_result(tool_name, body) or body
    else:
        rendered = body
    escaped = _html.escape(rendered).replace("\n", "&#10;")
    name_html = _html.escape(tool_name or "tool")
    return (
        '<details class="tool-row" open>'
        '<summary class="tool-row-summary">'
        '<span class="tool-row-icon">≈</span>'
        f'<span class="tool-row-name">{name_html}</span>'
        '</summary>'
        f'<pre class="tool-row-body">{escaped}</pre>'
        '</details>'
    )


def _operator_actor() -> str:
    """``actor`` for cockpit turns. Hardcoded in v0.13.1 — perimeter trust."""
    return OPERATOR_ACTOR


async def _load_focus_snapshot(customer_id: str) -> dict[str, Any]:
    """Best-effort customer + subscription snapshot for the focus block.

    Mirrors the v0.5 ``agent_bridge`` pattern: when an operator pins a
    customer for the cockpit session, the system prompt carries enough
    state for the LLM to make a single-shot tool call without rounds
    of discovery. Returns ``{}`` on any read failure — the prompt
    falls back to a focus-only block.
    """
    try:
        clients = get_clients()
        cust = await clients.crm.get_customer(customer_id)
    except (ClientError, Exception):  # noqa: BLE001
        return {}
    individual = cust.get("individual") or {}
    name = " ".join(
        s for s in [individual.get("givenName"), individual.get("familyName")] if s
    ).strip() or customer_id
    snapshot: dict[str, Any] = {
        "customer_id": customer_id,
        "customer_name": name,
        "customer_status": cust.get("status", "?"),
        "kyc_status": cust.get("kycStatus", "?"),
    }
    try:
        subs = await clients.subscription.list_for_customer(customer_id)
    except (ClientError, Exception):  # noqa: BLE001
        subs = []
    if subs:
        # Surface the first sub's state + headline balance row so the
        # LLM doesn't need to call subscription.get to discover that
        # the line is blocked.
        primary = subs[0]
        snapshot["subscription_id"] = primary.get("id", "")
        snapshot["subscription_state"] = primary.get("state", "?")
        snapshot["msisdn"] = primary.get("msisdn", "")
        snapshot["offering_id"] = primary.get("offeringId", "")
        balances = primary.get("balances") or []
        if balances:
            data_row = next(
                (b for b in balances if b.get("type") == "data"
                 or b.get("allowanceType") == "data"),
                balances[0],
            )
            snapshot["data_remaining"] = data_row.get(
                "remaining", data_row.get("used")
            )
            snapshot["data_total"] = data_row.get("total", "")
    return snapshot


# ── Routes ───────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Sessions index — recent operator conversations, time-grouped."""
    cfg = cockpit_config_current()
    rows = await Conversation.list_for(OPERATOR_ACTOR)
    grouped = await _group_sessions_for_index(rows)
    return templates.TemplateResponse(
        request,
        "sessions_index.html",
        {
            "active_page": "sessions",
            "model": cfg.settings.llm.model or "(env default)",
            "grouped_sessions": grouped,
        },
    )


async def _group_sessions_for_index(
    rows: list[ConversationSummary],
) -> list[dict[str, Any]]:
    """Build the time-grouped + name-resolved view for the sessions list.

    For each session we fetch the first user message (cheap — one row
    per session) to use as the human-friendly title, and resolve
    customer focus to a name via crm.get_customer (best-effort; falls
    back to the CUST-id if the lookup fails).
    """
    if not rows:
        return []

    clients = get_clients()

    # 1. Resolve customer focus → name in a single pass.
    focus_ids = sorted({r.customer_focus for r in rows if r.customer_focus})
    focus_name_by_id: dict[str, str] = {}
    for cust_id in focus_ids:
        try:
            cust = await clients.crm.get_customer(cust_id)
            individual = cust.get("individual") or {}
            name = " ".join(
                s for s in [individual.get("givenName"), individual.get("familyName")] if s
            ).strip() or cust.get("name", "") or cust_id
            focus_name_by_id[cust_id] = name
        except Exception:  # noqa: BLE001
            focus_name_by_id[cust_id] = cust_id  # fall back

    # 2. Resolve each session's first user message → title.
    title_by_session: dict[str, str] = {}
    for r in rows:
        title_by_session[r.session_id] = await _first_user_message_title(
            r.session_id, fallback=r.label
        )

    # 3. Time-bucket newest-first. clock_now is the source of truth.
    now = clock_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=7)

    buckets: dict[str, list[dict[str, Any]]] = {
        "Today": [],
        "Yesterday": [],
        "Earlier this week": [],
        "Older": [],
    }

    for r in rows:
        last_active = _aware(r.last_active_at)
        if last_active >= today_start:
            label = "Today"
        elif last_active >= yesterday_start:
            label = "Yesterday"
        elif last_active >= week_start:
            label = "Earlier this week"
        else:
            label = "Older"

        focus_label = (
            focus_name_by_id.get(r.customer_focus)
            if r.customer_focus else None
        )
        buckets[label].append({
            "session_id": r.session_id,
            "title": title_by_session[r.session_id],
            "focus_label": focus_label,
            "last_active_human": _humanize_time(last_active, now),
            "message_count": r.message_count,
        })

    return [
        {"label": label, "rows": rows_in_bucket}
        for label, rows_in_bucket in buckets.items()
        if rows_in_bucket
    ]


async def _first_user_message_title(
    session_id: str, *, fallback: str | None
) -> str:
    """First user message in the session, trimmed for the sessions list.

    Falls back to the operator-provided label, then to a generic
    "(empty conversation)" placeholder.
    """
    # Use the conversation API rather than reach into the store
    # internals, so a future reshape of the message table doesn't
    # break this path.
    try:
        conv = await Conversation.resume(session_id)
        transcript = await conv.transcript_text()
    except Exception:  # noqa: BLE001
        return fallback or "(empty conversation)"
    for block in transcript.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        head, _, body = block.partition("\n")
        if head.strip().rstrip(":") == "user":
            text = body.strip().split("\n", 1)[0]
            if len(text) > 80:
                text = text[:77] + "…"
            return text or fallback or "(empty conversation)"
    return fallback or "(empty conversation)"


def _aware(dt: datetime) -> datetime:
    """Coerce naive datetimes to UTC-aware (Postgres returns aware; tests
    occasionally pass naive — keep the comparison total-orderable)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _humanize_time(when: datetime, now: datetime) -> str:
    """Compact human time: "14:32", "yesterday 09:15", "Apr 23 17:40"."""
    when = _aware(when)
    now = _aware(now)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    if when >= today_start:
        return when.strftime("%H:%M")
    if when >= yesterday_start:
        return f"yesterday {when.strftime('%H:%M')}"
    return when.strftime("%b %d %H:%M")


@router.post("/cockpit/new")
async def new_session(request: Request) -> RedirectResponse:
    """Open a fresh cockpit session and 303 to it."""
    actor = _operator_actor()
    label = (await request.form()).get("label", "") or None
    conv = await Conversation.open(actor=actor, label=label)
    return RedirectResponse(url=f"/cockpit/{conv.session_id}", status_code=303)


@router.get("/cockpit/{session_id}", response_class=HTMLResponse)
async def cockpit_thread(
    request: Request, session_id: str
) -> HTMLResponse:
    """Chat-thread page. Renders prior messages + an input form."""
    try:
        conv = await Conversation.resume(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # v0.13.1 — fetch the structured message rows directly. The prior
    # path serialized to text and re-parsed on \n\n boundaries, which
    # truncated assistant bubbles whose body contained blank lines
    # (e.g., paragraph break + markdown table). Renderers consume
    # ConversationMessage rows now; nothing parses transcript text.
    rows = await conv.list_messages()
    blocks: list[dict[str, Any]] = []
    # v0.20.1 — track the tool calls seen since the most recent user
    # turn so a rehydrated assistant bubble can render pipe tables iff
    # a knowledge.* tool fired in the same turn. Mirrors the live SSE
    # path's ``allow_tables=knowledge_called(captured_tool_calls)``.
    turn_tool_names: list[str] = []
    for row in rows:
        block: dict[str, Any] = {
            "role": row.role,
            "tool_name": row.tool_name or "",
            "body": row.content,
        }
        if row.role == "user":
            turn_tool_names = []
        elif row.role == "tool":
            turn_tool_names.append(row.tool_name or "")
        if row.role == "assistant":
            block["body_html"] = render_chat_markdown(
                row.content,
                allow_tables=knowledge_called(turn_tool_names),
            )
        elif row.role == "tool":
            # Same helper as the SSE stream — both paths produce the
            # same <details open> block, same typography, same
            # collapse/expand affordance. No two-rulesets drift.
            block["body_html"] = _render_tool_row_as_pre(
                row.tool_name or "", row.content
            )
        else:
            block["body_html"] = row.content
        blocks.append(block)

    # Thread title from first user message (no parsing — direct lookup
    # over the structured rows).
    thread_title = next(
        (
            (r.content[:77] + "…") if len(r.content) > 80 else r.content
            for r in rows if r.role == "user"
        ),
        conv.label or "(empty conversation)",
    )
    focus_label: str | None = None
    if conv.customer_focus:
        try:
            cust = await get_clients().crm.get_customer(conv.customer_focus)
            individual = cust.get("individual") or {}
            focus_label = " ".join(
                s for s in [individual.get("givenName"), individual.get("familyName")] if s
            ).strip() or conv.customer_focus
        except Exception:  # noqa: BLE001
            focus_label = conv.customer_focus

    cfg = cockpit_config_current()
    return templates.TemplateResponse(
        request,
        "cockpit_thread.html",
        {
            "active_page": "thread",
            "model": cfg.settings.llm.model or "(env default)",
            "conversation": conv,
            "thread_title": thread_title,
            "focus_label": focus_label,
            "transcript_blocks": blocks,
            "stream_session_id": request.query_params.get("turn", ""),
        },
    )


def _split_transcript_blocks(transcript: str) -> list[dict[str, str]]:
    """Parse ``role:\\ncontent`` blocks for template rendering.

    Mirrors the role mapping the orchestrator uses (user / assistant /
    tool[NAME]). Tool blocks carry the bracketed name so the template
    can render them with a different visual treatment than assistant
    bubbles.
    """
    out: list[dict[str, str]] = []
    if not transcript.strip():
        return out
    for block in transcript.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        head, _, body = block.partition("\n")
        head = head.strip().rstrip(":")
        body = body.strip()
        if not head:
            continue
        if head.startswith("tool"):
            tool_name = ""
            if head.startswith("tool[") and head.endswith("]"):
                tool_name = head[len("tool["):-1]
            out.append({"role": "tool", "tool_name": tool_name, "body": body})
        elif head in {"user", "assistant"}:
            out.append({"role": head, "tool_name": "", "body": body})
    return out


@router.post("/cockpit/{session_id}/turn")
async def cockpit_turn(
    request: Request,
    session_id: str,
    message: str = Form(...),
) -> RedirectResponse:
    """Append the user message and 303 back so the SSE stream picks it up.

    The session_id query param signals the thread template to attach an
    ``hx-ext="sse"`` connection to ``/cockpit/<id>/events`` for the
    next render.
    """
    try:
        conv = await Conversation.resume(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    text = message.strip()
    if not text:
        return RedirectResponse(
            url=f"/cockpit/{session_id}", status_code=303
        )
    await conv.append_user_turn(text)
    # The events endpoint reads the latest user message off the
    # conversation row; no separate turn store needed.
    return RedirectResponse(
        url=f"/cockpit/{session_id}?turn=1", status_code=303
    )


@router.post("/cockpit/{session_id}/reset")
async def cockpit_reset(session_id: str) -> RedirectResponse:
    try:
        conv = await Conversation.resume(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await conv.reset()
    return RedirectResponse(url=f"/cockpit/{session_id}", status_code=303)


@router.post("/cockpit/{session_id}/confirm")
async def cockpit_confirm(session_id: str) -> RedirectResponse:
    """No-op except as a marker. The next turn's SSE handler consumes
    any pending_destructive row; this POST exists so the browser has a
    button to press, parity with the REPL's /confirm slash command."""
    try:
        await Conversation.resume(session_id)  # 404 if missing
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return RedirectResponse(url=f"/cockpit/{session_id}", status_code=303)


@router.post("/cockpit/{session_id}/focus")
async def cockpit_focus(
    session_id: str,
    customer_id: str = Form(default=""),
) -> RedirectResponse:
    try:
        conv = await Conversation.resume(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await conv.set_focus(customer_id.strip() or None)
    return RedirectResponse(url=f"/cockpit/{session_id}", status_code=303)


# ── SSE turn stream ──────────────────────────────────────────────────


@router.get("/cockpit/{session_id}/events")
async def cockpit_events(
    request: Request, session_id: str
) -> StreamingResponse:
    """Drive one cockpit turn through astream_once and yield SSE frames.

    The latest user message on the conversation is the prompt; prior
    turns become transcript context. On AgentEventToolCallStarted, a
    tool-pill frame is emitted; on FinalMessage, the assistant bubble
    is rendered. Pending-destructive proposals captured during the
    stream are stashed via Conversation.set_pending_destructive so the
    next /confirm-bracketed turn can authorise the call.
    """
    try:
        conv = await Conversation.resume(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    cfg = cockpit_config_current()
    actor = OPERATOR_ACTOR

    # Pull the latest user message off the conversation. Bail cleanly
    # if there isn't one (page reload after the turn already streamed).
    transcript = await conv.transcript_text()
    blocks = _split_transcript_blocks(transcript)
    last_user_index: int | None = None
    for i, b in enumerate(blocks):
        if b["role"] == "user":
            last_user_index = i
    if last_user_index is None:
        async def empty():  # noqa: ANN202
            yield _sse_frame("status", _status_html("done"))
        return StreamingResponse(
            empty(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Skip if this user message has already been answered (an assistant
    # turn after it). Page reloads thus don't re-run the LLM.
    answered_after = any(
        blocks[j]["role"] == "assistant" for j in range(last_user_index + 1, len(blocks))
    )
    if answered_after:
        async def replay():  # noqa: ANN202
            yield _sse_frame("status", _status_html("done"))
        return StreamingResponse(
            replay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    user_message = blocks[last_user_index]["body"]

    # Consume any pending_destructive row so build_cockpit_prompt can
    # surface the confirmed-action block.
    pending = await conv.consume_pending_destructive()
    allow_destructive_this_turn = pending is not None

    # v0.13.1 — slash-command interceptor for /confirm typed in the
    # textarea. Some LLMs (gemma) leak tool-call markup as plain text
    # rather than structured tool_calls; when that happens, no
    # ToolCallStarted event fires and pending_destructive never gets
    # stashed. The operator typing /confirm rightly expects "now run
    # the destructive thing" — even without a stashed propose. We
    # honour that intent: if the user message starts with /confirm,
    # this turn runs with allow_destructive=True regardless of the
    # pending row, and we strip the slash command from the prompt.
    # Doctrine note: this widens the trust beat by one turn —
    # acceptable for a single-operator-by-design cockpit behind a
    # secure perimeter (DECISIONS 2026-05-01).
    if user_message.lstrip().lower().startswith("/confirm"):
        allow_destructive_this_turn = True
        # Replace the prompt with a clear authorisation cue so the
        # LLM picks up the prior turn's propose context.
        user_message = (
            "(operator typed /confirm — proceed with the prior "
            "destructive proposal now; call the tool)"
        )

    # Transcript fed into the LLM = everything BEFORE the new user
    # message. The user message itself becomes the prompt.
    prior_blocks = blocks[:last_user_index]
    prior_transcript = _join_blocks(prior_blocks)

    extra_context: dict[str, Any] = {
        "model": cfg.settings.llm.model or "(env default)",
        "session_id": conv.session_id,
    }
    # Mirror v0.5 agent_bridge: when focus is pinned, surface a
    # customer/sub snapshot so the LLM can act in one shot without
    # discovery rounds (some models leak tool-call markup as text
    # when starved of context — DECISIONS 2026-05-01).
    if conv.customer_focus:
        snapshot = await _load_focus_snapshot(conv.customer_focus)
        if snapshot:
            extra_context["focus_snapshot"] = _json.dumps(
                snapshot, separators=(",", ":")
            )

    system_prompt = build_cockpit_prompt(
        operator_md=cfg.operator_md,
        customer_focus=conv.customer_focus,
        pending_destructive=pending,
        extra_context=extra_context,
    )

    captured_tool_calls: list[dict[str, Any]] = []
    last_proposal: tuple[str, dict] | None = None

    async def stream() -> AsyncIterator[bytes]:
        nonlocal last_proposal
        yield _sse_frame("status", _status_html("live"))
        try:
            async for event in astream_once(
                user_message,
                allow_destructive=allow_destructive_this_turn,
                channel="portal-csr",
                actor=actor,
                service_identity="operator_cockpit",
                tool_filter="operator_cockpit",
                system_prompt=system_prompt,
                transcript=prior_transcript,
                autonomy_mode=request.app.state.autonomy_mode,
            ):
                if isinstance(event, AgentEventToolCallStarted):
                    captured_tool_calls.append(
                        {
                            "name": event.name,
                            "args": event.args,
                            "call_id": event.call_id,
                        }
                    )
                    # NOTE: pre-v1.5 set last_proposal here when allow=
                    # False, but that path didn't observe whether the
                    # tool actually got BLOCKED — it inferred it from
                    # the gate flag. v1.5 unifies the staging signal on
                    # the COMPLETED-with-BLOCKED detection below, which
                    # also catches the granular post-confirm path where
                    # allow_destructive_this_turn=True yet the second
                    # destructive re-gates.
                    yield _sse_frame(
                        "message", render_tool_pill(event.name)
                    )
                    continue
                if isinstance(event, AgentEventToolCallCompleted):
                    # v0.19+ Option-1 doctrine — every tool result that
                    # has a registered renderer flows through the shared
                    # `render_tool_result`, gets persisted as a `tool`
                    # row, and is emitted to the browser as a <pre>
                    # block. The LLM's subsequent assistant bubble is
                    # commentary, not the answer; the answer is here.
                    raw = event.result_full or event.result or ""
                    # v1.5 — when a destructive tool's result IS the
                    # structured DESTRUCTIVE_OPERATION_BLOCKED response
                    # (either because allow_destructive=False blocked it
                    # at the gate OR because granular mode re-gated
                    # after a prior destructive in the same loop
                    # already fired), capture it as the proposal to
                    # stage. This is the path that lets the second
                    # destructive in a granular compound action surface
                    # as a fresh /confirm prompt even though we entered
                    # this turn with allow_destructive=True.
                    if (
                        event.name
                        and _is_destructive(event.name)
                        and "DESTRUCTIVE_OPERATION_BLOCKED" in raw
                    ):
                        args = {}
                        for tc in reversed(captured_tool_calls):
                            if tc.get("call_id") == event.call_id:
                                args = tc.get("args", {}) or {}
                                break
                        last_proposal = (event.name, args)
                    rendered = render_tool_result(event.name, raw)
                    if rendered is None:
                        # No registered renderer — surface the raw JSON
                        # verbatim. The LLM is forbidden from "helpfully"
                        # reformatting it; any markdown table coming
                        # back as the assistant bubble is a doctrine bug
                        # that this code path makes visible.
                        rendered = raw
                    if rendered:
                        await conv.append_tool_turn(event.name, rendered)
                        # Suppress the duplicate pill — the stream
                        # already emitted one via ToolCallStarted.
                        yield _sse_frame(
                            "message",
                            _render_tool_row_as_pre(
                                event.name, rendered, include_pill=False
                            ),
                        )
                    continue
                if isinstance(event, AgentEventError):
                    text = "Sorry — something went wrong. Please try again."
                    await conv.append_assistant_turn(
                        text, tool_calls_json=captured_tool_calls or None
                    )
                    yield _sse_frame(
                        "message", render_assistant_bubble(text, error=True)
                    )
                    yield _sse_frame("status", _status_html("error"))
                    return
                if isinstance(event, AgentEventFinalMessage):
                    text = strip_reasoning_leakage(event.text or "")
                    # v0.20.1 — empty terminal AIMessage after tool calls
                    # is a known gemma failure mode. Tell the operator
                    # what fired and how to recover, rather than dropping
                    # an opaque "(no reply)" bubble that looks like a
                    # crashed turn. astream_once's relaxed gate catches
                    # most cases by surfacing intermediate prose; this
                    # fallback covers the residual where the model
                    # produced no text at any phase.
                    if not text:
                        if captured_tool_calls:
                            called = ", ".join(
                                tc["name"] for tc in captured_tool_calls
                            )
                            text = (
                                f"(The model called `{called}` but did not "
                                "synthesise a final answer. Send the same "
                                "question again or rephrase to retry.)"
                            )
                            log.warning(
                                "cockpit.empty_final_after_tool_calls",
                                session_id=session_id,
                                called_tools=[
                                    tc["name"] for tc in captured_tool_calls
                                ],
                            )
                        else:
                            text = "(no reply)"
                    # Defense-in-depth against gemma's tool-recap habit:
                    # if the bubble mimics structured tool output and a
                    # rendered tool fired this turn, replace with a
                    # short acknowledgement. The deterministic ASCII
                    # card already showed; the operator doesn't need
                    # the LLM's prose copy.
                    text = _suppress_tool_recap(text, captured_tool_calls)
                    # v1.5 — anti-mimicry runtime backstop. If the LLM
                    # ignored the CALL TOOLS — DO NOT NARRATE rule, the
                    # strip removes the call shape + surrounding
                    # "I propose ..." narration + leftover backticks.
                    # Then — regardless of remaining bubble content —
                    # surface a stalled-state warning when no real
                    # tool_call fired AND there are mimicry signals.
                    # A half-stripped propose ("I propose to terminate
                    # SUB-0005.") still misleads the operator into
                    # typing /confirm; the explicit replacement is the
                    # only honest UX.
                    text, mimicry_stripped = strip_fake_propose(text)
                    _mentions_confirm = (
                        "/confirm" in text.lower()
                        or "type confirm" in text.lower()
                    )
                    if (
                        not captured_tool_calls
                        and (mimicry_stripped or _mentions_confirm)
                    ):
                        log.warning(
                            "cockpit.anti_mimicry_stall",
                            session_id=session_id,
                            mimicry_stripped=mimicry_stripped,
                            mentions_confirm=_mentions_confirm,
                        )
                        text = (
                            "(The model wrote a propose banner as text "
                            "instead of calling the tool — no pending "
                            "action. Rephrase the request or be more "
                            "direct.)"
                        )
                    # v1.5 — destructive-just-proposed bubble override.
                    # When a destructive tool_call returned BLOCKED this
                    # turn (= we just staged a pending_destructive row),
                    # Gemma's wrap-up is unreliable in three observed
                    # shapes — "Done." (implies action ran; didn't),
                    # "I propose to terminate ... Please type /confirm
                    # to proceed." (mimicry-shaped but paired with a
                    # real tool_call so the stall warning doesn't fire),
                    # and empty. All three are equally misleading. The
                    # staged pending row IS the truth; the bubble must
                    # match. Replace unconditionally with the canonical
                    # "Proposed X(args). Type /confirm to authorise."
                    if last_proposal is not None:
                        _tn, _ta = last_proposal
                        _args_preview = ", ".join(
                            f"{k}={v!r}" for k, v in list(_ta.items())[:3]
                        )
                        text = (
                            f"Proposed {_tn}({_args_preview}). "
                            f"Type /confirm to authorise."
                        )
                    # v0.20+ citation guard. Un-cited handbook claims →
                    # safe fallback. See REPL _RE_KNOWLEDGE_CLAIM mirror.
                    # v0.20.1 — compute via the imported ``knowledge_called``
                    # helper (single source of truth) and bind to a
                    # distinctly named local so the rendering path below
                    # can still call the function. Earlier draft of
                    # v0.20.1 named the local ``knowledge_called``,
                    # shadowing the import; ``hello`` turns crashed with
                    # ``TypeError: 'bool' object is not callable``.
                    called_tool_names = sorted(
                        tc["name"] for tc in captured_tool_calls
                    )
                    knowledge_was_called = knowledge_called(captured_tool_calls)
                    if (
                        text
                        and _claims_handbook(text)
                        and not knowledge_was_called
                    ):
                        log.warning(
                            "cockpit.knowledge_hallucination",
                            session_id=session_id,
                            called_tools=called_tool_names,
                        )
                        text = _KNOWLEDGE_HALLUCINATION_FALLBACK
                    asst_id = await conv.append_assistant_turn(
                        text, tool_calls_json=captured_tool_calls or None
                    )
                    # v1.5 — stage whenever a destructive proposal landed,
                    # regardless of the per-turn allow_destructive flag. The
                    # last_proposal is set in two paths today:
                    #   1) STARTED on allow=False (pre-v1.5 behaviour): the
                    #      LLM proposes, the wrapper would BLOCK so the
                    #      cockpit pre-empts the blocked round trip and
                    #      stages the proposal directly off the started event.
                    #   2) COMPLETED with BLOCKED in the result (v1.5
                    #      granular path): the LLM proposed a second
                    #      destructive in a /confirm-resumed turn; the
                    #      wrapper re-gated and returned BLOCKED, so the
                    #      cockpit needs to stage even though
                    #      allow_destructive_this_turn was True.
                    # Both paths converge here. Gating the staging on
                    # allow_destructive_this_turn (pre-v1.5 behaviour) would
                    # drop case (2) silently.
                    if last_proposal is not None:
                        tn, ta = last_proposal
                        await conv.set_pending_destructive(
                            tool_name=tn,
                            args=ta,
                            proposal_message_id=asst_id,
                        )
                    # v0.20.1 — opt into pipe-table rendering when a
                    # renderer-less knowledge tool fired this turn.
                    # Mirrors the v0.20 carve-out for the anti-recap
                    # rule: ``knowledge.*`` has no ASCII renderer, so
                    # the LLM's prose IS the answer; tables relayed
                    # from the handbook should render as <table>.
                    yield _sse_frame(
                        "message",
                        render_assistant_bubble(
                            text,
                            allow_tables=knowledge_was_called,
                        ),
                    )
                    yield _sse_frame("status", _status_html("done"))
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("cockpit.stream_crashed")
            yield _sse_frame("status", _status_html("error"))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _join_blocks(blocks: list[dict[str, str]]) -> str:
    """Inverse of ``_split_transcript_blocks`` — render back to the canonical
    ``role:\\ncontent\\n\\nrole:\\ncontent`` form."""
    parts: list[str] = []
    for b in blocks:
        if b["role"] == "tool":
            head = (
                f"tool[{b['tool_name']}]" if b["tool_name"] else "tool"
            )
        else:
            head = b["role"]
        parts.append(f"{head}:\n{b['body']}")
    return "\n\n".join(parts)
