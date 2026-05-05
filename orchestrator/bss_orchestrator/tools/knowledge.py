"""v0.20 — knowledge.search + knowledge.get tools (operator_cockpit only).

The cockpit's failure mode pre-v0.20 was paraphrasing handbook /
runbook content from the LLM's training data, which is stale. v0.20
indexes the doc corpus into ``knowledge.doc_chunk`` (Postgres FTS,
optionally pgvector embeddings) and exposes two tools the LLM
**must** call before claiming "the handbook says..." anything.

The citation guard in the REPL + browser cockpit (see
``cli/bss_cli/repl.py`` + ``portals/csr/bss_csr/routes/cockpit.py``)
catches un-cited handbook claims and replaces them with a templated
fallback pointing at ``bss admin knowledge search "<query>"``.

These tools live in the ``operator_cockpit`` profile only. Customer
chat does NOT get them by doctrine — the handbook + runbooks describe
destructive operator flows + perimeter posture, none of which belongs
in customer chat. Greppable rule + ``validate_profiles()`` enforces.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog
from bss_knowledge import get_chunk, search_fts
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from ..config import settings
from ._registry import register

log = structlog.get_logger(__name__)


def _knowledge_enabled() -> bool:
    """Read BSS_KNOWLEDGE_ENABLED at registration time. Default True
    (matches the v0.20 doctrine — operators opt OUT for air-gapped /
    stale-doc deploys). Read once at import so flipping at runtime
    requires a restart (consistent with token / provider patterns)."""
    raw = os.environ.get("BSS_KNOWLEDGE_ENABLED", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


_ENABLED = _knowledge_enabled()


# ─── lazy async engine (mirrors chat_caps.py pattern) ─────────────────

_engine: AsyncEngine | None = None
_engine_lock = asyncio.Lock()


async def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is not None:
        return _engine
    async with _engine_lock:
        if _engine is not None:
            return _engine
        if not settings.db_url:
            raise RuntimeError(
                "knowledge tools require BSS_DB_URL to be set — the "
                "orchestrator reads knowledge.doc_chunk directly."
            )
        _engine = create_async_engine(
            settings.db_url, pool_size=2, max_overflow=2, pool_pre_ping=True
        )
        return _engine


async def _close_engine() -> None:
    """Test hook."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


# ─── tools ────────────────────────────────────────────────────────────


def _maybe_register(name: str):
    """Register only when BSS_KNOWLEDGE_ENABLED. When disabled, the
    tool name is absent from TOOL_REGISTRY and validate_profiles()
    will fail at startup if the profile still lists it. So the
    profile entry is also gated — see _profiles.py."""
    if _ENABLED:
        return register(name)
    # No-op decorator when disabled.
    def _identity(fn):
        return fn
    return _identity


@_maybe_register("knowledge.search")
async def knowledge_search(
    query: str,
    k: int = 5,
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    """Search the BSS-CLI documentation corpus (handbook, doctrine, runbooks,
    architecture, decisions, tool surface, roadmap, contributing).

    CALL THIS FIRST for every operator question that isn't a direct
    list/show/get of platform data. That includes:
      - "how do I X" / "how does X work" / "what's the procedure for X"
      - "what is X" / "what does X mean" / "what's the difference between X and Y"
      - "where do I find X" / "what file/section covers X"
      - "is X allowed" / "what's the rule on X" / "what's the doctrine on X"
      - "what env var / setting / flag controls X"
      - "what command do I run to X"

    Your training data is months stale and frequently wrong about
    post-v0.x doctrine. The corpus IS the authoritative source. If
    you are about to write prose explaining something rather than
    rendering tool output, you should have called this tool first.

    Cite the returned ``anchor`` + ``source_path`` in your reply, e.g.
    ``[HANDBOOK §8.4](docs/HANDBOOK.md#84-rotate-api-tokens)``. The
    citation guard at the REPL + browser surface replaces un-cited
    first-person handbook claims with a templated fallback.

    Args:
        query: Natural-language search query, e.g. "rotate cockpit token",
            "prebaked KYC env flag", "how does roaming exhaustion work".
        k: How many top hits to return. Default 5.
        kinds: Optional list of doc kinds to scope the search.

            **STRONG DEFAULT: leave this UNSET (omit / pass ``None``) and
            let cross-corpus FTS ranking decide.** A "how do I manage
            MSISDN pool" question lands in ``architecture`` (topology)
            + ``runbook`` (port-in / port-out) + ``tool_surface``
            (inventory tools) + ``doctrine`` (domain coverage), NOT in
            ``handbook``. Restricting to ``kinds=["handbook"]`` silently
            zeroes out hits and the operator sees a "found nothing"
            reply that's just a filter mistake.

            Only pass ``kinds`` when the operator's question is
            EXPLICITLY scoped to a doc family — e.g. they ask "what
            does the **handbook** say about X" → ``kinds=["handbook"]``,
            or "what's the **doctrine** on Y" → ``kinds=["doctrine"]``.
            Implicit phrasing ("how do I", "what env var", "explain X")
            stays cross-corpus.

            Valid kinds: ``handbook``, ``doctrine``, ``runbook``,
            ``architecture``, ``decisions``, ``tool_surface``, ``roadmap``,
            ``contributing``.

    Returns:
        ``{"hits": [{"anchor", "source_path", "heading_path", "kind",
        "snippet", "content"}, ...], "query": "..."}``.

        IMPORTANT: read ``content`` to answer, NOT ``snippet``.
        ``snippet`` is a short ts_headline excerpt for ranking display.
        ``content`` is the FULL chunk — env var lists, command tables,
        step-by-step procedures all live there. If you answer from
        ``snippet``, you'll cut off in the middle of the actual
        information and feel weak. Use ``content``.

        Empty hits = no relevant content. Don't answer from training
        data; tell the operator what you searched and what you didn't
        find.

    Raises:
        RuntimeError: BSS_DB_URL not set on the orchestrator process.
            Configuration error, not a runtime miss.
    """
    engine = await _get_engine()
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        hits = await search_fts(session, query=query, k=k, kinds=kinds)
    payload = {"hits": [h.to_dict() for h in hits], "query": query}
    log.info(
        "knowledge.search",
        query=query[:120],
        k=k,
        kinds=kinds,
        hit_count=len(hits),
        top_anchor=hits[0].anchor if hits else None,
    )
    return payload


@_maybe_register("knowledge.get")
async def knowledge_get(anchor: str, source_path: str) -> dict[str, Any]:
    """Pull the full content of one indexed doc chunk by its anchor +
    source_path. Use this when ``knowledge.search`` returned a relevant
    hit but the snippet doesn't carry enough detail.

    Args:
        anchor: The chunk's anchor, e.g. ``84-rotate-api-tokens``.
        source_path: The chunk's source path, e.g. ``docs/HANDBOOK.md``.

    Returns:
        ``{"anchor", "source_path", "heading_path", "kind", "content",
        "indexed_at"}`` or ``{"error": "NOT_FOUND"}`` if the chunk
        doesn't exist (re-anchored / removed since last index).

    Raises:
        RuntimeError: BSS_DB_URL not set on the orchestrator process.
            Configuration error, not a runtime miss.
    """
    engine = await _get_engine()
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        result = await get_chunk(session, anchor=anchor, source_path=source_path)
    if result is None:
        return {
            "error": "NOT_FOUND",
            "message": (
                f"No indexed chunk at anchor={anchor!r} in {source_path!r}. "
                "The section may have been re-anchored or removed since the "
                "last reindex. Try knowledge.search with related keywords."
            ),
        }
    log.info(
        "knowledge.get",
        anchor=anchor,
        source_path=source_path,
        kind=result["kind"],
    )
    return result
