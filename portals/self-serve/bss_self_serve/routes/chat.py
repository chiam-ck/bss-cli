"""Customer chat surface (v0.12 PR7 + PR13 conversation memory + popup widget).

The only orchestrator-mediated route in the self-serve portal — every
other route writes directly via bss-clients per the v0.10 / v0.11
doctrine. The doctrine guard ``check-chat-only`` in the Makefile
asserts ``astream_once`` appears only here.

Routes:

* ``GET /chat`` — the standalone chat page (full window). Renders
  the running conversation if any.
* ``GET /chat/widget`` — the same UI as a fixed-position bottom-right
  popup partial. The body-level FAB in base.html loads this via
  ``hx-get`` so the customer can chat from any post-login page
  without navigating away.
* ``POST /chat/message`` — form submission. Cap-check, append the
  user's message to the conversation, create a turn, and either
  redirect (full-page form) or return an HTMX widget refresh
  partial that opens the SSE stream in place.
* ``POST /chat/reset`` — clear the running conversation. Returns a
  fresh widget partial when called via HTMX; otherwise 303 to /chat.
* ``GET /chat/events/{sid}`` — SSE stream. Reads turn + the
  customer's running conversation, runs astream_once with prior
  context inline, on FinalMessage appends the assistant reply to
  the conversation so the next turn sees it.

Doctrine: cap-trip → templated response, no LLM invocation.
``AgentOwnershipViolation`` → generic safety reply, no leaked detail.
``AgentEventTurnUsage`` → ``chat_caps.record_chat_turn`` (cost
accounting). ``AgentEventFinalMessage`` rendered in full — the
chat surface owns the user-visible reply, so unlike the agent-log
widget it does not truncate.
"""

from __future__ import annotations

import asyncio
import re as _re
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import structlog
from bss_orchestrator.chat_caps import check_caps, record_chat_turn
from bss_orchestrator.customer_chat_prompt import (
    build_balance_summary,
    build_customer_chat_prompt,
)
from bss_orchestrator.session import (
    AgentEventError,
    AgentEventFinalMessage,
    AgentEventToolCallStarted,
    AgentEventTurnUsage,
    astream_once,
)
from bss_portal_auth import IdentityView
from bss_portal_ui import (
    render_assistant_bubble as _chat_assistant_html,
)
from bss_portal_ui import (
    render_chat_markdown as _render_chat_markdown,
)
from bss_portal_ui import (
    render_tool_pill as _chat_tool_pill_html,
)
from bss_portal_ui import (
    strip_reasoning_leakage as _strip_reasoning_leakage,
)
from bss_portal_ui.sse import format_frame as _sse_frame
from bss_portal_ui.sse import status_html as _status_html
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..clients import get_clients
from ..security import requires_verified_email
from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


_OWNERSHIP_VIOLATION_REPLY = (
    "Sorry — I couldn't complete that. Please try again, or contact "
    "support if the issue persists."
)

# v0.13.1 — anti-hallucination. Detect FIRST-PERSON ACTIVE escalation
# claims ("I've escalated this", "I'm raising a case") and verify
# case.open_for_me actually fired this turn. Past-tense / third-person
# language ("your case has been raised", "case ID is CASE-123") is
# the LLM legitimately recapping a prior escalation; we don't want
# to false-positive on those when the customer asks "what's my case
# ID" later.
_RE_ESCALATION_CLAIM = _re.compile(
    r"\bI(?:'ve| have| am| will|'ll|'m)?\s+(?:"
    r"escalat\w*"
    r"|(?:raised?|opened?|filed?|raising|opening|filing)\s+"
    r"(?:a|the|your)?\s*case"
    r")\b",
    _re.IGNORECASE,
)
_ESCALATION_HALLUCINATION_FALLBACK = (
    "I can't take this further on my own — please email support directly "
    "at {email} so a human agent can look into it. Sorry for the extra "
    "step."
)


def _claims_escalation(text: str) -> bool:
    """True if the assistant's reply claims to have escalated."""
    return bool(_RE_ESCALATION_CLAIM.search(text or ""))


# ── Helpers ──────────────────────────────────────────────────────────


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _cap_key(identity: IdentityView) -> str:
    """Stable per-identity key for chat_caps + ChatConversationStore.

    Linked customers cap on the customer_id (existing v0.12 contract).
    Verified-but-unlinked identities cap on the identity_id with an
    ``anon-`` prefix so they never collide with a CUST-* id and the
    audit row's customer_id field reflects the (anonymous) reality.
    Pre-signup catalog enquiries get their own budget; signing up
    after that doesn't merge histories — clean break, by design.
    """
    if identity.customer_id:
        return identity.customer_id
    return f"anon-{identity.id}"


def _chat_actor(identity: IdentityView) -> str | None:
    """auth_context.actor binding. Linked customers get their CUST-*
    so .mine wrappers resolve. Anonymous (pre-signup) identities get
    None — the .mine wrappers refuse cleanly via _NoActorBound, the
    catalog-public reads still work, the system prompt steers the
    LLM accordingly."""
    return identity.customer_id


async def _load_customer_context(customer_id: str | None) -> dict:
    """Read customer + primary subscription for the system prompt.
    Best-effort: failures fall through to ``(loading)`` placeholders
    rather than blocking the chat turn.

    For unlinked (pre-signup) identities ``customer_id`` is None;
    no BSS reads happen and the prompt renders in browse-only mode.
    """
    customer_dict: dict = {}
    primary_sub: dict | None = None
    customer_email = ""
    customer_name = ""
    plan_id = "(loading)"
    if not customer_id:
        return {
            "customer_dict": customer_dict,
            "primary_sub": primary_sub,
            "customer_email": customer_email,
            "customer_name": customer_name,
            "plan_id": plan_id,
            "is_linked": False,
        }
    clients = get_clients()
    try:
        customer_dict = await clients.crm.get_customer(customer_id)
        customer_email = (
            customer_dict.get("email")
            or customer_dict.get("primaryEmail")
            or ""
        )
        customer_name = (
            customer_dict.get("name")
            or customer_dict.get("givenName")
            or "there"
        )
        subs = await clients.subscription.list_for_customer(customer_id)
        primary_sub = next(
            (s for s in subs if s.get("state") in {"active", "blocked"}),
            subs[0] if subs else None,
        )
        if primary_sub:
            plan_id = primary_sub.get("offeringId") or plan_id
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "chat.prompt_context_load_failed",
            customer_id=customer_id,
            error=str(exc),
        )
    return {
        "customer_dict": customer_dict,
        "primary_sub": primary_sub,
        "customer_email": customer_email,
        "customer_name": customer_name,
        "plan_id": plan_id,
        "is_linked": True,
    }


def _render_widget_context(
    *,
    customer_id: str,
    conversation,
    session_id: str | None,
    cap_tripped: str | None,
    retry_at: str | None,
) -> dict:
    """Shared template context for both the standalone page and the
    popup widget. Assistant bodies are pre-rendered through the
    chat-markdown converter (``body_html``) so on a page reload the
    bubbles look identical to live-streamed ones; the user-side
    body is left raw and Jinja escapes it via the default filter."""
    messages = []
    if conversation is not None:
        for m in conversation.messages:
            entry: dict[str, str] = {"role": m.role, "body": m.body}
            if m.role == "assistant":
                entry["body_html"] = _render_chat_markdown(m.body)
            messages.append(entry)
    return {
        "customer_id": customer_id,
        "session_id": session_id,
        "messages": messages,
        "has_history": bool(messages),
        "cap_tripped": cap_tripped,
        "retry_at": retry_at,
    }


# ── Routes ───────────────────────────────────────────────────────────


async def _resolve_session(
    request: Request, session: str | None, cap_key: str
) -> str | None:
    """Validate ``?session=<sid>`` against the turn store. Returns
    the id when valid + owned by the actor, otherwise ``None`` so
    the template skips the SSE host. Cross-customer (or cross-
    anonymous) impersonation via a crafted URL is blocked here
    (the SSE handler enforces too)."""
    if not session:
        return None
    turn = await request.app.state.chat_turn_store.get(session)
    if turn is None or turn.customer_id != cap_key:
        return None
    return session


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    cap_tripped: str | None = None,
    retry_at: str | None = None,
    session: str | None = None,
    identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    """Standalone chat page. Available to any verified-email
    identity; linked-customer status determines which tools the
    LLM can reach (``.mine`` wrappers refuse without a customer)."""
    cap_key = _cap_key(identity)
    conv = await request.app.state.chat_conversation_store.get(cap_key)
    valid_session = await _resolve_session(request, session, cap_key)
    return templates.TemplateResponse(
        request,
        "chat_page.html",
        _render_widget_context(
            customer_id=cap_key,
            conversation=conv,
            session_id=valid_session,
            cap_tripped=cap_tripped,
            retry_at=retry_at,
        ),
    )


@router.get("/chat/widget", response_class=HTMLResponse)
async def chat_widget(
    request: Request,
    session: str | None = None,
    cap_tripped: str | None = None,
    retry_at: str | None = None,
    identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    """The popup widget partial. Loaded by the FAB's ``hx-get``
    into the ``#chat-widget-host`` div on every page where the
    customer has a verified-email session. Linked or anonymous —
    both can browse the catalog via chat; .mine writes are gated
    by the wrapper's actor check."""
    cap_key = _cap_key(identity)
    conv = await request.app.state.chat_conversation_store.get(cap_key)
    valid_session = await _resolve_session(request, session, cap_key)
    return templates.TemplateResponse(
        request,
        "chat_widget.html",
        _render_widget_context(
            customer_id=cap_key,
            conversation=conv,
            session_id=valid_session,
            cap_tripped=cap_tripped,
            retry_at=retry_at,
        ),
    )


@router.post("/chat/message")
async def chat_message(
    request: Request,
    message: str = Form(...),
    identity: IdentityView = Depends(requires_verified_email),
):
    """Append the user's message to the conversation and either
    redirect (full-page) or return a widget refresh (HTMX) so the
    SSE stream picks up the new turn."""
    cap_key = _cap_key(identity)
    text = message.strip()
    if not text:
        if _is_htmx(request):
            return await chat_widget(request, identity=identity)
        return RedirectResponse(url="/chat", status_code=303)

    cap = await check_caps(cap_key)
    if not cap.allowed:
        params: dict[str, str] = {"cap_tripped": cap.reason or "cap_check_failed"}
        if cap.retry_at is not None:
            params["retry_at"] = cap.retry_at.isoformat()
        log.info(
            "chat.cap_tripped",
            cap_key=cap_key,
            reason=cap.reason,
        )
        if _is_htmx(request):
            return await chat_widget(
                request,
                identity=identity,
                cap_tripped=cap.reason or "cap_check_failed",
                retry_at=cap.retry_at.isoformat() if cap.retry_at else None,
            )
        return RedirectResponse(
            url=f"/chat?{urlencode(params)}", status_code=303
        )

    conv_store = request.app.state.chat_conversation_store
    conv = await conv_store.get_or_create(cap_key)
    conv.append("user", text)

    turn_store = request.app.state.chat_turn_store
    turn = await turn_store.create(customer_id=cap_key, question=text)

    if _is_htmx(request):
        return await chat_widget(
            request, session=turn.session_id, identity=identity
        )
    return RedirectResponse(
        url=f"/chat?session={turn.session_id}", status_code=303
    )


@router.post("/chat/reset")
async def chat_reset(
    request: Request,
    identity: IdentityView = Depends(requires_verified_email),
):
    """Clear the running conversation. The next message starts a
    fresh history — no prior context."""
    cap_key = _cap_key(identity)
    await request.app.state.chat_conversation_store.reset(cap_key)
    if _is_htmx(request):
        return await chat_widget(request, identity=identity)
    return RedirectResponse(url="/chat", status_code=303)


@router.get("/chat/events/{session_id}")
async def chat_events(
    request: Request,
    session_id: str,
    identity: IdentityView = Depends(requires_verified_email),
) -> StreamingResponse:
    """SSE stream for one in-flight chat turn.

    Loads prior turns of the running conversation so the system
    prompt carries context — the LLM answers the next user message
    in continuity. On FinalMessage, the assistant's reply lands
    in the conversation so subsequent turns see it.
    """
    cap_key = _cap_key(identity)
    chat_actor = _chat_actor(identity)

    turn_store = request.app.state.chat_turn_store
    turn = await turn_store.get(session_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="chat turn not found")
    if turn.customer_id != cap_key:
        log.warning(
            "chat.cross_customer_session_attempt",
            actor=cap_key,
            session_id=session_id,
            owner=turn.customer_id,
        )
        raise HTTPException(status_code=403, detail="not your chat session")

    conv_store = request.app.state.chat_conversation_store
    conv = await conv_store.get_or_create(cap_key)

    ctx = await _load_customer_context(chat_actor)
    primary_sub = ctx["primary_sub"]

    # Prior messages = everything in the conversation EXCEPT the
    # latest user message (the one this turn is answering — the
    # LLM sees it as ``prompt``, not as prior context).
    prior_pairs: list[tuple[str, str]] = [
        (m.role, m.body)
        for m in conv.messages
        if not (m.role == "user" and m.body == turn.question)
    ]

    portal_settings = request.app.state.settings
    system_prompt = build_customer_chat_prompt(
        customer_name=ctx["customer_name"] or "there",
        customer_email=ctx["customer_email"] or identity.email or "",
        account_state=(ctx["customer_dict"].get("status", "active")
                       if ctx["is_linked"] else "browsing"),
        current_plan=ctx["plan_id"],
        balance_summary=build_balance_summary(primary_sub),
        operator_name=portal_settings.bss_operator_name,
        operator_support_email=portal_settings.bss_operator_support_email,
        prior_messages=prior_pairs,
        is_linked=ctx["is_linked"],
    )

    # Transcript for case.open_for_me — full running text, including
    # the latest user message.
    transcript = conv.transcript_text()

    async def stream() -> AsyncIterator[bytes]:
        if turn.done:
            yield _sse_frame("status", _status_html("done"))
            return

        yield _sse_frame("status", _status_html("live"))

        # v0.13.1 — track tool calls on this turn so we can detect
        # escalation hallucinations. If the assistant claims to have
        # escalated but never called case.open_for_me, replace the
        # text with a safe fallback (no fake-escalation reply).
        called_tools_this_turn: list[str] = []

        try:
            async for event in astream_once(
                turn.question,
                allow_destructive=True,
                channel="portal-chat",
                # auth_context.actor = customer_id when linked, None
                # for anonymous identities. The .mine wrappers refuse
                # cleanly when None; catalog reads still work.
                actor=chat_actor or "",
                service_identity="portal_self_serve",
                tool_filter="customer_self_serve",
                system_prompt=system_prompt,
                transcript=transcript,
            ):
                # Token-usage: record cost; don't render.
                if isinstance(event, AgentEventTurnUsage):
                    try:
                        await record_chat_turn(
                            customer_id=cap_key,
                            prompt_tok=event.prompt_tok,
                            completion_tok=event.completion_tok,
                            model=event.model or None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "chat.cost_record_failed",
                            cap_key=cap_key,
                            error=str(exc),
                        )
                    continue

                # Trip-wire — generic safety reply, never leaked detail.
                if isinstance(event, AgentEventError) and (
                    "AgentOwnershipViolation" in event.message
                ):
                    turn.ownership_violation = True
                    turn.error = "ownership_violation"
                    turn.final_text = _OWNERSHIP_VIOLATION_REPLY
                    conv.append("assistant", _OWNERSHIP_VIOLATION_REPLY)
                    yield _sse_frame(
                        "message",
                        _chat_assistant_html(_OWNERSHIP_VIOLATION_REPLY, error=True),
                    )
                    yield _sse_frame("status", _status_html("error"))
                    return

                if isinstance(event, AgentEventError):
                    turn.error = event.message
                    fallback = "Sorry — something went wrong. Please try again."
                    conv.append("assistant", fallback)
                    yield _sse_frame(
                        "message", _chat_assistant_html(fallback, error=True)
                    )
                    yield _sse_frame("status", _status_html("error"))
                    return

                # Tool calls render as small inline pills so the
                # customer can see what the agent is doing.
                if isinstance(event, AgentEventToolCallStarted):
                    called_tools_this_turn.append(event.name)
                    yield _sse_frame(
                        "message", _chat_tool_pill_html(event.name)
                    )
                    continue

                if isinstance(event, AgentEventFinalMessage):
                    text = _strip_reasoning_leakage(event.text or "")
                    is_error = False
                    # Anti-hallucination: if the reply claims to have
                    # escalated but case.open_for_me wasn't actually
                    # called this turn, replace with a safe fallback.
                    # Doctrine-coupled: the v0.12 escalation contract
                    # is "the case row IS the escalation"; a model
                    # hallucinating the sentence without the side
                    # effect is a doctrine violation we fix at the
                    # edge, not by retrying the LLM.
                    if (
                        _claims_escalation(text)
                        and "case.open_for_me" not in called_tools_this_turn
                    ):
                        log.warning(
                            "chat.escalation_hallucination",
                            cap_key=cap_key,
                            called_tools=called_tools_this_turn,
                        )
                        text = _ESCALATION_HALLUCINATION_FALLBACK.format(
                            email=portal_settings.bss_operator_support_email
                        )
                        is_error = True
                    turn.done = True
                    turn.final_text = text
                    conv.append("assistant", text)
                    yield _sse_frame(
                        "message", _chat_assistant_html(text, error=is_error)
                    )
                    yield _sse_frame("status", _status_html("done"))
                    return

                # Everything else (intermediate AIMessages without
                # tool_calls, ToolMessage results) is hidden from the
                # chat log — the audit row + bss trace carry the
                # forensic record.
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("chat.stream_crashed")
            turn.error = f"{type(exc).__name__}: {exc}"
            yield _sse_frame("status", _status_html("error"))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# Chat-bubble HTML renderers were lifted to packages/bss-portal-ui/
# (bss_portal_ui.chat_html) in v0.13 PR5 so the operator cockpit's
# chat thread renders identically to this customer-chat surface.
# Imports above alias them under their original underscore-prefixed
# names so this module's body reads unchanged.
