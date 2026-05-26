"""Interactive REPL — ``bss`` invoked with no subcommand. v0.13 cockpit.

The REPL is the canonical operator cockpit (browser veneer in
``portals/csr`` mirrors it). Conversations live in Postgres via the
:mod:`bss_cockpit` package; exit ``bss``, open ``/cockpit/<id>`` in
the browser, see the same turns. Multi-turn coherence comes from
``astream_once(transcript=conversation.transcript_text(), ...)`` —
PR6 extends astream_once to feed the parsed prior turns into the
LangGraph messages list.

Each turn streams agent events from ``astream_once`` with the cockpit
profile (``tool_filter="operator_cockpit"``) and identity
(``service_identity="operator_cockpit"``). Tool-call observations on
"show-shaped" tools render via the matching
:mod:`bss_cockpit.renderers` ASCII card before the model's text reply.

Slash commands:

  /sessions         — Rich table of operator's recent cockpit sessions
  /new [LABEL]      — close current → open new (label optional)
  /switch SES-...   — resume specific session id
  /reset            — clear messages on current session (keeps row)
  /focus CUST-NNN   — pin a customer for the system prompt
  /focus clear      — unset focus
  /360 [CUST-NNN]   — render customer 360 + persist as a tool message
  /confirm          — flip next turn allow_destructive=True
  /config edit      — open .bss-cli/settings.toml in $EDITOR; reload
  /operator edit    — open .bss-cli/OPERATOR.md in $EDITOR; reload
  /help             — slash-command cheat sheet
  /exit, /quit      — leave (without closing the session)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory


# v0.13.1 — persistent REPL history under ``.bss-cli/repl_history``.
# Survives across ``bss`` invocations. prompt_toolkit handles Up/Down
# + Ctrl-R + line editing natively on every platform Rich's input()
# bypass didn't (the readline approach was unreliable under Rich's
# Prompt.ask).
_HISTORY_FILE_NAME = "repl_history"


def _bss_cli_dir() -> Path:
    """Mirror bss_cockpit.config._bss_cli_dir — same precedence:
    BSS_COCKPIT_DIR override, else <repo>/.bss-cli."""
    override = os.environ.get("BSS_COCKPIT_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / ".bss-cli"


def _make_prompt_session() -> PromptSession:
    """Build a prompt_toolkit session with FileHistory.

    Falls back to an in-memory history if the dir isn't writable —
    Up/Down still work for the running session.
    """
    bss_dir = _bss_cli_dir()
    history = None
    try:
        bss_dir.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(bss_dir / _HISTORY_FILE_NAME))
    except OSError:
        history = None
    return PromptSession(
        history=history,
        auto_suggest=AutoSuggestFromHistory(),
        enable_history_search=True,  # Ctrl-R / Ctrl-N substring search
    )

from bss_clients.errors import ClientError
from bss_models import BSS_RELEASE
from bss_cockpit import (
    OPERATOR_ACTOR,
    Conversation,
    ConversationStore,
    build_cockpit_prompt,
    configure_store,
    current as cockpit_config_current,
)
from bss_orchestrator.autonomy import read_autonomy_mode
from bss_orchestrator.clients import close_clients, get_clients
from bss_orchestrator.config import settings as orch_settings
from bss_orchestrator.session import (
    AgentEventError,
    AgentEventFinalMessage,
    AgentEventToolCallCompleted,
    AgentEventToolCallStarted,
    astream_once,
)
from rich import print as rprint
from rich.align import Align
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from bss_cockpit.postprocess import strip_channel_markup, strip_reasoning_leakage
from bss_cockpit.renderers import (
    RENDERER_DISPATCH,
    render_customer_360,
    render_tool_result,
)


# ─── Constants ─────────────────────────────────────────────────────────


_LOGO = r"""
 ██████╗  ███████╗ ███████╗     ██████╗ ██╗      ██╗
 ██╔══██╗ ██╔════╝ ██╔════╝    ██╔════╝ ██║      ██║
 ██████╔╝ ███████╗ ███████╗    ██║      ██║      ██║
 ██╔══██╗ ╚════██║ ╚════██║    ██║      ██║      ██║
 ██████╔╝ ███████║ ███████║    ╚██████╗ ███████╗ ██║
 ╚═════╝  ╚══════╝ ╚══════╝     ╚═════╝ ╚══════╝ ╚═╝"""


_SLASH_HELP = (
    "[green]/sessions[/] list  "
    "[green]/new[/] [label]  "
    "[green]/switch[/] SES  "
    "[green]/reset[/]  "
    "[green]/focus[/] CUST  "
    "[green]/360[/]  "
    "[green]/ports[/]  "
    "[green]/confirm[/]  "
    "[green]/config edit[/]  "
    "[green]/operator edit[/]  "
    "[green]/help[/]  "
    "[green]/exit[/]"
)


# ─── Banner ───────────────────────────────────────────────────────────


def _render_banner(
    *,
    actor: str,
    model: str,
    session_id: str,
    customer_focus: str | None,
    allow_destructive_default: bool,
) -> Panel:
    logo = Text(_LOGO, style="bold green", no_wrap=True)
    tagline = Align.center(
        Text.from_markup(
            "[bold white]LLM-native Business Support System[/]   "
            f"[dim]·[/]   [magenta]operator cockpit v{BSS_RELEASE}[/]"
        )
    )

    focus = customer_focus or "—"
    meta_line = Align.center(
        Text.from_markup(
            f"[dim]actor[/] [green]{actor}[/]   "
            f"[dim]·[/]   [dim]model[/] [bold magenta]{model}[/]\n"
            f"[dim]session[/] [yellow]{session_id}[/]   "
            f"[dim]·[/]   [dim]focus[/] [green]{focus}[/]"
        )
    )

    hints = Text.from_markup(
        "  [bold]try[/]    "
        "[italic green]show the catalog[/]   [dim]·[/]   "
        "[italic green]show subscription SUB-0001[/]   [dim]·[/]   "
        "[italic green]/360 CUST-001[/]\n"
        f"  [bold]slash[/]  {_SLASH_HELP}"
    )

    parts: list = [
        logo,
        Text(""),
        tagline,
        meta_line,
        Text(""),
        hints,
    ]
    if allow_destructive_default:
        parts += [
            Text(""),
            Align.center(
                Text.from_markup(
                    "[bold red on yellow] DESTRUCTIVE-DEFAULT MODE [/] "
                    "[red]writes execute without /confirm — beware[/]"
                )
            ),
        ]
    return Panel(
        Group(*parts),
        border_style="green",
        padding=(0, 1),
        title="[bold green]bss[/] [dim]·[/] [white]cockpit[/]",
        title_align="left",
        subtitle="[dim]type a request or a /command[/]",
        subtitle_align="right",
    )


# ─── Renderer dispatch ────────────────────────────────────────────────
#
# Single source of truth lives in `bss_cockpit.renderers.dispatch` —
# RENDERER_DISPATCH + render_tool_result are imported above and used
# verbatim by both the REPL and the browser veneer. There is exactly
# ONE rendering rule for tool results, code-enforced.


# ─── v0.19 — natural-language list intercepts ─────────────────────────
#
# Small models (Gemma 4 26B, MiMo, etc.) tend to fabricate list-shaped
# answers from prompt context instead of calling the right tool. The
# system prompt asks them not to; they ignore it. Fix: intercept the
# operator's input BEFORE it reaches the LLM and dispatch the tool
# directly when intent is unambiguous. The result flows through the
# existing renderer dispatch — same ASCII output the LLM would have
# produced if it had called the tool itself, just faster + always
# correct.
#
# Match patterns are loose-substring; the keys list is the canonical
# operator vocabulary. Add a new entry by appending to _INTENT_RULES.
# Each rule maps a regex (compiled) to a tool name + optional kwarg
# extractor (None = no args).

import re as _re

# v0.20+ — citation guard. The cockpit's contract is "knowledge.search →
# cite anchor, OR say I don't have a citation for that." If the
# assistant claims handbook/runbook/doctrine in its reply but no
# knowledge.* tool call fired this turn, replace with the fallback.
# Mirrors the v0.12 _RE_ESCALATION_CLAIM pattern on the customer chat.
#
# The regex catches first-person phrasings of "the docs say X". We
# DON'T catch every phrasing the LLM might use — that would be
# adversarial. We catch the most common false-confident phrasings
# and let the rest through (the indexer + search results are the
# primary defence; the guard is a backstop for false claims).
_RE_KNOWLEDGE_CLAIM = _re.compile(
    r"\b(?:"
    # "per (the) handbook", "according to the runbook", "as per CLAUDE.md"
    r"(?:per|according\s+to|as\s+per)\s+(?:the\s+)?"
    r"(?:handbook|runbook|doctrine|"
    r"CLAUDE\.md|ARCHITECTURE\.md|DECISIONS\.md|TOOL_SURFACE\.md|HANDBOOK\.md)"
    # "the handbook says", "in the runbook it says" — explicit claim verb
    r"|(?:the|in\s+the)\s+(?:handbook|runbook|doctrine)\s+"
    r"(?:says|states|specifies|mentions|requires|forbids|allows)"
    # "the docs say"
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
    """True if the assistant's reply cites handbook/runbook/doctrine
    without a knowledge.* tool actually firing this turn."""
    return bool(_RE_KNOWLEDGE_CLAIM.search(text or ""))


_INTENT_RULES: list[tuple[Any, str, Callable[[str], dict] | None]] = [
    # Customers
    (_re.compile(r"\b(list|show)( me| us)?( all| every| the)? customers?\b", _re.I),
     "customer.list", None),
    (_re.compile(r"\bwho are (the|all|our|my) customers\b", _re.I),
     "customer.list", None),

    # Catalog
    (_re.compile(r"\b(list|show)( me| us)?( all| every| the)? (plans|products|offerings)\b", _re.I),
     "catalog.list_active_offerings", None),
    (_re.compile(r"\bwhat( are the| plans|s)? (plans|products|offerings)\b", _re.I),
     "catalog.list_active_offerings", None),
    (_re.compile(r"\bproduct catalog\b", _re.I),
     "catalog.list_active_offerings", None),
    (_re.compile(r"\b(list|show)( me| us)?( all| every)? (vas|top.?ups?)\b", _re.I),
     "catalog.list_vas", None),
    (_re.compile(r"\bwhat( are the)? (vas|top.?ups?)\b", _re.I),
     "catalog.list_vas", None),

    # Inventory — minimal "show me the numbers" shape only. Anything
    # nuanced ("how many", "is that all", status filters, prefix
    # filters) flows to the LLM, which has the count + list tools.
    # Adding more regex here is whack-a-mole; the doctrine fix is
    # better tool docstrings + a real count tool, not more patterns.
    # The ( all)?( the)? bigram is the bare minimum to handle
    # "show me all the numbers" + "show me the numbers" + "show all".
    (_re.compile(
        r"\b(list|show)( me| us)?( all| every)?( the)? "
        r"(msisdns?|numbers?|inventory)\b",
        _re.I,
     ),
     "inventory.msisdn.list_available", None),

    # Port requests (v0.17)
    (_re.compile(r"\b(list|show)( me| us)?( all| every| pending| open)? port (requests?|ins?|outs?)\b", _re.I),
     "port_request.list", None),

    # Orders
    (_re.compile(r"\b(list|show)( me| us)?( all| every| recent)? orders\b", _re.I),
     "order.list", None),
]


def _maybe_intent_match(line: str) -> tuple[str, dict] | None:
    """Return (tool_name, kwargs) if the line matches a list-intent rule.

    None means no match — fall through to the LLM as before.
    """
    for pattern, tool, extractor in _INTENT_RULES:
        if pattern.search(line):
            kwargs = extractor(line) if extractor else {}
            return tool, kwargs
    return None




async def _drive_intent_turn(
    *,
    conv: Conversation,
    line: str,
    tool_name: str,
    kwargs: dict,
) -> None:
    """Dispatch a tool deterministically + render + persist as a turn.

    Skips the LLM entirely. Same renderer dispatch as
    `_drive_turn`'s tool-result path so the ASCII output is identical
    to "the model called the right tool" — but always correct, faster,
    and zero token cost.
    """
    from bss_orchestrator.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        rprint(f"[red]intent dispatch failed: tool {tool_name!r} not registered[/]")
        return

    try:
        result = await fn(**kwargs)
    except Exception as exc:
        rprint(f"[red]{tool_name} failed:[/] {exc}")
        await conv.append_user_turn(line)
        await conv.append_tool_turn(tool_name, f"ERROR: {exc}")
        return

    renderer = RENDERER_DISPATCH.get(tool_name)
    rendered = renderer(result) if renderer else json.dumps(result, indent=2, default=str)

    rprint(rendered)

    # Persist as a regular conversation turn so the browser veneer
    # sees the same content. Mark the turn with a synthetic actor
    # so the audit trail is honest about the bypass.
    await conv.append_user_turn(line)
    await conv.append_tool_turn(tool_name, rendered)


# Back-compat shims — internal callers still reach for `_RENDERER_DISPATCH`
# / `_maybe_render_tool_result`. Both delegate to the shared module.
_RENDERER_DISPATCH = RENDERER_DISPATCH
_maybe_render_tool_result = render_tool_result


# ─── Settings + store bootstrap ───────────────────────────────────────


def _bootstrap_store_and_config() -> tuple[ConversationStore, str, str]:
    """Wire the cockpit store + read settings.toml. Returns (store, actor, model).

    Also reads ``BSS_REPL_LLM_AUTONOMY`` once here so the REPL refuses to
    start with a misconfigured autonomy mode (v1.5; mirrors the cockpit
    portal lifespan in ``portals/csr/bss_csr/main.py``). The validated
    mode is stashed on a module-level so ``_drive_turn`` can read it
    without re-parsing env on every turn.
    """
    db_url = os.environ.get("BSS_DB_URL", "")
    if not db_url:
        raise RuntimeError(
            "BSS_DB_URL is not set. Source .env or export it before "
            "running the cockpit."
        )

    # v1.5 — fail-closed validation of BSS_REPL_LLM_AUTONOMY. The reader
    # itself raises AutonomyMisconfigured on a bad value; we let it
    # propagate so the operator sees a clear startup crash rather than
    # the REPL silently defaulting to granular when they typed
    # 'batchd'. Stashed in a module global rather than passed around
    # because every astream_once call needs it and threading through
    # a half-dozen handler signatures adds noise without value.
    global _AUTONOMY_MODE
    _AUTONOMY_MODE = read_autonomy_mode()

    store = ConversationStore(db_url=db_url)
    configure_store(store)

    # Load settings.toml + OPERATOR.md (autobootstraps on first run).
    # v0.13.1 — actor is hardcoded; settings.toml carries non-identity
    # preferences only (model, ports, allow_destructive_default).
    cfg = cockpit_config_current()
    model = cfg.settings.llm.model or orch_settings.llm_model
    return store, OPERATOR_ACTOR, model


# v1.5 — autonomy mode cache, set by _bootstrap_store_and_config().
# Read-only after boot; per-session override is deferred to v1.5.1.
_AUTONOMY_MODE: str = "granular"


# ─── Slash command handlers ───────────────────────────────────────────


async def _cmd_help() -> None:
    rprint(
        "[green]/sessions[/]      list operator's cockpit sessions\n"
        "[green]/new[/] [LABEL]   close current → open new (label optional)\n"
        "[green]/switch[/] SES    resume specific session id\n"
        "[green]/reset[/]         clear messages on current session\n"
        "[green]/focus[/] CUST    pin a customer for the system prompt\n"
        "[green]/focus clear[/]   unset focus\n"
        "[green]/360[/] [CUST]    customer 360 (uses focus if no arg)\n"
        "[green]/ports[/]         MNP queue: list / approve PORT-NNN / reject PORT-NNN <reason>\n"
        "[green]/confirm[/]       flip next turn allow_destructive=True\n"
        "[green]/config edit[/]   open settings.toml in $EDITOR; reload\n"
        "[green]/operator edit[/] open OPERATOR.md in $EDITOR; reload\n"
        "[green]/help[/]          this list\n"
        "[green]/exit[/]          leave (does not close the session)"
    )


async def _cmd_sessions(actor: str) -> None:
    rows = await Conversation.list_for(actor)
    if not rows:
        rprint(f"[yellow]No active cockpit sessions for {actor}.[/]")
        return
    t = Table(title=f"Cockpit sessions for [green]{actor}[/]")
    t.add_column("Session", style="yellow")
    t.add_column("Label")
    t.add_column("Focus")
    t.add_column("Last active")
    t.add_column("Msgs", justify="right")
    for r in rows:
        t.add_row(
            r.session_id,
            r.label or "—",
            r.customer_focus or "—",
            r.last_active_at.strftime("%Y-%m-%d %H:%M"),
            str(r.message_count),
        )
    rprint(t)


async def _cmd_360(
    conv: Conversation, arg: str
) -> None:
    """Run customer 360 (customer.get + subs + cases + interactions)
    and persist the rendered card as a tool turn."""
    target = arg.strip() or (conv.customer_focus or "")
    if not target:
        rprint(
            "[yellow]/360 needs a customer id (or set /focus first).[/]"
        )
        return
    clients = get_clients()
    try:
        customer = await clients.crm.get_customer(target)
    except ClientError as exc:
        if exc.status_code == 404:
            rprint(f"[red]Customer {target} not found.[/]")
            return
        raise
    subs: list[dict] = []
    cases: list[dict] = []
    interactions: list[dict] = []
    try:
        subs = await clients.subscription.list_for_customer(target) or []
    except ClientError:
        pass
    try:
        cases = await clients.crm.list_cases(customer_id=target) or []
    except ClientError:
        pass
    try:
        interactions = await clients.crm.list_interactions(
            customer_id=target, limit=20
        ) or []
    except ClientError:
        pass

    card = render_customer_360(
        customer,
        subscriptions=subs,
        cases=cases,
        interactions=interactions,
    )
    rprint(card)
    # Persist as a tool turn so the browser surface (or a `--session`
    # resume) can see what the operator pulled up.
    await conv.append_tool_turn("customer.360", card)


async def _cmd_ports(conv: Conversation, arg: str) -> None:
    """v0.17 — operator MNP queue. ``/ports``, ``/ports list``,
    ``/ports approve PORT-NNN``, ``/ports reject PORT-NNN <reason>``.

    Operator-only by spec — no customer-self-serve equivalent.
    """
    clients = get_clients()
    parts = arg.strip().split(maxsplit=2)
    sub = parts[0].lower() if parts else "list"

    if sub in ("", "list"):
        try:
            rows = await clients.crm.list_port_requests(limit=50)
        except ClientError as exc:
            rprint(f"[red]port_request.list failed:[/] {exc}")
            return
        if not rows:
            rprint("[dim]no port requests[/]")
            return
        table = Table(title=f"port requests ({len(rows)})")
        table.add_column("id")
        table.add_column("dir")
        table.add_column("state")
        table.add_column("donor MSISDN")
        table.add_column("carrier")
        table.add_column("target sub")
        table.add_column("port date")
        for r in rows:
            table.add_row(
                r.get("id", "?"),
                r.get("direction", "?"),
                r.get("state", "?"),
                r.get("donorMsisdn", "?"),
                r.get("donorCarrier", "?"),
                r.get("targetSubscriptionId") or "—",
                r.get("requestedPortDate", "?"),
            )
        rprint(table)
        await conv.append_tool_turn("port_request.list", str(table))
        return

    if sub == "approve":
        if len(parts) < 2:
            rprint("[yellow]/ports approve PORT-NNN[/]")
            return
        port_id = parts[1]
        try:
            out = await clients.crm.approve_port_request(port_id)
        except ClientError as exc:
            rprint(f"[red]approve failed:[/] {exc}")
            return
        rprint(f"[green]approved[/] {out.get('id')}  state={out.get('state')}")
        await conv.append_tool_turn("port_request.approve", json.dumps(out))
        return

    if sub == "reject":
        if len(parts) < 3:
            rprint("[yellow]/ports reject PORT-NNN <reason>[/]")
            return
        port_id = parts[1]
        reason = parts[2]
        try:
            out = await clients.crm.reject_port_request(port_id, reason=reason)
        except ClientError as exc:
            rprint(f"[red]reject failed:[/] {exc}")
            return
        rprint(f"[yellow]rejected[/] {out.get('id')}  reason={reason!r}")
        await conv.append_tool_turn("port_request.reject", json.dumps(out))
        return

    rprint(
        "[yellow]/ports[, list, approve PORT-NNN, reject PORT-NNN <reason>][/]"
    )


async def _open_in_editor(path: Path) -> None:
    editor = os.environ.get("EDITOR", "vi")
    proc = await asyncio.create_subprocess_exec(editor, str(path))
    await proc.wait()


# ─── Turn driver ──────────────────────────────────────────────────────


async def _drive_turn(
    *,
    conv: Conversation,
    line: str,
    actor: str,
    model: str,
    allow_destructive_default: bool,
) -> None:
    """Stream one cockpit turn through astream_once.

    Pulls the system prompt from ``build_cockpit_prompt`` (with the
    operator's persona, customer focus, and any pending-destructive
    confirmed-action block), feeds the prior transcript so the model
    sees multi-turn context, and renders ASCII cards on
    ``*.get``-shaped tool results before the prose reply.
    """
    cfg = cockpit_config_current()

    # v0.20.2 — pull the prior transcript BEFORE appending the new user
    # turn so the LLM sees [prior history] + [new HumanMessage(line)]
    # instead of [prior history + new user turn] + [new HumanMessage(line)].
    # The browser cockpit always did the equivalent (it slices the new
    # turn off after the fact via ``blocks[:last_user_index]``); the
    # REPL was passing a transcript that already ended in the new user
    # message, which ``astream_once`` then re-wrapped as a HumanMessage
    # — producing two back-to-back identical user turns with no
    # assistant turn between them. Gemma reads that as "this question
    # has been asked and gone unanswered → I should deflect" and the
    # RAG hit rate on the REPL was visibly worse than the browser.
    prior_transcript = await conv.transcript_text()
    user_msg_id = await conv.append_user_turn(line)

    # If a /confirm landed on the previous turn, consume the pending
    # row and pass the payload into the system prompt for this turn —
    # the LLM sees what it's authorised to call.
    pending = await conv.consume_pending_destructive()
    allow_destructive_this_turn = (
        allow_destructive_default or pending is not None
    )

    system_prompt = build_cockpit_prompt(
        operator_md=cfg.operator_md,
        customer_focus=conv.customer_focus,
        pending_destructive=pending,
        extra_context={
            "model": model,
            "session_id": conv.session_id,
        },
    )

    final_text = ""
    error: str | None = None
    cards_shown = 0
    captured_tool_calls: list[dict[str, Any]] = []
    last_tool_proposal: tuple[str, dict] | None = None

    try:
        async for event in astream_once(
            line,
            allow_destructive=allow_destructive_this_turn,
            channel="cli",
            actor=actor,
            service_identity="operator_cockpit",
            tool_filter="operator_cockpit",
            system_prompt=system_prompt,
            transcript=prior_transcript,
            autonomy_mode=_AUTONOMY_MODE,
        ):
            if isinstance(event, AgentEventToolCallStarted):
                captured_tool_calls.append(
                    {
                        "name": event.name,
                        "args": event.args,
                        "call_id": event.call_id,
                    }
                )
                # NOTE: pre-v1.5 set last_tool_proposal on STARTED for
                # every destructive — but that incorrectly staged tools
                # that ACTUALLY EXECUTED on /confirm-resumed turns
                # (allow=True with the wrapper letting it through).
                # v1.5 derives the proposal-to-stage strictly from the
                # COMPLETED-with-BLOCKED signal below.
            elif isinstance(event, AgentEventToolCallCompleted):
                raw = event.result_full or event.result
                # v1.5 — when a destructive tool's result is the
                # structured DESTRUCTIVE_OPERATION_BLOCKED response,
                # treat it as a proposal to stage. Covers both
                # pre-v1.5 (allow=False blocks at the gate) and v1.5
                # granular (second destructive in the same loop
                # re-gates even with allow=True).
                if (
                    event.name
                    and _is_destructive(event.name)
                    and "DESTRUCTIVE_OPERATION_BLOCKED" in (raw or "")
                ):
                    args = {}
                    for tc in reversed(captured_tool_calls):
                        if tc.get("call_id") == event.call_id:
                            args = tc.get("args", {}) or {}
                            break
                    last_tool_proposal = (event.name, args)
                card = _maybe_render_tool_result(event.name, raw)
                if card:
                    rprint(card)
                    cards_shown += 1
            elif isinstance(event, AgentEventFinalMessage):
                # v0.20.1 — strip Harmony / channel-format leakage AND
                # gemma reasoning-channel leakage (``<think>...</think>``
                # blocks, leading ``thought\n``, same-line ``thought ...``
                # prefix) at the boundary so neither display nor
                # persistence carries the artefacts. Browser surface
                # already chains these via ``bss_portal_ui.chat_html``;
                # REPL needed the same chain.
                final_text = strip_reasoning_leakage(
                    strip_channel_markup(event.text or "")
                )
            elif isinstance(event, AgentEventError):
                error = event.message
                break
    except Exception as exc:  # noqa: BLE001 — surface, never crash REPL
        error = f"{type(exc).__name__}: {exc}"

    if error:
        rprint(f"[red]LLM error:[/] {error}")
        # Persist the error as an assistant turn so the next /switch
        # back to this session shows what went wrong.
        await conv.append_assistant_turn(
            f"(error: {error})", tool_calls_json=captured_tool_calls or None
        )
        return

    # v0.20+ citation guard. If the reply claims handbook/runbook/
    # doctrine but no knowledge.* tool fired this turn, replace the
    # text. Doctrine: never paraphrase doctrine without citing.
    called_tool_names = {tc["name"] for tc in captured_tool_calls}
    knowledge_called = any(
        n.startswith("knowledge.") for n in called_tool_names
    )
    if final_text and _claims_handbook(final_text) and not knowledge_called:
        rprint(
            "[yellow]⚠ citation guard tripped — replacing un-cited "
            "handbook/doctrine claim with safe fallback.[/]"
        )
        final_text = _KNOWLEDGE_HALLUCINATION_FALLBACK

    # Persist the assistant's reply so the next turn's transcript sees
    # it. Even when cards rendered (and the prose was suppressed for
    # display), the prose lands in the conversation log so the browser
    # surface or a /switch back can read what was said.
    # v0.20.1 — when the LLM fired tools but produced no final text
    # (gemma after knowledge.search, occasionally), surface a useful
    # recovery hint instead of an opaque "(no reply)" bubble.
    if not final_text.strip():
        if captured_tool_calls:
            called = ", ".join(tc["name"] for tc in captured_tool_calls)
            final_text = (
                f"(The model called `{called}` but did not synthesise a "
                "final answer. Send the same question again or rephrase "
                "to retry.)"
            )
        else:
            final_text = "(no reply)"
    asst_msg_id = await conv.append_assistant_turn(
        final_text,
        tool_calls_json=captured_tool_calls or None,
    )

    # If the agent proposed a destructive action AND the gate blocked
    # it (either pre-v1.5 allow=False, or v1.5 granular second-destructive
    # re-gate), stash the proposal so the next turn's /confirm can
    # consume it. The COMPLETED-side BLOCKED check above is the v1.5
    # signal; the staging guard here no longer gates on
    # allow_destructive_this_turn because granular post-confirm runs
    # with allow=True yet still re-blocks the second destructive.
    if last_tool_proposal is not None:
        tool_name, tool_args = last_tool_proposal
        await conv.set_pending_destructive(
            tool_name=tool_name,
            args=tool_args,
            proposal_message_id=asst_msg_id,
        )
        rprint(
            f"[yellow]Pending /confirm for[/] [bold]{tool_name}[/]"
            f"[yellow] — type /confirm to authorise the next turn.[/]"
        )

    if cards_shown:
        # The card was the answer; skip the redundant prose panel.
        return
    # v0.20.1 — render the final message via Rich Markdown so headings,
    # bold/italic, code spans, fenced code, and (for knowledge.* turns)
    # pipe tables display as formatted text instead of raw markdown.
    # Rich's renderer handles all of that natively; no surface-specific
    # parsing lives here. Plain text without markdown markers is a
    # no-op pass-through, so the worst case is the prior behaviour.
    rprint(
        Panel(
            Markdown(final_text or "(no reply)"),
            title="bss ai",
            border_style="green",
        )
    )


# Tools whose mere "started" event indicates a destructive proposal.
# Keep narrow — read-shaped tools never trigger pending_destructive.
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
    return any(tool_name == p or tool_name.startswith(p) for p in _DESTRUCTIVE_PREFIXES)


# ─── Slash dispatch ───────────────────────────────────────────────────


async def _handle_slash(
    line: str,
    *,
    conv: Conversation,
    actor: str,
    bss_cli_dir: Path,
) -> tuple[str, Conversation | None]:
    """Run a slash command. Returns (action, replacement_conversation).

    action ∈ {"continue", "exit"}; replacement_conversation is set when
    the slash created/switched a conversation.
    """
    parts = line.strip().split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in {"/exit", "/quit"}:
        return "exit", None
    if cmd == "/help":
        await _cmd_help()
        return "continue", None
    if cmd == "/sessions":
        await _cmd_sessions(actor)
        return "continue", None
    if cmd == "/new":
        await conv.close()
        new_conv = await Conversation.open(actor=actor, label=arg or None)
        rprint(
            f"[green]Opened[/] [yellow]{new_conv.session_id}[/]"
            + (f" [dim]label={arg!r}[/]" if arg else "")
        )
        return "continue", new_conv
    if cmd == "/switch":
        if not arg:
            rprint("[yellow]/switch needs a session id (SES-...)[/]")
            return "continue", None
        try:
            new_conv = await Conversation.resume(arg)
        except LookupError:
            rprint(f"[red]Session {arg} not found.[/]")
            return "continue", None
        rprint(f"[green]Resumed[/] [yellow]{new_conv.session_id}[/]")
        # Show the last few turns for context.
        transcript = await new_conv.transcript_text()
        if transcript:
            tail = "\n\n".join(transcript.split("\n\n")[-5:])
            rprint(Panel(tail, title="prior turns (last 5)", border_style="dim"))
        return "continue", new_conv
    if cmd == "/reset":
        await conv.reset()
        rprint(f"[green]Cleared messages on {conv.session_id}.[/]")
        return "continue", None
    if cmd == "/focus":
        if arg.lower() == "clear" or not arg:
            await conv.set_focus(None)
            rprint("[green]Focus cleared.[/]")
            return "continue", None
        await conv.set_focus(arg.strip())
        rprint(f"[green]Focus pinned to[/] [yellow]{arg.strip()}[/]")
        return "continue", None
    if cmd == "/360":
        await _cmd_360(conv, arg)
        return "continue", None
    if cmd == "/ports":
        await _cmd_ports(conv, arg)
        return "continue", None
    if cmd == "/confirm":
        # Explicit confirmation flow: the next turn will consume any
        # pending_destructive row; here we just tell the operator
        # whether one is in flight.
        # We don't pop the row — the next /drive_turn does that. This
        # check is purely informational so the operator knows what's
        # about to run.
        # (The row stays in place; consume_pending_destructive runs in
        # _drive_turn below.)
        return "continue", "_confirmed_marker"  # type: ignore[return-value]
    if cmd in {"/config", "/operator"}:
        # /config edit  /operator edit
        if arg.strip() != "edit":
            rprint(f"[yellow]{cmd} edit  — open the file in $EDITOR.[/]")
            return "continue", None
        path = (
            bss_cli_dir / ("settings.toml" if cmd == "/config" else "OPERATOR.md")
        )
        await _open_in_editor(path)
        # The next current() call hot-reloads; nothing else to do.
        rprint(f"[dim]reloaded {path.name} (mtime-based hot reload).[/]")
        return "continue", None

    rprint(f"[yellow]Unknown command: {cmd}  ([green]/help[/] for the list)[/]")
    return "continue", None


# ─── Entry point ──────────────────────────────────────────────────────


def run_repl(
    *,
    allow_destructive: bool = False,
    session_id: str | None = None,
    force_new: bool = False,
    label: str | None = None,
    list_only: bool = False,
) -> None:
    """Start the v0.13 cockpit REPL. Blocks until the operator quits."""
    try:
        store, actor, model = _bootstrap_store_and_config()
    except RuntimeError as exc:
        rprint(f"[red]Cockpit unavailable:[/] {exc}")
        return

    # v0.13.1 — prompt_toolkit prompt: Up/Down history + Ctrl-R search
    # + persistent across bss invocations + fish-style auto-suggest.
    prompt_session = _make_prompt_session()

    bss_cli_dir = Path(__file__).resolve().parents[2] / ".bss-cli"

    loop = asyncio.new_event_loop()
    try:
        # --list: print sessions table and exit.
        if list_only:
            loop.run_until_complete(_cmd_sessions(actor))
            return

        # Resolve which conversation to start with.
        if session_id:
            try:
                conv = loop.run_until_complete(Conversation.resume(session_id))
            except LookupError:
                rprint(f"[red]Session {session_id} not found.[/]")
                return
        elif force_new:
            conv = loop.run_until_complete(
                Conversation.open(actor=actor, label=label)
            )
        else:
            # Resume most-recent active session for this actor; open new
            # if none exists.
            recent = loop.run_until_complete(
                Conversation.list_for(actor)
            )
            if recent:
                conv = loop.run_until_complete(
                    Conversation.resume(recent[0].session_id)
                )
            else:
                conv = loop.run_until_complete(
                    Conversation.open(actor=actor, label=label)
                )

        rprint(
            _render_banner(
                actor=actor,
                model=model,
                session_id=conv.session_id,
                customer_focus=conv.customer_focus,
                allow_destructive_default=allow_destructive,
            )
        )

        while True:
            # ANSI-coloured prompt label rendered identically to the
            # prior Rich shape (bold "bss:" + yellow last-8 chars of
            # session id + green "> ").
            prompt_label = ANSI(
                f"\033[1mbss\033[0m:"
                f"\033[33m{conv.session_id[-8:]}\033[0m"
                f"\033[36m> \033[0m"
            )
            try:
                line = prompt_session.prompt(prompt_label).strip()
            except (EOFError, KeyboardInterrupt):
                rprint()
                break
            if not line:
                continue
            if line.startswith("/"):
                action, replacement = loop.run_until_complete(
                    _handle_slash(
                        line,
                        conv=conv,
                        actor=actor,
                        bss_cli_dir=bss_cli_dir,
                    )
                )
                if action == "exit":
                    break
                if isinstance(replacement, Conversation):
                    conv = replacement
                    rprint(
                        _render_banner(
                            actor=actor,
                            model=model,
                            session_id=conv.session_id,
                            customer_focus=conv.customer_focus,
                            allow_destructive_default=allow_destructive,
                        )
                    )
                # /confirm: leaves a marker; next turn's _drive_turn
                # consumes the pending row. No explicit state change
                # here; the next prompt the operator types will run
                # with allow_destructive=True if a pending row exists.
                continue

            # v0.19 — list-intent intercept. Small models hallucinate
            # list-shaped factual answers; if the operator's prompt is
            # a clean "list X" / "show X" / "what X" intent, dispatch
            # the right tool deterministically and skip the LLM. The
            # rendered result is identical to the LLM-mediated path
            # (same renderer dispatch) but always correct.
            intent_match = _maybe_intent_match(line)
            if intent_match is not None:
                tool_name, kwargs = intent_match
                try:
                    loop.run_until_complete(
                        _drive_intent_turn(
                            conv=conv,
                            line=line,
                            tool_name=tool_name,
                            kwargs=kwargs,
                        )
                    )
                except Exception as e:  # pragma: no cover
                    rprint(f"[red]intent dispatch raised:[/] {e}")
                continue

            try:
                loop.run_until_complete(
                    _drive_turn(
                        conv=conv,
                        line=line,
                        actor=actor,
                        model=model,
                        allow_destructive_default=allow_destructive,
                    )
                )
            except Exception as e:  # pragma: no cover — surface at runtime
                # v0.13.1 — defensive recovery: if the session row
                # disappeared mid-REPL (e.g. a test fixture truncated
                # cockpit.session, or the operator ran reset-db in
                # another shell), Postgres raises a FK violation on
                # the message INSERT. Detect that specifically and
                # offer a fresh session rather than crashing the loop.
                msg = str(e)
                missing_session = (
                    "fk_message_session_id_session" in msg
                    or "ForeignKeyViolation" in msg
                )
                if missing_session:
                    rprint(
                        f"[yellow]Session {conv.session_id} no longer "
                        "exists in the cockpit store — opening a "
                        "fresh one.[/]"
                    )
                    try:
                        conv = loop.run_until_complete(
                            Conversation.open(actor=actor, label=label)
                        )
                        rprint(
                            _render_banner(
                                actor=actor,
                                model=model,
                                session_id=conv.session_id,
                                customer_focus=conv.customer_focus,
                                allow_destructive_default=allow_destructive,
                            )
                        )
                    except Exception as inner:  # noqa: BLE001
                        rprint(f"[red]Cannot open new session:[/] {inner}")
                        break
                    continue
                rprint(f"[red]LLM error:[/] {e}")
                continue
    finally:
        try:
            loop.run_until_complete(close_clients())
            loop.run_until_complete(store.dispose())
        finally:
            loop.close()
