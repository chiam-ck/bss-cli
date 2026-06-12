"""In-memory chat state for the v0.12 self-serve portal.

Two collections, both single-process and TTL-evicted:

* ``ChatConversationStore`` — one running conversation per customer.
  Holds the full message history (user + assistant turns) so the
  next POST /chat/message can render prior context into the system
  prompt. The conversation persists across page navigations within
  the portal: returning to /chat or popping the floating widget
  reloads the same thread.

* ``ChatTurnStore`` — one in-flight turn per stream. Keyed on a
  random session_id the SSE handler uses to find the customer's
  question for a given GET /chat/events/{sid}. A turn always has a
  back-pointer to the customer's conversation so the SSE handler
  can append the assistant's final text on completion.

Single-process only; v1.x can swap either store for Redis if
multiple portal replicas land.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

Role = Literal["user", "assistant"]


@dataclass
class ConversationMessage:
    role: Role
    body: str
    at: float = field(default_factory=monotonic)


@dataclass
class ChatConversation:
    customer_id: str
    messages: list[ConversationMessage] = field(default_factory=list)
    created_at: float = field(default_factory=monotonic)
    last_active_at: float = field(default_factory=monotonic)

    def append(self, role: Role, body: str) -> None:
        self.messages.append(ConversationMessage(role=role, body=body))
        self.last_active_at = monotonic()

    def transcript_text(self) -> str:
        """Render as ``User: ...\\nAssistant: ...`` text suitable for
        the system prompt's prior-conversation context block and for
        case.open_for_me's transcript hashing."""
        lines: list[str] = []
        for m in self.messages:
            label = "User" if m.role == "user" else "Assistant"
            lines.append(f"{label}: {m.body}")
        return "\n".join(lines) + ("\n" if lines else "")


@dataclass
class ChatTurn:
    session_id: str
    customer_id: str
    question: str
    created_at: float = field(default_factory=monotonic)
    done: bool = False
    error: str | None = None
    final_text: str = ""
    ownership_violation: bool = False
    event_log: list[dict[str, Any]] = field(default_factory=list)


class ChatConversationStore:
    """One conversation per (customer_id). Bounded TTL idle eviction."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, ChatConversation] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, customer_id: str) -> ChatConversation:
        async with self._lock:
            self._prune_locked()
            conv = self._items.get(customer_id)
            if conv is None:
                conv = ChatConversation(customer_id=customer_id)
                self._items[customer_id] = conv
            return conv

    async def get(self, customer_id: str) -> ChatConversation | None:
        async with self._lock:
            self._prune_locked()
            return self._items.get(customer_id)

    async def reset(self, customer_id: str) -> None:
        async with self._lock:
            self._items.pop(customer_id, None)

    def _prune_locked(self) -> None:
        cutoff = monotonic() - self._ttl
        expired = [
            cid for cid, c in self._items.items() if c.last_active_at < cutoff
        ]
        for cid in expired:
            self._items.pop(cid, None)


class ChatTurnStore:
    """Per-stream lookup. The conversation store owns the durable
    history; this store is the per-SSE-stream working set."""

    def __init__(self, ttl_seconds: int = 1800) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, ChatTurn] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, customer_id: str, question: str) -> ChatTurn:
        turn = ChatTurn(
            session_id=uuid.uuid4().hex,
            customer_id=customer_id,
            question=question,
        )
        async with self._lock:
            self._prune_locked()
            self._items[turn.session_id] = turn
        return turn

    async def get(self, session_id: str) -> ChatTurn | None:
        async with self._lock:
            self._prune_locked()
            return self._items.get(session_id)

    def _prune_locked(self) -> None:
        cutoff = monotonic() - self._ttl
        expired = [sid for sid, t in self._items.items() if t.created_at < cutoff]
        for sid in expired:
            self._items.pop(sid, None)
