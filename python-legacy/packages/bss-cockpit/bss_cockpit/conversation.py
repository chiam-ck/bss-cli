"""Conversation + ConversationStore — Postgres-backed cockpit session store.

Schema lives in ``cockpit.session`` / ``cockpit.message`` /
``cockpit.pending_destructive`` (alembic 0014, v0.13 PR1). Both
surfaces of the cockpit (CLI REPL + browser veneer) read and write
through this module — no second store, no in-memory shadow. That
single-store invariant is the entire point of v0.13: exit ``bss``,
open ``/cockpit/<id>`` in the browser, see the same turns.

Design choices worth surfacing:

* The class :class:`ConversationStore` is the explicit, testable seam
  — it owns the SQLAlchemy ``async_sessionmaker`` and every public
  method opens its own short-lived ``AsyncSession``. Tests instantiate
  one bound to the test DB; production code calls
  :func:`configure_store` at module import (REPL) or in lifespan
  (portal) to register a process-wide default.
* The :class:`Conversation` instance returned by ``open`` / ``resume``
  caches the session row's mutable attributes (``actor``,
  ``customer_focus``, ``allow_destructive``, ``state``, ``label``).
  Mutating methods (``set_focus``, ``close``) update both Postgres and
  the cache. Concurrent writes from another surface are NOT live —
  consumers re-resume to pick up changes (the spec accepts this; v0.13
  is single-operator-by-design, contention is rare).
* ``transcript_text()`` reads messages in ``created_at`` order and
  formats them as a plain-text transcript suitable for
  ``astream_once(transcript=...)``. Bounded only by the operator's
  /reset discipline; truncation to a synopsis is a v0.14 concern (see
  phases/V0_13_0.md "The trap" — long-transcript token-cost trap).
* IDs are ``SES-YYYYMMDD-<8 hex>`` so a session id is usefully
  human-readable (the date) and rare-collision (the hex). Generated
  via :mod:`secrets` — small enough for one operator to glance at.

The orchestrator's ``astream_once(transcript=...)`` parameter is the
consumer; it is currently wired only into ``auth_context.set_actor``
(v0.12 escalation). PR6 extends ``astream_once`` to feed the prior
transcript into the LangGraph messages list so multi-turn coherence
actually lands; until then the transcript is captured for the
audit/store but does not influence the LLM's reply.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from bss_clock import now as clock_now
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from .chrome_filter import is_cockpit_chrome

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConversationMessage:
    """One row from ``cockpit.message``, structured for template /
    render consumption.

    v0.13.1 — the prior path was to serialize messages into a single
    text transcript via ``transcript_text()`` and re-parse on the
    consumer side, which is lossy when an assistant message contains
    blank lines (e.g., a markdown table separating paragraphs). This
    dataclass exposes the rows directly so renderers don't have to
    parse anything.
    """

    id: int
    role: str  # 'user' | 'assistant' | 'tool'
    content: str
    tool_name: str | None
    created_at: datetime


@dataclass(frozen=True)
class ConversationSummary:
    """One row of ``Conversation.list_for(actor)`` output.

    Carries enough for the REPL ``/sessions`` Rich table and the
    browser sessions index without re-fetching messages.
    """

    session_id: str
    actor: str
    label: str | None
    customer_focus: str | None
    state: str
    started_at: datetime
    last_active_at: datetime
    message_count: int


@dataclass(frozen=True)
class PendingDestructive:
    """Returned by ``Conversation.consume_pending_destructive`` on hit.

    Carries the agent's prior proposal so the next turn can flip
    ``allow_destructive=True`` and run the named tool. The row is
    deleted by ``consume`` (single-shot per propose).
    """

    tool_name: str
    tool_args: dict[str, Any]
    proposal_message_id: int
    proposed_at: datetime


# ─────────────────────────────────────────────────────────────────────
# ConversationStore + Conversation
# ─────────────────────────────────────────────────────────────────────


def _new_session_id() -> str:
    """``SES-YYYYMMDD-<8 hex>`` — readable + rare-collision."""
    today = clock_now().strftime("%Y%m%d")
    return f"SES-{today}-{secrets.token_hex(4)}"


class ConversationStore:
    """Postgres-backed factory + singleton holder.

    Construct from either an ``AsyncEngine`` or a ``BSS_DB_URL`` string;
    the store builds an ``async_sessionmaker`` and uses it for every
    short-lived transaction. ``dispose()`` closes the underlying engine
    (used by tests + portal lifespan shutdown).
    """

    def __init__(
        self,
        engine: AsyncEngine | None = None,
        *,
        db_url: str | None = None,
    ) -> None:
        if engine is None and not db_url:
            raise ValueError(
                "ConversationStore requires either engine= or db_url="
            )
        self._engine = engine or create_async_engine(db_url)  # type: ignore[arg-type]
        self._owns_engine = engine is None
        self._factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    async def dispose(self) -> None:
        """Release the engine if this store owns it. Safe to call twice."""
        if self._owns_engine and self._engine is not None:
            await self._engine.dispose()
            self._owns_engine = False

    # ── factory methods (also reachable via Conversation.classmethods) ──

    async def open(
        self,
        actor: str,
        *,
        label: str | None = None,
        customer_focus: str | None = None,
        allow_destructive: bool = False,
        tenant_id: str = "DEFAULT",
    ) -> "Conversation":
        """Insert a fresh session row and return a handle."""
        if not actor:
            raise ValueError("Conversation.open: actor must be non-empty")
        session_id = _new_session_id()
        now = clock_now()
        async with self._factory() as db:
            await db.execute(
                text(
                    """
                    INSERT INTO cockpit.session (
                        id, actor, customer_focus, allow_destructive,
                        state, started_at, last_active_at, label, tenant_id
                    )
                    VALUES (
                        :id, :actor, :focus, :allow_dest,
                        'active', :now, :now, :label, :tenant_id
                    )
                    """
                ),
                {
                    "id": session_id,
                    "actor": actor,
                    "focus": customer_focus,
                    "allow_dest": allow_destructive,
                    "now": now,
                    "label": label,
                    "tenant_id": tenant_id,
                },
            )
            await db.commit()
        log.info(
            "cockpit.session.opened",
            session_id=session_id,
            actor=actor,
            label=label,
            customer_focus=customer_focus,
        )
        return Conversation(
            store=self,
            session_id=session_id,
            actor=actor,
            customer_focus=customer_focus,
            allow_destructive=allow_destructive,
            state="active",
            label=label,
            started_at=now,
            last_active_at=now,
        )

    async def resume(self, session_id: str) -> "Conversation":
        """Re-load an existing session by id. Raises ``LookupError`` if missing."""
        async with self._factory() as db:
            row = (
                await db.execute(
                    text(
                        """
                        SELECT id, actor, customer_focus, allow_destructive,
                               state, started_at, last_active_at, label
                        FROM cockpit.session
                        WHERE id = :id
                        """
                    ),
                    {"id": session_id},
                )
            ).one_or_none()
            if row is None:
                raise LookupError(
                    f"cockpit session {session_id!r} not found"
                )
            # Touch last_active_at so /sessions ranks resumed sessions at top.
            now = clock_now()
            await db.execute(
                text(
                    "UPDATE cockpit.session SET last_active_at = :now "
                    "WHERE id = :id"
                ),
                {"now": now, "id": session_id},
            )
            await db.commit()
        return Conversation(
            store=self,
            session_id=row.id,
            actor=row.actor,
            customer_focus=row.customer_focus,
            allow_destructive=row.allow_destructive,
            state=row.state,
            label=row.label,
            started_at=row.started_at,
            last_active_at=now,
        )

    async def list_for(
        self,
        actor: str,
        *,
        active_only: bool = True,
        limit: int = 50,
    ) -> list[ConversationSummary]:
        """Sessions for ``actor``, newest first. ``active_only`` excludes closed."""
        clause = "WHERE actor = :actor"
        if active_only:
            clause += " AND state = 'active'"
        async with self._factory() as db:
            rows = (
                await db.execute(
                    text(
                        f"""
                        SELECT s.id, s.actor, s.label, s.customer_focus,
                               s.state, s.started_at, s.last_active_at,
                               COALESCE(m.cnt, 0) AS message_count
                        FROM cockpit.session s
                        LEFT JOIN (
                            SELECT session_id, COUNT(*) AS cnt
                            FROM cockpit.message
                            GROUP BY session_id
                        ) m ON m.session_id = s.id
                        {clause}
                        ORDER BY s.last_active_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"actor": actor, "limit": limit},
                )
            ).all()
        return [
            ConversationSummary(
                session_id=r.id,
                actor=r.actor,
                label=r.label,
                customer_focus=r.customer_focus,
                state=r.state,
                started_at=r.started_at,
                last_active_at=r.last_active_at,
                message_count=int(r.message_count),
            )
            for r in rows
        ]


@dataclass
class Conversation:
    """Handle on one cockpit session.

    Instances are returned by ``ConversationStore.open`` / ``.resume``
    (or the equivalent classmethods that delegate to the configured
    default store). Mutating methods write Postgres + update the cache.
    """

    store: ConversationStore
    session_id: str
    actor: str
    customer_focus: str | None
    allow_destructive: bool
    state: str
    label: str | None
    started_at: datetime
    last_active_at: datetime

    # ── classmethod delegators (default-store convenience) ───────────

    @classmethod
    async def open(
        cls,
        actor: str,
        *,
        label: str | None = None,
        customer_focus: str | None = None,
        allow_destructive: bool = False,
        tenant_id: str = "DEFAULT",
    ) -> "Conversation":
        return await _require_default_store().open(
            actor,
            label=label,
            customer_focus=customer_focus,
            allow_destructive=allow_destructive,
            tenant_id=tenant_id,
        )

    @classmethod
    async def resume(cls, session_id: str) -> "Conversation":
        return await _require_default_store().resume(session_id)

    @classmethod
    async def list_for(
        cls,
        actor: str,
        *,
        active_only: bool = True,
        limit: int = 50,
    ) -> list[ConversationSummary]:
        return await _require_default_store().list_for(
            actor, active_only=active_only, limit=limit
        )

    # ── instance API ─────────────────────────────────────────────────

    async def append_user_turn(self, content: str) -> int:
        """Append a user-role message. Returns its db id."""
        return await self._append_message(
            role="user", content=content, tool_calls_json=None
        )

    async def append_assistant_turn(
        self,
        content: str,
        *,
        tool_calls_json: list[dict[str, Any]] | None = None,
    ) -> int:
        """Append an assistant-role message; ``tool_calls_json`` is the
        v0.12 AgentEvent shape captured for the assistant turn that
        proposed them. Pass ``None`` for plain reply turns."""
        return await self._append_message(
            role="assistant",
            content=content,
            tool_calls_json=tool_calls_json,
        )

    async def append_tool_turn(
        self,
        tool_name: str,
        content: str,
    ) -> int:
        """Append a tool-role message — the tool's stringified result.

        Used by the REPL ``/360`` slash command to persist a rendered
        customer-360 card so the same view shows up in the browser.
        """
        return await self._append_message(
            role="tool",
            content=content,
            tool_calls_json={"tool_name": tool_name},
        )

    async def _append_message(
        self,
        *,
        role: str,
        content: str,
        tool_calls_json: dict[str, Any] | list[dict[str, Any]] | None,
    ) -> int:
        if role not in {"user", "assistant", "tool"}:
            raise ValueError(
                f"unknown role {role!r} (expected user|assistant|tool)"
            )
        async with self.store._factory() as db:
            now = clock_now()
            row = (
                await db.execute(
                    text(
                        """
                        INSERT INTO cockpit.message (
                            session_id, role, content, tool_calls_json,
                            created_at, tenant_id
                        )
                        VALUES (
                            :sid, :role, :content, CAST(:tc AS json),
                            :now, 'DEFAULT'
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "sid": self.session_id,
                        "role": role,
                        "content": content,
                        # asyncpg's JSON binding accepts a string here.
                        "tc": (
                            None if tool_calls_json is None
                            else _to_json(tool_calls_json)
                        ),
                        "now": now,
                    },
                )
            ).one()
            await db.execute(
                text(
                    "UPDATE cockpit.session SET last_active_at = :now "
                    "WHERE id = :sid"
                ),
                {"now": now, "sid": self.session_id},
            )
            await db.commit()
        self.last_active_at = now
        return int(row.id)

    async def list_messages(self) -> list[ConversationMessage]:
        """Structured view of the message log for template / renderer
        consumption. Avoids the lossy ``\\n\\n``-split round trip that
        ``transcript_text()`` + a re-parser produce.
        """
        async with self.store._factory() as db:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT id, role, content, tool_calls_json, created_at
                        FROM cockpit.message
                        WHERE session_id = :sid
                        ORDER BY created_at, id
                        """
                    ),
                    {"sid": self.session_id},
                )
            ).all()
        out: list[ConversationMessage] = []
        for r in rows:
            tc = r.tool_calls_json
            tool_name: str | None = None
            if r.role == "tool" and isinstance(tc, dict):
                tool_name = tc.get("tool_name") or None
            out.append(
                ConversationMessage(
                    id=int(r.id),
                    role=r.role,
                    content=r.content,
                    tool_name=tool_name,
                    created_at=r.created_at,
                )
            )
        return out

    async def transcript_text(self) -> str:
        """Plain-text transcript for ``astream_once(transcript=...)``.

        Format: ``role: content`` lines, blank line between turns, in
        ``created_at`` order. Tool-role messages are prefixed with
        ``tool[NAME]:`` so the LLM can see what the cockpit's slash
        commands have surfaced inline.

        v1.5 — assistant rows that match the cockpit's own emit chrome
        (``(no reply)``, the empty-final recovery bubble, the route
        error fallback, the citation-guard fallback) are filtered out
        before the transcript reaches the LLM. See
        ``bss_cockpit.chrome_filter`` for the rationale — leaving
        chrome in causes the LLM to mimic the placeholder output
        instead of doing the work.
        """
        async with self.store._factory() as db:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT role, content, tool_calls_json
                        FROM cockpit.message
                        WHERE session_id = :sid
                        ORDER BY created_at, id
                        """
                    ),
                    {"sid": self.session_id},
                )
            ).all()
        out: list[str] = []
        for r in rows:
            if r.role == "tool":
                tc = r.tool_calls_json or {}
                tool_name = (
                    tc.get("tool_name", "")
                    if isinstance(tc, dict) else ""
                )
                prefix = (
                    f"tool[{tool_name}]" if tool_name else "tool"
                )
                out.append(f"{prefix}:\n{r.content}")
            elif r.role == "assistant" and is_cockpit_chrome(r.content):
                # Drop the chrome row entirely — the LLM never said it,
                # so showing it back as "prior reasoning" only invites
                # mimicry. User/tool turns around it stitch back to a
                # coherent transcript because LLMs handle "I asked, I
                # got a tool result, I'm asked again" naturally.
                continue
            else:
                out.append(f"{r.role}:\n{r.content}")
        return "\n\n".join(out)

    async def reset(self) -> None:
        """Clear all messages on this session row. Keeps the row itself."""
        async with self.store._factory() as db:
            await db.execute(
                text(
                    "DELETE FROM cockpit.message WHERE session_id = :sid"
                ),
                {"sid": self.session_id},
            )
            # The pending_destructive row's FK is to message; clear it
            # explicitly in case it survived (CASCADE would eat it but
            # being explicit keeps the contract obvious).
            await db.execute(
                text(
                    "DELETE FROM cockpit.pending_destructive "
                    "WHERE session_id = :sid"
                ),
                {"sid": self.session_id},
            )
            await db.commit()
        log.info("cockpit.session.reset", session_id=self.session_id)

    async def close(self) -> None:
        """Mark this session ``state='closed'``. Idempotent."""
        async with self.store._factory() as db:
            await db.execute(
                text(
                    "UPDATE cockpit.session SET state = 'closed', "
                    "last_active_at = :now WHERE id = :sid"
                ),
                {"now": clock_now(), "sid": self.session_id},
            )
            await db.commit()
        self.state = "closed"

    async def set_focus(self, customer_id: str | None) -> None:
        """Pin a customer for the system-prompt focus block. ``None`` clears."""
        async with self.store._factory() as db:
            await db.execute(
                text(
                    "UPDATE cockpit.session SET customer_focus = :cf "
                    "WHERE id = :sid"
                ),
                {"cf": customer_id, "sid": self.session_id},
            )
            await db.commit()
        self.customer_focus = customer_id

    async def set_pending_destructive(
        self,
        tool_name: str,
        args: dict[str, Any],
        proposal_message_id: int,
    ) -> None:
        """Stash an in-flight propose. Replaces any prior unconfirmed row."""
        async with self.store._factory() as db:
            await db.execute(
                text(
                    """
                    INSERT INTO cockpit.pending_destructive (
                        session_id, proposed_at, tool_name,
                        tool_args_json, proposal_message_id, tenant_id
                    )
                    VALUES (
                        :sid, :now, :tn, CAST(:args AS json), :pmid, 'DEFAULT'
                    )
                    ON CONFLICT (session_id) DO UPDATE SET
                        proposed_at = EXCLUDED.proposed_at,
                        tool_name = EXCLUDED.tool_name,
                        tool_args_json = EXCLUDED.tool_args_json,
                        proposal_message_id = EXCLUDED.proposal_message_id
                    """
                ),
                {
                    "sid": self.session_id,
                    "now": clock_now(),
                    "tn": tool_name,
                    "args": _to_json(args),
                    "pmid": proposal_message_id,
                },
            )
            await db.commit()

    async def peek_pending_destructive(self) -> PendingDestructive | None:
        """Read the in-flight propose row WITHOUT deleting it. ``None`` if
        absent. v1.5 — used by the REPL ``/confirm`` slash handler to
        report what's staged (or warn loudly when nothing is, which
        catches the mimicry-stalled case where the LLM narrated the
        proposal in prose instead of emitting a tool_call).
        """
        async with self.store._factory() as db:
            row = (
                await db.execute(
                    text(
                        """
                        SELECT tool_name, tool_args_json,
                               proposal_message_id, proposed_at
                        FROM cockpit.pending_destructive
                        WHERE session_id = :sid
                        """
                    ),
                    {"sid": self.session_id},
                )
            ).one_or_none()
        if row is None:
            return None
        args = row.tool_args_json
        if isinstance(args, str):
            import json as _json
            args = _json.loads(args)
        return PendingDestructive(
            tool_name=row.tool_name,
            tool_args=args or {},
            proposal_message_id=int(row.proposal_message_id),
            proposed_at=row.proposed_at,
        )

    async def consume_pending_destructive(self) -> PendingDestructive | None:
        """Atomically read+delete the in-flight propose row. ``None`` if absent."""
        async with self.store._factory() as db:
            row = (
                await db.execute(
                    text(
                        """
                        DELETE FROM cockpit.pending_destructive
                        WHERE session_id = :sid
                        RETURNING tool_name, tool_args_json,
                                  proposal_message_id, proposed_at
                        """
                    ),
                    {"sid": self.session_id},
                )
            ).one_or_none()
            await db.commit()
        if row is None:
            return None
        args = row.tool_args_json
        # asyncpg returns json columns as already-parsed dicts; defend
        # against a str fallback if someone wires this against a non-pg
        # dialect or stores a string by mistake.
        if isinstance(args, str):
            import json as _json
            args = _json.loads(args)
        return PendingDestructive(
            tool_name=row.tool_name,
            tool_args=args or {},
            proposal_message_id=int(row.proposal_message_id),
            proposed_at=row.proposed_at,
        )


# ─────────────────────────────────────────────────────────────────────
# Default-store registry
# ─────────────────────────────────────────────────────────────────────


_default_store: ConversationStore | None = None


def configure_store(store: ConversationStore | None) -> None:
    """Register the process-wide default store.

    ``Conversation.open`` / ``.resume`` / ``.list_for`` delegate here.
    Pass ``None`` to clear (used by tests). The REPL calls this at
    module-import time; the portal calls this in lifespan.
    """
    global _default_store
    _default_store = store


def _require_default_store() -> ConversationStore:
    if _default_store is None:
        raise RuntimeError(
            "bss_cockpit: no default store configured. Call "
            "bss_cockpit.configure_store(ConversationStore(...)) at "
            "process start (REPL module import / portal lifespan)."
        )
    return _default_store


# ─────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────


def _to_json(value: Any) -> str:
    """Serialize ``value`` as a JSON string for the asyncpg ``CAST(... AS json)``
    insert path. Centralised so tests + production agree on the shape."""
    import json as _json
    return _json.dumps(value, separators=(",", ":"), default=str)
